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
    """Full pipeline: fetch → bake → pack → index."""
    from mat_vis_baker.bake import bake_batch
    from mat_vis_baker.index_builder import build_index, write_index
    from mat_vis_baker.parquet_writer import (
        generate_rowmap,
        write_partitioned_parquet,
        write_rowmap,
    )

    source = args.source
    tier = args.tier
    output_dir = Path(args.output_dir)
    resolution_px = TIER_TO_PX[tier]

    mtlx_dir = output_dir / "mtlx"

    log.info("=== fetch %s %s ===", source, tier)
    fetch = _get_fetcher(source)
    if source == "physicallybased":
        records = fetch()  # scalar only, no tier/output_dir
    else:
        records = fetch(tier, output_dir / "textures", limit=args.limit, mtlx_dir=mtlx_dir)

    if source == "physicallybased":
        # Scalar only — no bake, no parquet, just index
        log.info("=== index (scalar only) ===")
        index_data = build_index(records, source)
        write_index(index_data, output_dir / f"{source}.json")
        log.info("=== done: %d records ===", len(records))
        return 0

    log.info("=== bake ===")
    thumb_dir = output_dir / "mtlx"
    records = bake_batch(records, output_dir / "baked", tier, thumb_dir=thumb_dir)

    ok = [r for r in records if r.status == "ok"]
    total = len(records)
    if total > 0 and len(ok) / total < 0.95:
        log.error(
            "success rate %.1f%% < 95%% threshold (%d/%d)", len(ok) / total * 100, len(ok), total
        )
        return 1

    log.info("=== pack (category-partitioned) ===")
    pq_paths = write_partitioned_parquet(records, source, tier, output_dir, resolution_px)

    # Generate one rowmap per partition
    from collections import defaultdict

    by_cat: dict[str, list] = defaultdict(list)
    for rec in ok:
        by_cat[rec.category].append(rec)

    for pq_path in pq_paths:
        # Extract category from filename: mat-vis-ambientcg-1k-metal.parquet → metal
        cat = pq_path.stem.rsplit("-", 1)[-1]
        cat_records = by_cat.get(cat, [])
        if cat_records:
            rowmap = generate_rowmap(pq_path, source, tier, args.release_tag, cat_records)
            write_rowmap(rowmap, output_dir / f"{source}-{tier}-{cat}-rowmap.json")

    log.info("=== index ===")
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

    log.info("=== done: %d ok, %d failed ===", len(ok), total - len(ok))
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
