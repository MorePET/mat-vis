"""Derive sub-1k tiers by reading from existing release parquets.

Streams one material at a time: fetch PNGs via HTTP range reads from the
source-tier parquet, resize with PIL, write to new parquet. Never holds
more than one material's textures in memory.
"""

from __future__ import annotations

import io
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from mat_vis_baker.common import (
    BAKER_VERSION,
    CANONICAL_CHANNELS,
    TIER_TO_PX,
    MaterialRecord,
)
from mat_vis_baker.parquet_writer import (
    CHANNEL_COLS,
    _SCHEMA,
    RowmapCollector,
    emit_rowmaps_for_bake,
)

log = logging.getLogger("mat-vis-baker.derive-from-release")


def _resize_png(png_bytes: bytes, target_px: int) -> bytes:
    """Resize a PNG (as raw bytes) to target_px square. Returns new PNG bytes."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        if img.width <= target_px and img.height <= target_px:
            return png_bytes  # already small enough
        resized = img.convert("RGB").copy()
        resized.thumbnail((target_px, target_px), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, "PNG")
        return buf.getvalue()


def _make_client(release_tag: str):
    """Create a MatVisClient, handling the dev-layout import path."""
    try:
        from mat_vis_client import MatVisClient
    except ImportError:
        client_path = str(Path(__file__).resolve().parents[2] / "clients" / "python" / "src")
        if client_path not in sys.path:
            sys.path.insert(0, client_path)
        from mat_vis_client import MatVisClient

    return MatVisClient(tag=release_tag)


def derive_from_release(
    source: str,
    target_tier: str,
    output_dir: Path,
    *,
    source_tier: str = "1k",
    release_tag: str = "v0000.00.0",
    limit: int | None = None,
    _client=None,
) -> int:
    """Derive a smaller tier from an existing release's parquets.

    Uses MatVisClient to discover materials and range-read PNGs from the
    source-tier parquet, resizes to target_tier, and streams into new
    parquet files (one row at a time, constant memory).

    Args:
        _client: Optional pre-built client (for testing). If None, creates one
                 from release_tag.

    Returns 0 on success, 1 on failure.
    """
    target_px = TIER_TO_PX[target_tier]
    source_px = TIER_TO_PX[source_tier]

    if target_px >= source_px:
        log.error(
            "target tier %s (%dpx) must be smaller than source tier %s (%dpx)",
            target_tier,
            target_px,
            source_tier,
            source_px,
        )
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "derive-from-release: %s %s→%s (release %s)",
        source,
        source_tier,
        target_tier,
        release_tag,
    )

    t0 = time.monotonic()

    # ── discover materials from release ──
    client = _client or _make_client(release_tag)
    material_ids = client.materials(source, source_tier)
    if limit:
        material_ids = material_ids[:limit]

    log.info("discovered %d materials in %s %s", len(material_ids), source, source_tier)

    # ── stream: fetch → resize → write, one material at a time ──
    # Group by category as we go (need index for category info)
    index_entries = client.index(source)
    index_by_id = {e["id"]: e for e in index_entries}

    # Accumulate materials per category, but process one at a time
    # We need category upfront to route into per-category parquets.
    # Strategy: two passes would break streaming. Instead, collect
    # (category, row_data) tuples then write per-category at the end.
    # BUT that holds all resized PNGs in memory.
    #
    # Better: buffer into per-category temp parquets streaming,
    # using one ParquetWriter per category opened lazily.

    now = datetime.now(timezone.utc).isoformat()
    compression = {
        col: "NONE" if col in CHANNEL_COLS else "ZSTD" for col in [f.name for f in _SCHEMA]
    }
    use_dictionary = {col: col not in CHANNEL_COLS for col in [f.name for f in _SCHEMA]}

    writers: dict[str, pq.ParquetWriter] = {}
    collectors: dict[str, RowmapCollector] = {}
    records_by_cat: dict[str, list[MaterialRecord]] = defaultdict(list)
    n_ok = 0
    n_fail = 0

    try:
        for i, mid in enumerate(material_ids):
            entry = index_by_id.get(mid, {})
            category = entry.get("category", "other")

            channels = client.channels(source, mid, source_tier)
            if not channels:
                log.warning("[%d/%d] %s: no channels, skipping", i + 1, len(material_ids), mid)
                n_fail += 1
                continue

            # Fetch and resize each channel
            row = {
                "id": [mid],
                "source": [source],
                "category": [category],
                "resolution_px": [target_px],
                "source_url": [entry.get("source_url", "")],
                "source_license": [entry.get("source_license", "CC0-1.0")],
                "baker_version": [BAKER_VERSION],
                "baked_at": [now],
            }
            row_channels = []
            channel_lengths: dict[str, int] = {}
            for ch in CANONICAL_CHANNELS:
                if ch in channels:
                    try:
                        png_bytes = client.fetch_texture(source, mid, ch, source_tier)
                        resized = _resize_png(png_bytes, target_px)
                        row[ch] = [resized]
                        row_channels.append(ch)
                        channel_lengths[ch] = len(resized)
                    except Exception:
                        log.warning("%s/%s: fetch/resize failed, nulling channel", mid, ch)
                        row[ch] = [None]
                else:
                    row[ch] = [None]

            if not row_channels:
                log.warning("[%d/%d] %s: all channels failed", i + 1, len(material_ids), mid)
                n_fail += 1
                del row
                continue

            # Write to per-category parquet (lazy open)
            if category not in writers:
                pq_path = output_dir / f"mat-vis-{source}-{target_tier}-{category}.parquet"
                writers[category] = pq.ParquetWriter(
                    pq_path,
                    _SCHEMA,
                    compression=compression,
                    use_dictionary=use_dictionary,
                )
                collectors[category] = RowmapCollector()

            collectors[category].record(mid, channel_lengths)

            table = pa.table(row, schema=_SCHEMA)
            writers[category].write_table(table)

            # Track record for rowmap generation
            records_by_cat[category].append(
                MaterialRecord(
                    id=mid,
                    source=source,
                    name=entry.get("name", mid),
                    category=category,
                    tags=entry.get("tags", []),
                    source_url=entry.get("source_url", ""),
                    source_license=entry.get("source_license", "CC0-1.0"),
                    last_updated=entry.get("last_updated", ""),
                    available_tiers=[target_tier],
                    maps=sorted(row_channels),
                )
            )

            n_ok += 1
            del row, table  # free PNG bytes immediately

            if (i + 1) % 50 == 0 or (i + 1) == len(material_ids):
                log.info(
                    "[%d/%d] %s ok, %d fail (%.1fs)",
                    i + 1,
                    len(material_ids),
                    n_ok,
                    n_fail,
                    time.monotonic() - t0,
                )
    finally:
        for w in writers.values():
            w.close()

    t_derive = time.monotonic() - t0
    log.info(
        "derive-from-release: %d ok, %d fail in %.1fs",
        n_ok,
        n_fail,
        t_derive,
    )

    # ── generate rowmaps (sidecar — authoritative, no magic-byte scan) ──
    # Iterate writers.keys(), not records_by_cat.keys() — same fix as
    # ktx2.py. See emit_rowmaps_for_bake for the reason.
    t1 = time.monotonic()
    parquet_paths = [
        output_dir / f"mat-vis-{source}-{target_tier}-{cat}.parquet"
        for cat in sorted(writers.keys())
    ]
    collectors_by_path = {
        output_dir / f"mat-vis-{source}-{target_tier}-{cat}.parquet": collectors.get(
            cat, RowmapCollector()
        )
        for cat in writers.keys()
    }
    emit_rowmaps_for_bake(
        parquet_paths,
        collectors_by_path,
        source=source,
        tier=target_tier,
        release_tag=release_tag,
        output_dir=output_dir,
    )

    # ── write index ──
    from mat_vis_baker.index_builder import build_index, write_index

    all_records = []
    for cat_records in records_by_cat.values():
        all_records.extend(cat_records)

    index_data = build_index(all_records, source)
    write_index(index_data, output_dir / f"{source}.json")

    t_total = time.monotonic() - t0
    log.info(
        "derive-from-release DONE: %d materials, %.1fs total (derive %.1fs, meta %.1fs)",
        n_ok,
        t_total,
        t_derive,
        time.monotonic() - t1,
    )
    return 0
