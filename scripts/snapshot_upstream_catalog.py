"""Snapshot upstream catalogs into ``upstream-catalog.json``.

Called at release prep time to capture what each upstream source
*currently advertises*. The validator's catalog-contract gate
(#88 Phase 2) uses this as the truth baseline — bakes must match
``upstream \\ WAIVED``.

Per-release JSON is committed as a release asset and in
``metrics/upstream-catalog-{tag}.json`` so diffs across releases
surface upstream additions and removals (``diff catalog_N catalog_{N-1}``).

Usage::

    python -m scripts.snapshot_upstream_catalog \\
        --release-tag v2026.04.1 \\
        --out metrics/upstream-catalog-v2026.04.1.json \\
        [--sources ambientcg polyhaven gpuopen]

Network: this script hits live upstream APIs. For offline/testing,
inject IDs via ``_ids_for_source`` (mockable — see tests).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


__all__ = [
    "build_catalog",
    "snapshot",
]


def _ids_for_source(source: str) -> list[str]:
    """Call the source adapter's discover() and extract material IDs.

    This is the one network-hitting hook. Mocked in tests so the rest
    of the snapshot logic (JSON shape, sorting, aggregation) is unit
    testable without live APIs.
    """
    # Deferred imports: adapters pull in requests etc. which shouldn't
    # be loaded for offline tests that mock this function.
    if source == "ambientcg":
        from mat_vis_baker.sources.ambientcg import discover

        return [e["assetId"] for e in discover()]
    if source == "polyhaven":
        from mat_vis_baker.sources.polyhaven import discover

        # polyhaven.discover() returns {slug: meta, ...}
        return list(discover().keys())
    if source == "gpuopen":
        from mat_vis_baker.sources.gpuopen import discover

        return [e.get("id") or e.get("name") for e in discover() if e]
    if source == "physicallybased":
        from mat_vis_baker.sources.physicallybased import fetch

        return [r.id for r in fetch()]
    raise ValueError(f"unknown source: {source!r}")


def build_catalog(
    *,
    release_tag: str,
    sources: dict[str, list[str]],
    snapshotted_at: str | None = None,
) -> dict:
    """Build the catalog dict from a mapping of ``source -> [ids]``.

    Pure function — no IO. IDs are sorted for deterministic output so
    ``diff`` between releases produces meaningful line-by-line deltas.
    """
    if snapshotted_at is None:
        snapshotted_at = datetime.now(timezone.utc).isoformat()
    return {
        "release_tag": release_tag,
        "snapshotted_at": snapshotted_at,
        "sources": {
            name: {"count": len(ids), "ids": sorted(set(ids))} for name, ids in sources.items()
        },
    }


def snapshot(
    *,
    output_path: Path,
    release_tag: str,
    sources: list[str],
) -> dict:
    """Fetch each source's ID list, build the catalog, write to ``output_path``.

    Returns the catalog dict so callers can chain (e.g. to compute stats
    in the same workflow step).
    """
    ids_by_source = {name: _ids_for_source(name) for name in sources}
    catalog = build_catalog(release_tag=release_tag, sources=ids_by_source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, indent=2) + "\n")
    return catalog


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="snapshot-upstream-catalog")
    p.add_argument("--release-tag", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument(
        "--sources",
        nargs="+",
        default=["ambientcg", "polyhaven", "gpuopen"],
        help="sources to snapshot (default: all three)",
    )
    args = p.parse_args(argv)
    cat = snapshot(
        output_path=args.out,
        release_tag=args.release_tag,
        sources=args.sources,
    )
    total = sum(s["count"] for s in cat["sources"].values())
    print(f"snapshot {args.release_tag}: {total} materials across {len(cat['sources'])} sources")
    for name, s in sorted(cat["sources"].items()):
        print(f"  {name}: {s['count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
