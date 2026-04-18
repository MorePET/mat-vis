"""CLI entry point for the mat-vis baker.

Usage:
    mat-vis-baker all <source> <tier> <output_dir> [--limit N] [--release-tag TAG]
    mat-vis-baker derive <source> <tier> <source_dir> <output_dir> [--release-tag TAG]
    mat-vis-baker derive-from-release <source> <tier> <output_dir> [--source-tier 1k] [--release-tag TAG] [--limit N]
    mat-vis-baker fetch <source> <tier> <output_dir> [--limit N]

Called directly in release.yml. Not a user-facing tool.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from mat_vis_baker.common import TIER_TO_PX, VALID_TIERS

log = logging.getLogger("mat-vis-baker")

SOURCES = ["ambientcg", "polyhaven", "gpuopen", "physicallybased"]


def _get_fetcher(source: str):
    if source == "ambientcg":
        from mat_vis_baker.sources.ambientcg import fetch

        return fetch
    if source == "polyhaven":
        from mat_vis_baker.sources.polyhaven import fetch

        return fetch
    if source == "gpuopen":
        from mat_vis_baker.sources.gpuopen import fetch

        return fetch
    if source == "physicallybased":
        from mat_vis_baker.sources.physicallybased import fetch

        return fetch
    raise NotImplementedError(f"Source {source!r} not yet implemented")


def cmd_all(args: argparse.Namespace) -> int:
    """Streaming pipeline: per-batch fetch → bake → append to lazy parquet writers → delete textures.

    Disk usage stays bounded by BATCH_SIZE × per-material size. Suitable for
    GH runners with 14GB disk even on 2k tier with thousands of materials.
    """
    import shutil
    import time
    from datetime import datetime, timezone

    import pyarrow as pa
    import pyarrow.parquet as pq

    from mat_vis_baker.bake import bake_material
    from mat_vis_baker.common import BAKER_VERSION, CANONICAL_CHANNELS, hash_textures
    from mat_vis_baker.index_builder import build_index, write_index
    from mat_vis_baker.parquet_writer import (
        CHANNEL_COLS,
        _SCHEMA,
        generate_rowmap_from_parquet,
        write_rowmap,
    )

    source = args.source
    tier = args.tier
    output_dir = Path(args.output_dir)
    resolution_px = TIER_TO_PX[tier]
    mtlx_dir = output_dir / "mtlx"
    thumb_dir = output_dir / "mtlx"

    # ── physicallybased: scalar only, no pipeline ──
    if source == "physicallybased":
        log.info("=== fetch physicallybased ===")
        fetch = _get_fetcher(source)
        records = fetch()
        log.info("=== index (scalar only) ===")
        index_data = build_index(records, source)
        write_index(index_data, output_dir / f"{source}.json")
        log.info("=== done: %d records ===", len(records))
        return 0

    # ── streaming bake: batched fetch + bake + pack + cleanup ──
    BATCH_SIZE = int(getattr(args, "batch_size", 50) or 50)
    fetch = _get_fetcher(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    textures_dir = output_dir / "textures"

    now = datetime.now(timezone.utc).isoformat()
    compression = {
        col: "NONE" if col in CHANNEL_COLS else "ZSTD" for col in [f.name for f in _SCHEMA]
    }
    use_dictionary = {col: col not in CHANNEL_COLS for col in [f.name for f in _SCHEMA]}

    writers: dict[str, pq.ParquetWriter] = {}
    all_records: list = []
    n_ok = 0
    n_failed = 0
    t0 = time.monotonic()
    offset = args.offset or 0
    user_limit = args.limit  # total cap if set
    fetched_so_far = 0

    log.info(
        "=== streaming bake: %s %s, batch=%d, offset=%d, limit=%s ===",
        source,
        tier,
        BATCH_SIZE,
        offset,
        user_limit if user_limit else "all",
    )

    try:
        while True:
            # Determine this batch's size
            batch_limit = BATCH_SIZE
            if user_limit and fetched_so_far + batch_limit > user_limit:
                batch_limit = user_limit - fetched_so_far
                if batch_limit <= 0:
                    break

            log.info("=== batch offset=%d limit=%d ===", offset, batch_limit)
            t_b = time.monotonic()

            # Fetch
            batch = fetch(tier, textures_dir, limit=batch_limit, offset=offset, mtlx_dir=mtlx_dir)
            if not batch:
                log.info("no more materials, done")
                break

            # Bake (resize in place + hash)
            for rec in batch:
                if rec.status == "ok":
                    bake_material(rec, output_dir / "baked", thumb_dir, tier)
                    if rec.status == "ok":
                        hash_textures(rec)

            # Pack: append each ok record to its category writer (lazy open)
            for rec in batch:
                if rec.status != "ok":
                    n_failed += 1
                    continue

                cat = rec.category
                if cat not in writers:
                    pq_path = output_dir / f"mat-vis-{source}-{tier}-{cat}.parquet"
                    writers[cat] = pq.ParquetWriter(
                        pq_path, _SCHEMA, compression=compression, use_dictionary=use_dictionary
                    )

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
                    path = rec.texture_paths.get(ch)
                    row[ch] = [path.read_bytes() if path and path.exists() else None]

                table = pa.table(row, schema=_SCHEMA)
                writers[cat].write_table(table)
                del row, table
                n_ok += 1

            all_records.extend(batch)
            fetched_so_far += len(batch)

            # CLEAR cache: delete this batch's textures + baked dirs
            if textures_dir.exists():
                shutil.rmtree(textures_dir, ignore_errors=True)
            baked_dir = output_dir / "baked"
            if baked_dir.exists():
                shutil.rmtree(baked_dir, ignore_errors=True)

            # Disk usage check
            try:
                disk = shutil.disk_usage(str(output_dir))
                free_gb = disk.free / 1e9
            except Exception:
                free_gb = -1
            log.info(
                "batch done: %d records (%.1fs), totals: %d ok %d fail, free disk: %.1f GB",
                len(batch),
                time.monotonic() - t_b,
                n_ok,
                n_failed,
                free_gb,
            )

            offset += len(batch)
            # If batch came back smaller than requested, we're at the end
            if len(batch) < batch_limit:
                log.info("partial batch (%d < %d), done", len(batch), batch_limit)
                break
    finally:
        for w in writers.values():
            w.close()

    t_stream = time.monotonic() - t0
    log.info(
        "PERF stream: %.1fs, %d ok / %d total (%.1f mat/s)",
        t_stream,
        n_ok,
        n_ok + n_failed,
        n_ok / max(t_stream, 0.1),
    )

    if n_ok == 0:
        log.error("no successful materials")
        return 1

    # ── generate rowmaps from each parquet (sources deleted, scan parquet) ──
    log.info("=== rowmap generation ===")
    t_rm = time.monotonic()
    total_bytes = 0
    for cat in sorted(writers.keys()):
        pq_path = output_dir / f"mat-vis-{source}-{tier}-{cat}.parquet"
        rowmap = generate_rowmap_from_parquet(pq_path, source, tier, args.release_tag)
        rm_path = output_dir / f"{source}-{tier}-{cat}-rowmap.json"
        write_rowmap(rowmap, rm_path)
        total_bytes += pq_path.stat().st_size
    log.info(
        "PERF rowmap: %.1fs, %d partitions, %.1f GB",
        time.monotonic() - t_rm,
        len(writers),
        total_bytes / 1e9,
    )

    # ── index + manifest + catalog ──
    log.info("=== index + manifest + catalog ===")
    index_data = build_index(all_records, source)
    write_index(index_data, output_dir / f"{source}.json")

    from mat_vis_baker.manifest import generate_manifest, write_manifest

    manifest = generate_manifest(output_dir, args.release_tag, [source], [tier])
    write_manifest(manifest, output_dir / "release-manifest.json")

    from mat_vis_baker.catalog import generate_catalog, write_catalog

    catalog_md = generate_catalog(output_dir, output_dir / "mtlx")
    write_catalog(catalog_md, output_dir / "catalog.md")

    t_total = time.monotonic() - t0
    log.info("=== PERFORMANCE SUMMARY ===")
    log.info("  stream:  %6.1fs  (%d ok, %d failed)", t_stream, n_ok, n_failed)
    log.info("  total:   %6.1fs  (%.1f GB output)", t_total, total_bytes / 1e9)
    return 0


def cmd_derive(args: argparse.Namespace) -> int:
    """Derive a smaller tier from existing bake output — resize, repack, no download."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from mat_vis_baker.bake import _validate_and_resize_png
    from mat_vis_baker.common import TIER_TO_PX, hash_textures
    from mat_vis_baker.index_builder import build_index, write_index
    from mat_vis_baker.parquet_writer import generate_rowmap, write_parquet, write_rowmap

    source_dir = Path(args.source_dir)
    target_tier = args.tier
    target_px = TIER_TO_PX[target_tier]
    output_dir = Path(args.output_dir)
    source = args.source

    # Find existing texture files from a previous bake
    tex_dir = source_dir / "textures"
    if not tex_dir.exists():
        log.error("No textures dir at %s — run 'all' first", tex_dir)
        return 1

    import json

    index_path = source_dir / f"{source}.json"
    if not index_path.exists():
        log.error("No index at %s — run 'all' first", index_path)
        return 1

    index_data = json.loads(index_path.read_text())
    ok_entries = [e for e in index_data if e.get("status") != "failed"]
    log.info("deriving %s from %d materials at %dpx", target_tier, len(ok_entries), target_px)

    t0 = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_tex = output_dir / "textures"

    from mat_vis_baker.common import MaterialRecord

    records = []

    def _derive_one(entry):
        mid = entry["id"]
        src_mat = tex_dir / mid
        if not src_mat.exists():
            return None
        dst_mat = out_tex / mid
        dst_mat.mkdir(parents=True, exist_ok=True)
        paths = {}
        for ch in entry.get("maps", []):
            src_png = src_mat / f"{ch}.png"
            if not src_png.exists():
                continue
            dst_png = dst_mat / f"{ch}.png"
            import shutil

            shutil.copy2(src_png, dst_png)
            _validate_and_resize_png(dst_png, target_px)
            paths[ch] = dst_png

        if not paths:
            return None

        rec = MaterialRecord(
            id=mid,
            source=source,
            name=entry.get("name", mid),
            category=entry.get("category", "other"),
            tags=entry.get("tags", []),
            source_url=entry.get("source_url", ""),
            source_license=entry.get("source_license", "CC0-1.0"),
            last_updated=entry.get("last_updated", ""),
            available_tiers=[target_tier],
            maps=sorted(paths.keys()),
            texture_paths=paths,
        )
        hash_textures(rec)
        return rec

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_derive_one, ok_entries))

    records = [r for r in results if r is not None]
    t_derive = time.monotonic() - t0
    log.info("PERF derive: %.1fs, %d materials at %dpx", t_derive, len(records), target_px)

    # Pack
    from collections import defaultdict as _dd

    by_cat: dict[str, list] = _dd(list)
    for rec in records:
        by_cat[rec.category].append(rec)

    t1 = time.monotonic()
    for cat, cat_records in sorted(by_cat.items()):
        pq_path = output_dir / f"mat-vis-{source}-{target_tier}-{cat}.parquet"
        write_parquet(cat_records, source, target_tier, pq_path, target_px)
        rowmap = generate_rowmap(pq_path, source, target_tier, args.release_tag, cat_records)
        write_rowmap(rowmap, output_dir / f"{source}-{target_tier}-{cat}-rowmap.json")

    t_pack = time.monotonic() - t1

    # Index
    index_out = build_index(records, source)
    write_index(index_out, output_dir / f"{source}.json")

    log.info(
        "PERF derive total: %.1fs (derive %.1fs + pack %.1fs), %d materials",
        time.monotonic() - t0,
        t_derive,
        t_pack,
        len(records),
    )
    return 0


def cmd_derive_from_release(args: argparse.Namespace) -> int:
    """Derive a smaller tier from an existing release's parquets (no download from upstream)."""
    from mat_vis_baker.derive_from_release import derive_from_release

    return derive_from_release(
        source=args.source,
        target_tier=args.tier,
        output_dir=Path(args.output_dir),
        source_tier=args.source_tier,
        release_tag=args.release_tag,
        limit=args.limit,
    )


def cmd_catalog(args: argparse.Namespace) -> int:
    """Generate catalog + thumbnails from published release."""
    from mat_vis_baker.catalog_from_release import (
        fetch_thumbnails_from_release,
        generate_catalog_from_release,
    )

    output_dir = Path(args.output_dir)
    thumb_dir = output_dir / "mtlx"
    index_dir = output_dir / "index"

    if not args.skip_thumbnails:
        log.info("=== fetching thumbnails from release ===")
        count = fetch_thumbnails_from_release(args.release_tag, thumb_dir)
        log.info("saved %d thumbnails", count)

    log.info("=== generating catalog ===")
    md = generate_catalog_from_release(
        args.release_tag,
        thumb_dir,
        index_dir if index_dir.exists() else None,
    )
    catalog_path = output_dir / "docs" / "catalog.md"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(md)
    log.info("wrote %s", catalog_path)
    return 0


def cmd_derive_ktx2(args: argparse.Namespace) -> int:
    """Derive KTX2 tier from existing release PNGs."""
    from mat_vis_baker.ktx2 import derive_ktx2_from_release

    output_dir = Path(args.output_dir)
    source_tier = args.source_tier
    target_tier = args.target_tier or f"ktx2-{source_tier}"
    sources = [args.source] if args.source else None

    paths = derive_ktx2_from_release(
        tag=args.release_tag,
        source_tier=source_tier,
        target_tier=target_tier,
        output_dir=output_dir,
        sources=sources,
    )
    log.info("wrote %d KTX2 parquet files", len(paths))
    return 0


def cmd_pack_mtlx(args: argparse.Namespace) -> int:
    """Pack original upstream MaterialX files into a JSON map."""
    from mat_vis_baker.mtlx_tier import pack_original_mtlx_json

    output_dir = Path(args.output_dir)
    source = args.source or "gpuopen"

    path = pack_original_mtlx_json(
        mtlx_dir=Path(args.mtlx_dir),
        source=source,
        output_dir=output_dir,
    )
    log.info("wrote %s", path)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch only — download textures from upstream."""
    fetch = _get_fetcher(args.source)
    records = fetch(args.tier, Path(args.output_dir), limit=args.limit)
    ok = sum(1 for r in records if r.status == "ok")
    log.info("fetch done: %d ok, %d failed", ok, len(records) - ok)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="mat-vis-baker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_all = sub.add_parser("all", help="Full pipeline: fetch → bake → pack → index")
    p_all.add_argument("source", choices=SOURCES)
    p_all.add_argument("tier", choices=VALID_TIERS)
    p_all.add_argument("output_dir")
    p_all.add_argument("--offset", type=int, default=0, help="Skip first N materials")
    p_all.add_argument("--limit", type=int, default=None)
    p_all.add_argument("--release-tag", default="v0000.00.0")

    p_derive = sub.add_parser("derive", help="Derive smaller tier from existing bake output")
    p_derive.add_argument("source", choices=SOURCES)
    p_derive.add_argument("tier", choices=VALID_TIERS)
    p_derive.add_argument(
        "source_dir", help="Directory with existing bake output (textures + index)"
    )
    p_derive.add_argument("output_dir")
    p_derive.add_argument("--release-tag", default="v0000.00.0")

    p_dfr = sub.add_parser(
        "derive-from-release",
        help="Derive smaller tier from existing release parquets (no upstream download)",
    )
    p_dfr.add_argument("source", choices=SOURCES)
    p_dfr.add_argument("tier", choices=VALID_TIERS)
    p_dfr.add_argument("output_dir")
    p_dfr.add_argument(
        "--source-tier", default="1k", choices=VALID_TIERS, help="Tier to read from (default: 1k)"
    )
    p_dfr.add_argument("--release-tag", default="v0000.00.0")
    p_dfr.add_argument("--limit", type=int, default=None, help="Process only first N materials")

    p_fetch = sub.add_parser("fetch", help="Fetch textures from upstream")
    p_fetch.add_argument("source", choices=SOURCES)
    p_fetch.add_argument("tier", choices=VALID_TIERS)
    p_fetch.add_argument("output_dir")
    p_fetch.add_argument("--limit", type=int, default=None)

    p_cat = sub.add_parser("catalog", help="Generate catalog + thumbnails from release")
    p_cat.add_argument("release_tag", help="Release tag (e.g. v2026.04.0)")
    p_cat.add_argument(
        "--output-dir", default=".", help="Repo root (writes docs/catalog.md + mtlx/)"
    )
    p_cat.add_argument("--skip-thumbnails", action="store_true", help="Skip thumbnail download")

    p_ktx2 = sub.add_parser(
        "derive-ktx2",
        help="Derive KTX2-compressed tier from existing release PNGs",
    )
    p_ktx2.add_argument("output_dir")
    p_ktx2.add_argument("--release-tag", default="v2026.04.0")
    p_ktx2.add_argument(
        "--source-tier", default="1k", help="PNG tier to transcode from (default: 1k)"
    )
    p_ktx2.add_argument(
        "--target-tier", default=None, help="KTX2 tier name (default: ktx2-{source-tier})"
    )
    p_ktx2.add_argument("--source", default=None, help="Restrict to one source")

    p_mtlx = sub.add_parser(
        "pack-mtlx",
        help="Pack original upstream .mtlx files into JSON map for release",
    )
    p_mtlx.add_argument("output_dir")
    p_mtlx.add_argument("--source", default=None, help="Source (default: gpuopen)")
    p_mtlx.add_argument("--mtlx-dir", default="mtlx", help="Directory with upstream .mtlx files")

    args = parser.parse_args()

    if args.command == "all":
        return cmd_all(args)
    if args.command == "derive":
        return cmd_derive(args)
    if args.command == "derive-from-release":
        return cmd_derive_from_release(args)
    if args.command == "fetch":
        return cmd_fetch(args)
    if args.command == "catalog":
        return cmd_catalog(args)
    if args.command == "derive-ktx2":
        return cmd_derive_ktx2(args)
    if args.command == "pack-mtlx":
        return cmd_pack_mtlx(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
