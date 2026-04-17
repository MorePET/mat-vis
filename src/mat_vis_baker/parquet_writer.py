"""Pack baked ONGs into Parquet + generate rowmap JSON for HTTP range reads.

Binary (PNG) columns are UNCOMPRESSED so that byte-range reads return raw
PNG payload without any decompression. Scalar columns use ZSTD.

Row group size = 1 (one material per row group) for per-material offset discovery.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from mat_vis_baker.common import BAKER_VERSION, CANONICAL_CHANNELS, MaterialRecord

log = logging.getLogger("mat-vis-baker.parquet")

PNG_MAGIC = b"\x89PNG"

CHANNEL_COLS = frozenset(CANONICAL_CHANNELS)

_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("category", pa.string(), nullable=False),
        pa.field("resolution_px", pa.int32(), nullable=False),
        pa.field("color", pa.binary()),
        pa.field("normal", pa.binary()),
        pa.field("roughness", pa.binary()),
        pa.field("metalness", pa.binary()),
        pa.field("ao", pa.binary()),
        pa.field("displacement", pa.binary()),
        pa.field("emission", pa.binary()),
        pa.field("source_url", pa.string(), nullable=False),
        pa.field("source_license", pa.string(), nullable=False),
        pa.field("baker_version", pa.string(), nullable=False),
        pa.field("baked_at", pa.string(), nullable=False),
    ]
)


def _read_png_bytes(path: Path | None) -> bytes | None:
    if path is None or not path.exists():
        return None
    return path.read_bytes()


def write_parquet(
    records: list[MaterialRecord],
    source: str,
    tier: str,
    output_path: Path,
    resolution_px: int,
) -> Path:
    """Write a Parquet file streaming one row at a time. Constant memory.

    Each material's PNGs are read, written as a single row group, then freed.
    Never holds more than one material's textures in memory.
    """
    ok_records = [r for r in records if r.status == "ok"]
    if not ok_records:
        raise ValueError("No successful records to write")

    now = datetime.now(timezone.utc).isoformat()
    compression = {
        col: "NONE" if col in CHANNEL_COLS else "ZSTD" for col in [f.name for f in _SCHEMA]
    }
    use_dictionary = {col: col not in CHANNEL_COLS for col in [f.name for f in _SCHEMA]}

    output_path.parent.mkdir(parents=True, exist_ok=True)

    writer = pq.ParquetWriter(
        output_path,
        _SCHEMA,
        compression=compression,
        use_dictionary=use_dictionary,
    )

    try:
        for rec in ok_records:
            # Build one row — read PNGs, write, free
            row = {
                "id": [rec.id],
                "source": [source],
                "category": [rec.category],
                "resolution_px": [resolution_px],
                "source_url": [rec.source_url],
                "source_license": [rec.source_license],
                "baker_version": [BAKER_VERSION],
                "baked_at": [now],
            }
            for ch in CANONICAL_CHANNELS:
                row[ch] = [_read_png_bytes(rec.texture_paths.get(ch))]

            table = pa.table(row, schema=_SCHEMA)
            writer.write_table(table)
            del row, table  # free PNG bytes immediately
    finally:
        writer.close()

    log.info(
        "wrote %s (%d rows, %.1f MB)",
        output_path,
        len(ok_records),
        output_path.stat().st_size / 1e6,
    )
    return output_path


MAX_PARTITION_BYTES = 1_800_000_000  # 1.8 GB — stay under GitHub's 2 GB limit


def _estimate_partition_size(records: list[MaterialRecord]) -> int:
    """Estimate parquet size from texture file sizes."""
    total = 0
    for rec in records:
        for ch, path in rec.texture_paths.items():
            if not ch.startswith("_") and path.exists():
                total += path.stat().st_size
    return total


def write_partitioned_parquet(
    records: list[MaterialRecord],
    source: str,
    tier: str,
    output_dir: Path,
    resolution_px: int,
) -> list[Path]:
    """Write size-aware partitioned parquet files. Returns list of paths.

    First partitions by category. If a category exceeds MAX_PARTITION_BYTES,
    splits further into numbered chunks (alphabetical by material ID).
    """
    from collections import defaultdict

    ok_records = [r for r in records if r.status == "ok"]
    if not ok_records:
        raise ValueError("No successful records to write")

    by_cat: dict[str, list[MaterialRecord]] = defaultdict(list)
    for rec in ok_records:
        by_cat[rec.category].append(rec)

    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    for cat in sorted(by_cat.keys()):
        cat_records = sorted(by_cat[cat], key=lambda r: r.id)
        est_size = _estimate_partition_size(cat_records)

        if est_size <= MAX_PARTITION_BYTES:
            # Single partition
            filename = f"mat-vis-{source}-{tier}-{cat}.parquet"
            path = output_dir / filename
            write_parquet(cat_records, source, tier, path, resolution_px)
            paths.append(path)
        else:
            # Split into chunks that fit under the limit
            n_chunks = (est_size // MAX_PARTITION_BYTES) + 1
            chunk_size = max(1, len(cat_records) // n_chunks)
            log.info(
                "%s: %.1f GB estimated, splitting into %d chunks of ~%d materials",
                cat,
                est_size / 1e9,
                n_chunks,
                chunk_size,
            )
            for i in range(0, len(cat_records), chunk_size):
                chunk = cat_records[i : i + chunk_size]
                chunk_num = (i // chunk_size) + 1
                filename = f"mat-vis-{source}-{tier}-{cat}-{chunk_num}.parquet"
                path = output_dir / filename
                write_parquet(chunk, source, tier, path, resolution_px)
                paths.append(path)

    log.info(
        "wrote %d partitioned parquet files for %s %s (%d total records)",
        len(paths),
        source,
        tier,
        len(ok_records),
    )
    return paths


# ── rowmap generation ───────────────────────────────────────────


_MAX_PAGE_HEADER_SIZE = 256  # Thrift page header can be larger for nullable columns


def _find_png_in_page(data: bytes, page_offset: int, png_length: int) -> int | None:
    """Find PNG magic within the first bytes after a data page offset.

    Only scans a small window after the page offset to avoid false positives
    from PNG magic bytes inside other columns' binary data.
    """
    search_end = min(page_offset + _MAX_PAGE_HEADER_SIZE, len(data))
    idx = data.find(PNG_MAGIC, page_offset, search_end)
    if idx is None or idx < 0:
        return None
    # Verify we have enough bytes for the full PNG
    if idx + png_length > len(data):
        return None
    return idx


def generate_rowmap(
    parquet_path: Path,
    source: str,
    tier: str,
    release_tag: str,
    records: list[MaterialRecord],
) -> dict:
    """Generate a rowmap JSON from a Parquet file.

    For each binary column, scans a small window after the data page offset
    for the PNG magic bytes. The window is limited to avoid false positives
    from PNG magic appearing inside other columns' compressed data.
    """
    pf = pq.ParquetFile(parquet_path)
    fh = open(parquet_path, "rb")  # noqa: SIM115 — kept open for seeking
    meta = pf.metadata

    ok_records = [r for r in records if r.status == "ok"]
    parquet_name = parquet_path.name

    materials: dict[str, dict[str, dict[str, int]]] = {}

    for rg_idx in range(meta.num_row_groups):
        rg = meta.row_group(rg_idx)
        rec = ok_records[rg_idx]
        channels: dict[str, dict[str, int]] = {}

        for col_idx in range(rg.num_columns):
            col_meta = rg.column(col_idx)
            col_name = col_meta.path_in_schema

            if col_name not in CHANNEL_COLS:
                continue

            png_bytes = _read_png_bytes(rec.texture_paths.get(col_name))
            if png_bytes is None:
                continue

            # Read only a small window around the page offset (not the whole file)
            page_offset = col_meta.data_page_offset
            fh.seek(page_offset)
            window = fh.read(_MAX_PAGE_HEADER_SIZE)
            png_idx = window.find(PNG_MAGIC)
            png_start = (page_offset + png_idx) if png_idx >= 0 else None
            if png_start is None:
                log.warning(
                    "%s/%s: PNG magic not found within %d bytes of page offset %d",
                    rec.id,
                    col_name,
                    _MAX_PAGE_HEADER_SIZE,
                    page_offset,
                )
                continue

            channels[col_name] = {
                "offset": png_start,
                "length": len(png_bytes),
            }

        if channels:
            materials[rec.id] = channels

    rowmap = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": tier,
        "parquet_file": parquet_name,
        "materials": materials,
    }

    fh.close()

    log.info(
        "rowmap: %d materials, %d total channels",
        len(materials),
        sum(len(v) for v in materials.values()),
    )
    return rowmap


def write_rowmap(rowmap: dict, output_path: Path) -> Path:
    """Write rowmap JSON to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rowmap, indent=2) + "\n")
    log.info("wrote %s", output_path)
    return output_path
