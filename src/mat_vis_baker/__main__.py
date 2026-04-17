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
    from mat_vis_baker.parquet_writer import generate_rowmap, write_parquet, write_rowmap

    source = args.source
    tier = args.tier
    output_dir = Path(args.output_dir)
    resolution_px = TIER_TO_PX[tier]

    log.info("=== fetch %s %s ===", source, tier)
    fetch = _get_fetcher(source)
    if source == "physicallybased":
        records = fetch()  # scalar only, no tier/output_dir
    else:
        records = fetch(tier, output_dir / "textures", limit=args.limit)

    log.info("=== bake ===")
    records = bake_batch(records, output_dir / "baked", tier)

    ok = [r for r in records if r.status == "ok"]
    total = len(records)
    if total > 0 and len(ok) / total < 0.95:
        log.error(
            "success rate %.1f%% < 95%% threshold (%d/%d)", len(ok) / total * 100, len(ok), total
        )
        return 1

    log.info("=== pack ===")
    pq_path = output_dir / f"mat-vis-{source}-{tier}.parquet"
    write_parquet(records, source, tier, pq_path, resolution_px)

    rowmap = generate_rowmap(pq_path, source, tier, args.release_tag, records)
    write_rowmap(rowmap, output_dir / f"{source}-{tier}-rowmap.json")

    log.info("=== index ===")
    index_data = build_index(records, source)
    write_index(index_data, output_dir / f"{source}.json")

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
