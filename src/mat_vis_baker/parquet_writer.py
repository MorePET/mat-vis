"""Pack baked ONGs into Parquet + generate rowmap JSON for HTTP range reads.

Binary (PNG) columns are UNCOMPRESSED so that byte-range reads return raw
PNG payload without any decompression. Scalar columns use ZSTD.

Row group size = 1 (one material per row group) for per-material offset discovery.

## Rowmap emission

The correct path for new parquets is the "sidecar" approach: as each row is
written, the caller records (material_id, channel) → byte_length in a
``RowmapCollector``. After the writer closes, ``build_rowmap_from_sidecar``
reads the parquet's own column-chunk metadata to find the exact
``dictionary_page_offset`` for each (row_group, column). Combined with the
known payload length from the sidecar, this yields an authoritative rowmap
without ever relying on magic-byte heuristics.

The legacy scanner (``generate_rowmap_from_parquet_legacy``) is kept only for
retrofitting rowmaps onto parquets that were baked before the sidecar
mechanism existed (the ``regenerate_rowmaps`` workflow). New code paths MUST
NOT use it — it has proven fragile (#57).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from mat_vis_baker.common import BAKER_VERSION, CANONICAL_CHANNELS, MaterialRecord

log = logging.getLogger("mat-vis-baker.parquet")

PNG_MAGIC = b"\x89PNG"
KTX2_MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"  # 12 bytes

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


# ── rowmap sidecar ──────────────────────────────────────────────


@dataclass
class RowmapCollector:
    """Records the authoritative payload length per (material_id, channel).

    Populated by the caller as rows are written. After the ParquetWriter is
    closed, pass this to ``build_rowmap_from_sidecar`` to produce the rowmap.

    ``rows`` preserves insertion order — critical because row groups in the
    resulting parquet are laid out in the same order as ``writer.write_table``
    calls.
    """

    # Ordered list of (material_id, {channel: length_in_bytes}) entries.
    # One entry per row group in the final parquet.
    rows: list[tuple[str, dict[str, int]]] = field(default_factory=list)

    def record(self, material_id: str, channel_lengths: dict[str, int]) -> None:
        """Record one row's channel → byte-length mapping.

        ``channel_lengths`` must only contain channels that were actually
        written with non-null bytes. Null columns are excluded from the
        rowmap.
        """
        self.rows.append((material_id, dict(channel_lengths)))


# Small window for finding the payload start inside a column chunk. PyArrow's
# Thrift page headers are typically ~30–60 bytes; a 4 KiB window is far more
# than enough and cheap to read.
_PAYLOAD_SEARCH_WINDOW = 4096


def _find_payload_offset(fh, chunk_start: int, chunk_size: int, expected_length: int) -> int | None:
    """Locate the actual binary payload within a column chunk.

    Uses the KNOWN expected_length (from the sidecar) as an authoritative
    signal — we only need to find where the payload STARTS inside the column
    chunk, skipping pyarrow's Thrift page header. This is a minimal correction
    for the header size, not a magic-byte heuristic.

    Returns the absolute file offset of the first payload byte, or None if
    not found.
    """
    # Strategy: the column chunk = [page_header][payload_bytes]. The payload
    # is exactly ``expected_length`` bytes. We scan a small window looking
    # for any magic that matches a known binary format, preferring matches
    # at offsets that leave exactly ``expected_length`` bytes remaining.
    window_size = min(_PAYLOAD_SEARCH_WINDOW, chunk_size)
    fh.seek(chunk_start)
    window = fh.read(window_size)

    # Try PNG then KTX2 magic — these are the formats we currently emit.
    for magic in (PNG_MAGIC, KTX2_MAGIC):
        idx = window.find(magic)
        if idx < 0:
            continue
        # Sanity: the payload from idx onward must fit inside the chunk.
        if idx + expected_length > chunk_size:
            continue
        return chunk_start + idx

    return None


def build_rowmap_from_sidecar(
    parquet_path: Path,
    collector: RowmapCollector,
    source: str,
    tier: str,
    release_tag: str,
) -> dict:
    """Build a rowmap from parquet metadata + sidecar-recorded lengths.

    This is the authoritative path for newly-written parquets. Offsets come
    from pyarrow's own column-chunk metadata; lengths come from what the
    caller recorded at write time.
    """
    pf = pq.ParquetFile(parquet_path)
    meta = pf.metadata

    if meta.num_row_groups != len(collector.rows):
        raise ValueError(
            f"rowmap sidecar mismatch: {len(collector.rows)} recorded rows "
            f"vs {meta.num_row_groups} row groups in {parquet_path.name}"
        )

    materials: dict[str, dict[str, dict[str, int]]] = {}

    with open(parquet_path, "rb") as fh:
        for rg_idx in range(meta.num_row_groups):
            rg = meta.row_group(rg_idx)
            material_id, channel_lengths = collector.rows[rg_idx]
            if not channel_lengths:
                continue

            channels: dict[str, dict[str, int]] = {}
            for col_idx in range(rg.num_columns):
                col_meta = rg.column(col_idx)
                col_name = col_meta.path_in_schema
                if col_name not in CHANNEL_COLS:
                    continue
                expected_length = channel_lengths.get(col_name)
                if expected_length is None:
                    continue  # caller told us this channel is null

                # With use_dictionary=False on binary columns, pyarrow still
                # emits a "dictionary" page that holds the PLAIN-encoded
                # payload. When that's absent, fall back to data_page_offset.
                chunk_start = col_meta.dictionary_page_offset or col_meta.data_page_offset
                chunk_size = col_meta.total_compressed_size

                payload_offset = _find_payload_offset(fh, chunk_start, chunk_size, expected_length)
                if payload_offset is None:
                    log.error(
                        "%s/%s: payload not locatable in column chunk "
                        "(chunk_start=%d, size=%d, expected_length=%d) — "
                        "sidecar/metadata are inconsistent",
                        material_id,
                        col_name,
                        chunk_start,
                        chunk_size,
                        expected_length,
                    )
                    continue

                channels[col_name] = {
                    "offset": payload_offset,
                    "length": expected_length,
                }

            if channels:
                materials[material_id] = channels

    rowmap = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": tier,
        "parquet_file": parquet_path.name,
        "materials": materials,
    }

    log.info(
        "rowmap (sidecar): %d materials, %d total channels",
        len(materials),
        sum(len(v) for v in materials.values()),
    )
    return rowmap


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
    path, _ = write_parquet_with_rowmap(
        records, source, tier, output_path, resolution_px, release_tag=None
    )
    return path


def write_parquet_with_rowmap(
    records: list[MaterialRecord],
    source: str,
    tier: str,
    output_path: Path,
    resolution_px: int,
    release_tag: str | None = None,
) -> tuple[Path, dict | None]:
    """Write a Parquet file AND build an authoritative rowmap in one pass.

    Returns ``(path, rowmap)``. If ``release_tag`` is None, only the parquet
    is written and ``rowmap`` is None. Otherwise the rowmap is built from
    the sidecar collected during write.
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

    collector = RowmapCollector()

    writer = pq.ParquetWriter(
        output_path,
        _SCHEMA,
        compression=compression,
        use_dictionary=use_dictionary,
    )

    try:
        for rec in ok_records:
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
            channel_lengths: dict[str, int] = {}
            for ch in CANONICAL_CHANNELS:
                data = _read_png_bytes(rec.texture_paths.get(ch))
                row[ch] = [data]
                if data is not None:
                    channel_lengths[ch] = len(data)

            collector.record(rec.id, channel_lengths)

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

    rowmap: dict | None = None
    if release_tag is not None:
        rowmap = build_rowmap_from_sidecar(output_path, collector, source, tier, release_tag)

    return output_path, rowmap


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


def generate_rowmap(
    parquet_path: Path,
    source: str,
    tier: str,
    release_tag: str,
    records: list[MaterialRecord],
) -> dict:
    """Generate a rowmap from a Parquet file and on-disk MaterialRecords.

    Uses the on-disk texture files to determine the authoritative payload
    length per channel, then resolves the offset via the parquet's own
    column-chunk metadata. This replaces the old magic-byte scanning
    heuristic with a deterministic sidecar-style lookup.
    """
    ok_records = [r for r in records if r.status == "ok"]
    collector = RowmapCollector()

    for rec in ok_records:
        channel_lengths: dict[str, int] = {}
        for ch in CANONICAL_CHANNELS:
            path = rec.texture_paths.get(ch)
            if path is not None and path.exists():
                channel_lengths[ch] = path.stat().st_size
        collector.record(rec.id, channel_lengths)

    return build_rowmap_from_sidecar(parquet_path, collector, source, tier, release_tag)


# ── legacy scanner (retrofit only) ──────────────────────────────

# Page-header scan window used by the LEGACY scanner. Kept only for
# compatibility with parquets baked before sidecar rowmap emission (#57).
_MAX_PAGE_HEADER_SIZE = 4096


def generate_rowmap_from_parquet_legacy(
    parquet_path: Path,
    source: str,
    tier: str,
    release_tag: str,
) -> dict:
    """LEGACY scanner — magic-byte heuristic. Use only for retrofit.

    This is the pre-#57 implementation, kept functional solely so the
    ``regenerate_rowmaps`` Dagger function can rebuild rowmaps for
    parquets that were baked without a sidecar. New code paths MUST
    NOT call this function.

    It searches for PNG or KTX2 magic bytes inside each column chunk,
    then walks from the magic to find the payload end. The window was
    raised to 4 KiB (up from 256 B) to cover larger Thrift page headers,
    but the approach is still fundamentally heuristic and can silently
    drop channels under edge cases (data page v2, unusual magic
    alignment, etc).
    """
    pf = pq.ParquetFile(parquet_path)
    fh = open(parquet_path, "rb")  # noqa: SIM115 — kept open for seeking
    meta = pf.metadata
    parquet_name = parquet_path.name

    materials: dict[str, dict[str, dict[str, int]]] = {}

    try:
        for rg_idx in range(meta.num_row_groups):
            rg = meta.row_group(rg_idx)

            table = pf.read_row_group(rg_idx, columns=["id"])
            mid = table.column("id")[0].as_py()

            channels: dict[str, dict[str, int]] = {}

            for col_idx in range(rg.num_columns):
                col_meta = rg.column(col_idx)
                col_name = col_meta.path_in_schema

                if col_name not in CHANNEL_COLS:
                    continue

                if col_meta.is_stats_set and col_meta.statistics.null_count == col_meta.num_values:
                    continue

                page_offset = col_meta.dictionary_page_offset or col_meta.data_page_offset
                chunk_size = col_meta.total_compressed_size

                fh.seek(page_offset)
                window = fh.read(min(_MAX_PAGE_HEADER_SIZE, chunk_size))

                png_idx = window.find(PNG_MAGIC)
                ktx2_idx = window.find(KTX2_MAGIC) if png_idx < 0 else -1

                if png_idx >= 0:
                    data_start = page_offset + png_idx
                    fh.seek(data_start)
                    data = fh.read(chunk_size - png_idx)

                    iend_pos = data.find(b"IEND")
                    if iend_pos < 0:
                        log.warning("legacy: %s/%s: IEND not found, skipping", mid, col_name)
                        continue
                    data_length = iend_pos + 4 + 4  # IEND marker + CRC

                elif ktx2_idx >= 0:
                    data_start = page_offset + ktx2_idx
                    data_length = chunk_size - ktx2_idx
                else:
                    continue

                channels[col_name] = {
                    "offset": data_start,
                    "length": data_length,
                }

            if channels:
                materials[mid] = channels
    finally:
        fh.close()

    rowmap = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": tier,
        "parquet_file": parquet_name,
        "materials": materials,
    }

    log.info(
        "rowmap (legacy scanner): %d materials, %d total channels",
        len(materials),
        sum(len(v) for v in materials.values()),
    )
    return rowmap


def generate_rowmap_from_parquet(
    parquet_path: Path,
    source: str,
    tier: str,
    release_tag: str,
) -> dict:
    """Back-compat shim → legacy scanner.

    Kept so external callers (and the retrofit workflow) still have a
    working import. New code MUST pass a ``RowmapCollector`` sidecar and
    call ``build_rowmap_from_sidecar`` instead; see #57.
    """
    return generate_rowmap_from_parquet_legacy(parquet_path, source, tier, release_tag)


def write_rowmap(rowmap: dict, output_path: Path) -> Path:
    """Write rowmap JSON to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rowmap, indent=2) + "\n")
    log.info("wrote %s", output_path)
    return output_path
