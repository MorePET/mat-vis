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
    """Full pipeline: fetch+bake (parallel) → pack (parallel per category) → index."""
    import time
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor

    from mat_vis_baker.bake import bake_material
    from mat_vis_baker.common import hash_textures
    from mat_vis_baker.index_builder import build_index, write_index
    from mat_vis_baker.parquet_writer import (
        generate_rowmap,
        write_parquet,
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

    # ── phase 1: fetch (parallel downloads, done inside fetcher) ──
    log.info("=== phase 1: fetch %s %s ===", source, tier)
    t0 = time.monotonic()
    fetch = _get_fetcher(source)
    # offset+limit enables batched processing across parallel Dagger containers
    fetch_limit = (args.offset + args.limit) if args.limit else None
    records = fetch(tier, output_dir / "textures", limit=fetch_limit, mtlx_dir=mtlx_dir)
    if args.offset > 0:
        records = records[args.offset :]
    t_fetch = time.monotonic() - t0
    n_fetched = sum(1 for r in records if r.status == "ok")
    log.info(
        "PERF fetch: %.1fs, %d materials, %.1f mat/s",
        t_fetch,
        n_fetched,
        n_fetched / t_fetch if t_fetch > 0 else 0,
    )

    # ── phase 2: bake + hash + thumbnail (parallel per material) ──
    log.info("=== phase 2: bake + hash + thumbnail (8 workers) ===")
    t1 = time.monotonic()

    def _bake_one(rec):
        if rec.status == "ok":
            bake_material(rec, output_dir / "baked", thumb_dir, tier)
            if rec.status == "ok":
                hash_textures(rec)
        return rec

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(_bake_one, records))

    t_bake = time.monotonic() - t1
    ok = [r for r in records if r.status == "ok"]
    total = len(records)
    ok_pct = (len(ok) / total * 100) if total > 0 else 0
    log.info(
        "PERF bake: %.1fs, %d ok / %d total (%.1f%%), %.1f mat/s",
        t_bake,
        len(ok),
        total,
        ok_pct,
        len(ok) / t_bake if t_bake > 0 else 0,
    )

    if total > 0 and len(ok) / total < 0.95:
        log.error("success rate %.1f%% < 95%% threshold", ok_pct)
        return 1

    # ── phase 3: pack parquet + rowmap (parallel per category) ──
    log.info("=== phase 3: pack %d categories (4 workers) ===", len(set(r.category for r in ok)))
    t2 = time.monotonic()

    by_cat: dict[str, list] = defaultdict(list)
    for rec in ok:
        by_cat[rec.category].append(rec)

    def _pack_category(cat_and_records):
        cat, cat_records = cat_and_records
        pq_path = output_dir / f"mat-vis-{source}-{tier}-{cat}.parquet"
        write_parquet(cat_records, source, tier, pq_path, resolution_px)
        rowmap = generate_rowmap(pq_path, source, tier, args.release_tag, cat_records)
        rm_path = output_dir / f"{source}-{tier}-{cat}-rowmap.json"
        write_rowmap(rowmap, rm_path)
        return pq_path, pq_path.stat().st_size

    with ThreadPoolExecutor(max_workers=4) as pool:
        pack_results = list(pool.map(_pack_category, sorted(by_cat.items())))

    t_pack = time.monotonic() - t2
    total_bytes = sum(size for _, size in pack_results)
    log.info(
        "PERF pack: %.1fs, %d partitions, %.1f GB, %.1f MB/s",
        t_pack,
        len(pack_results),
        total_bytes / 1e9,
        total_bytes / 1e6 / t_pack if t_pack > 0 else 0,
    )

    # ── phase 4: index + manifest + catalog ──
    log.info("=== phase 4: index + manifest + catalog ===")
    t3 = time.monotonic()
    index_data = build_index(records, source)
    write_index(index_data, output_dir / f"{source}.json")

    log.info("=== manifest ===")
    from mat_vis_baker.manifest import generate_manifest, write_manifest

    manifest = generate_manifest(output_dir, args.release_tag, [source], [tier])
    write_manifest(manifest, output_dir / "release-manifest.json")

    log.info("=== catalog ===")
    from mat_vis_baker.catalog import generate_catalog, write_catalog

    catalog_md = generate_catalog(output_dir, output_dir / "mtlx")
    write_catalog(catalog_md, output_dir / "catalog.md")

    t_meta = time.monotonic() - t3
    t_total = time.monotonic() - t0

    log.info("=== PERFORMANCE SUMMARY ===")
    log.info(
        "  fetch:   %6.1fs  (%d materials, %.1f mat/s)",
        t_fetch,
        n_fetched,
        n_fetched / max(t_fetch, 0.1),
    )
    log.info("  bake:    %6.1fs  (%d ok, %.1f mat/s)", t_bake, len(ok), len(ok) / max(t_bake, 0.1))
    log.info(
        "  pack:    %6.1fs  (%d partitions, %.1f GB, %.1f MB/s)",
        t_pack,
        len(pack_results),
        total_bytes / 1e9,
        total_bytes / 1e6 / max(t_pack, 0.1),
    )
    log.info("  meta:    %6.1fs", t_meta)
    log.info("  TOTAL:   %6.1fs  (%d ok, %d failed)", t_total, len(ok), total - len(ok))
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

    args = parser.parse_args()

    if args.command == "all":
        return cmd_all(args)
    if args.command == "derive":
        return cmd_derive(args)
    if args.command == "derive-from-release":
        return cmd_derive_from_release(args)
    if args.command == "fetch":
        return cmd_fetch(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
