"""CLI entry point for the mat-vis baker.

Usage:
    mat-vis-baker all <source> <tier> <output_dir> [--limit N] [--release-tag TAG]
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
    records = fetch(tier, output_dir / "textures", limit=args.limit, mtlx_dir=mtlx_dir)
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
    p_all.add_argument("--limit", type=int, default=None)
    p_all.add_argument("--release-tag", default="v0000.00.0")

    p_fetch = sub.add_parser("fetch", help="Fetch textures from upstream")
    p_fetch.add_argument("source", choices=SOURCES)
    p_fetch.add_argument("tier", choices=VALID_TIERS)
    p_fetch.add_argument("output_dir")
    p_fetch.add_argument("--limit", type=int, default=None)

    args = parser.parse_args()

    if args.command == "all":
        return cmd_all(args)
    if args.command == "fetch":
        return cmd_fetch(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
