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
    """Write a Parquet file from MaterialRecords. Returns output path."""
    ok_records = [r for r in records if r.status == "ok"]
    if not ok_records:
        raise ValueError("No successful records to write")

    now = datetime.now(timezone.utc).isoformat()

    rows: dict[str, list] = {f.name: [] for f in _SCHEMA}
    for rec in ok_records:
        rows["id"].append(rec.id)
        rows["source"].append(source)
        rows["category"].append(rec.category)
        rows["resolution_px"].append(resolution_px)
        for ch in CANONICAL_CHANNELS:
            rows[ch].append(_read_png_bytes(rec.texture_paths.get(ch)))
        rows["source_url"].append(rec.source_url)
        rows["source_license"].append(rec.source_license)
        rows["baker_version"].append(BAKER_VERSION)
        rows["baked_at"].append(now)

    table = pa.table(rows, schema=_SCHEMA)

    compression = {col: "NONE" if col in CHANNEL_COLS else "ZSTD" for col in table.column_names}
    use_dictionary = {col: col not in CHANNEL_COLS for col in table.column_names}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        output_path,
        compression=compression,
        use_dictionary=use_dictionary,
        row_group_size=1,
        write_statistics=True,
    )

    log.info(
        "wrote %s (%d rows, %.1f MB)",
        output_path,
        len(ok_records),
        output_path.stat().st_size / 1e6,
    )
    return output_path


# ── rowmap generation ───────────────────────────────────────────


def _find_png_offset(data: bytes, start: int) -> int | None:
    """Find the first PNG magic bytes at or after `start`."""
    idx = data.find(PNG_MAGIC, start)
    return idx if idx >= 0 else None


def generate_rowmap(
    parquet_path: Path,
    source: str,
    tier: str,
    release_tag: str,
    records: list[MaterialRecord],
) -> dict:
    """Generate a rowmap JSON from a Parquet file.

    Uses the PNG magic byte scan approach: for each binary column in each
    row group, find the PNG start after the data page offset and use the
    known PNG byte length from the records.
    """
    pf = pq.ParquetFile(parquet_path)
    file_bytes = parquet_path.read_bytes()
    meta = pf.metadata

    ok_records = [r for r in records if r.status == "ok"]
    parquet_name = f"mat-vis-{source}-{tier}.parquet"

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

            page_offset = col_meta.data_page_offset
            png_start = _find_png_offset(file_bytes, page_offset)
            if png_start is None:
                log.warning(
                    "%s/%s: PNG magic not found after offset %d", rec.id, col_name, page_offset
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
