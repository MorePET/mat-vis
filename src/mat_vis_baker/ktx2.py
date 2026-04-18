"""Transcode PNG textures to KTX2 (Basis Universal) for GPU-native delivery.

Uses the Khronos `toktx` CLI (from KTX-Software) to compress PBR texture
channels with appropriate settings per channel type:

- color/emission: sRGB, Basis Universal (--bcmp)
- normal: tangent-space normal map mode (--bcmp --normal_mode)
- roughness/metalness/ao/displacement: linear transfer (--bcmp --assign_oetf linear)

The resulting KTX2 files are ~5x smaller than PNG and can be uploaded to the
GPU without a decode step.

Rowmaps for KTX2 parquets are generated via the sidecar mechanism in
``parquet_writer.build_rowmap_from_sidecar``: the exact byte length of each
encoded KTX2 payload is recorded at write time, so offset discovery matches
on the 12-byte KTX2 magic and confirms the known length fits in the column
chunk — no IEND scanning, no format-specific heuristics.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

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
    build_rowmap_from_sidecar,
    write_rowmap,
)

log = logging.getLogger("mat-vis-baker.ktx2")

# KTX2 file magic — first 12 bytes of every valid KTX2 file.
KTX2_MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"

# Channels that use sRGB transfer function (everything else is linear).
_SRGB_CHANNELS = frozenset({"color", "emission"})

# Channels that need tangent-space normal map optimization.
_NORMAL_CHANNELS = frozenset({"normal"})


def check_toktx() -> bool:
    """Check if ``toktx`` is available on PATH."""
    return shutil.which("toktx") is not None


def png_to_ktx2(png_bytes: bytes, channel: str) -> bytes:
    """Convert PNG bytes to KTX2 using the Khronos ``toktx`` CLI.

    Args:
        png_bytes: Raw PNG file content.
        channel: One of :data:`CANONICAL_CHANNELS` — determines compression
            flags (sRGB vs linear, normal-map mode).

    Returns:
        Raw KTX2 file bytes.

    Raises:
        FileNotFoundError: If ``toktx`` is not installed.
        RuntimeError: If ``toktx`` exits with a non-zero code.
    """
    if not check_toktx():
        raise FileNotFoundError(
            "toktx is not installed or not on PATH. "
            "Install KTX-Software from https://github.com/KhronosGroup/KTX-Software/releases "
            "or via your package manager (e.g. `brew install ktx-software`, "
            "`apt install ktx-tools`)."
        )

    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="mat-vis-ktx2-")
        input_path = Path(tmp_dir) / "input.png"
        output_path = Path(tmp_dir) / "output.ktx2"

        input_path.write_bytes(png_bytes)

        # Build toktx command
        # --assign_primaries bt709 — silence "No color primaries" warning
        # which becomes a fatal error in newer toktx versions
        cmd: list[str] = ["toktx", "--bcmp", "--assign_primaries", "bt709"]

        if channel in _NORMAL_CHANNELS:
            # --normal_mode requires linear input; assign + convert
            cmd.extend(["--normal_mode", "--assign_oetf", "linear"])
        elif channel in _SRGB_CHANNELS:
            cmd.extend(["--assign_oetf", "srgb"])
        else:
            # Linear channels: roughness, metalness, ao, displacement
            cmd.extend(["--assign_oetf", "linear"])

        cmd.extend([str(output_path), str(input_path)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"toktx failed (exit {result.returncode}) for channel '{channel}': "
                f"{result.stderr.strip()}"
            )

        ktx2_bytes = output_path.read_bytes()

        if not ktx2_bytes.startswith(KTX2_MAGIC):
            raise RuntimeError(
                f"toktx output does not start with KTX2 magic bytes for channel '{channel}'"
            )

        return ktx2_bytes

    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


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


def derive_ktx2_from_release(
    tag: str,
    source_tier: str,
    target_tier: str,
    output_dir: Path,
    sources: list[str] | None = None,
) -> list[Path]:
    """Derive KTX2-compressed parquets from an existing PNG release.

    Streams one material at a time: fetch PNGs via HTTP range reads from the
    source-tier parquet, transcode to KTX2, write to new parquet. Never holds
    more than one material's textures in memory.

    Args:
        tag: Release tag to read from (e.g. ``"v0000.00.0"``).
        source_tier: Tier to read PNGs from (e.g. ``"1k"``).
        target_tier: Target KTX2 tier name (e.g. ``"ktx2-1k"``).
        output_dir: Directory to write parquet and rowmap files into.
        sources: List of sources to process (e.g. ``["ambientcg"]``). If
            ``None``, discovers all sources from the release.

    Returns:
        List of parquet file paths created.
    """
    if not check_toktx():
        raise FileNotFoundError(
            "toktx is not installed. Cannot derive KTX2 tier. "
            "Install KTX-Software from https://github.com/KhronosGroup/KTX-Software/releases"
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    client = _make_client(tag)

    # Discover sources if not specified
    if sources is None:
        sources = client.sources()

    all_parquet_paths: list[Path] = []

    for source in sources:
        log.info(
            "derive-ktx2: source=%s %s→%s (release %s)",
            source,
            source_tier,
            target_tier,
            tag,
        )

        t0 = time.monotonic()

        material_ids = client.materials(source, source_tier)
        log.info("discovered %d materials in %s %s", len(material_ids), source, source_tier)

        if not material_ids:
            continue

        # Build index lookup for category and metadata
        index_entries = client.index(source)
        index_by_id = {e["id"]: e for e in index_entries}

        # Resolution: use source tier's pixel size (KTX2 keeps the same
        # resolution, just a different encoding).
        resolution_px = TIER_TO_PX.get(source_tier, 0)

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
                    log.warning(
                        "[%d/%d] %s: no channels, skipping",
                        i + 1,
                        len(material_ids),
                        mid,
                    )
                    n_fail += 1
                    continue

                # Build one row — fetch PNG, transcode to KTX2, write, free
                row: dict[str, list] = {
                    "id": [mid],
                    "source": [source],
                    "category": [category],
                    "resolution_px": [resolution_px],
                    "source_url": [entry.get("source_url", "")],
                    "source_license": [entry.get("source_license", "CC0-1.0")],
                    "baker_version": [BAKER_VERSION],
                    "baked_at": [now],
                }
                row_channels: list[str] = []
                channel_lengths: dict[str, int] = {}

                for ch in CANONICAL_CHANNELS:
                    if ch in channels:
                        try:
                            png_bytes = client.fetch_texture(source, mid, ch, source_tier)
                            ktx2_bytes = png_to_ktx2(png_bytes, ch)
                            row[ch] = [ktx2_bytes]
                            row_channels.append(ch)
                            channel_lengths[ch] = len(ktx2_bytes)
                        except Exception:
                            log.warning(
                                "%s/%s: fetch/transcode failed, nulling channel",
                                mid,
                                ch,
                                exc_info=True,
                            )
                            row[ch] = [None]
                    else:
                        row[ch] = [None]

                if not row_channels:
                    log.warning(
                        "[%d/%d] %s: all channels failed",
                        i + 1,
                        len(material_ids),
                        mid,
                    )
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
                del row, table  # free texture bytes immediately

                if (i + 1) % 50 == 0 or (i + 1) == len(material_ids):
                    log.info(
                        "[%d/%d] %d ok, %d fail (%.1fs)",
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
            "derive-ktx2: %d ok, %d fail in %.1fs",
            n_ok,
            n_fail,
            t_derive,
        )

        # Collect parquet paths
        parquet_paths = [
            output_dir / f"mat-vis-{source}-{target_tier}-{cat}.parquet"
            for cat in sorted(writers.keys())
        ]
        all_parquet_paths.extend(parquet_paths)

        # ── generate rowmaps (sidecar — KTX2-safe) ──
        # The sidecar collector recorded exact byte lengths for every KTX2
        # payload written. build_rowmap_from_sidecar locates the payload
        # start inside each column chunk by matching on the KTX2 magic and
        # confirming the known length fits — no IEND scanning required.
        for category in sorted(records_by_cat.keys()):
            pq_path = output_dir / f"mat-vis-{source}-{target_tier}-{category}.parquet"
            collector = collectors.get(category, RowmapCollector())
            rowmap = build_rowmap_from_sidecar(pq_path, collector, source, target_tier, tag)
            rm_path = output_dir / f"{source}-{target_tier}-{category}-rowmap.json"
            write_rowmap(rowmap, rm_path)

    return all_parquet_paths
