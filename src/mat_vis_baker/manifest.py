"""Generate release-manifest.json for client auto-discovery.

Two modes:
1. From bake output dir (single source) — used during bake
2. From release assets (all sources) — used to rebuild after all bakes complete

Schema: docs/specs/release-manifest-schema.json
See issue #22 for context.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("mat-vis-baker.manifest")

GITHUB_BASE = "https://github.com/MorePET/mat-vis/releases/download"


def generate_manifest(
    output_dir: Path,
    release_tag: str,
    sources: list[str],
    tiers: list[str],
) -> dict:
    """Build a release manifest from the bake output directory."""
    # schema_version is required by client >= 0.3.0
    manifest: dict = {
        "schema_version": 1,
        "release_tag": release_tag,
        "tiers": {},
    }

    base_url = f"{GITHUB_BASE}/{release_tag}/"

    for tier in tiers:
        tier_entry: dict = {
            "base_url": base_url,
            "sources": {},
        }

        for source in sources:
            pattern = f"mat-vis-{source}-{tier}-*.parquet"
            pq_files = sorted(output_dir.glob(pattern))
            if not pq_files:
                single = output_dir / f"mat-vis-{source}-{tier}.parquet"
                if single.exists():
                    pq_files = [single]

            if not pq_files:
                continue

            rowmap_pattern = f"{source}-{tier}-*-rowmap.json"
            rowmap_files = sorted(output_dir.glob(rowmap_pattern))
            if not rowmap_files:
                single_rm = output_dir / f"{source}-{tier}-rowmap.json"
                if single_rm.exists():
                    rowmap_files = [single_rm]

            tier_entry["sources"][source] = {
                "parquet_files": [f.name for f in pq_files],
                "rowmap_files": [f.name for f in rowmap_files],
            }

        if tier_entry["sources"]:
            manifest["tiers"][tier] = tier_entry

    return manifest


def rebuild_manifest_from_release(release_tag: str) -> dict:
    """Build manifest from actual release assets on GitHub.

    Uses `gh release view` to list assets, then parses filenames
    to discover all source × tier combos. This is the authoritative
    manifest — call after all bakes for a release are complete.
    """
    result = subprocess.run(
        ["gh", "release", "view", release_tag, "--json", "assets", "--jq", ".assets[].name"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to list release assets: {result.stderr}")

    asset_names = result.stdout.strip().split("\n")
    base_url = f"{GITHUB_BASE}/{release_tag}/"

    # schema_version is required by client >= 0.3.0
    manifest: dict = {
        "schema_version": 1,
        "release_tag": release_tag,
        "tiers": {},
    }

    # Parse name: mat-vis-{source}-{tier}-{category}[-{chunk}].parquet
    # Tier can be simple (1k, 128) or hyphenated (ktx2-128, mtlx).
    # Category is pinned to the schema enum — no lazy wildcard, no silent
    # acceptance of malformed names.
    from mat_vis_baker.common import CANONICAL_CATEGORIES

    cat_alt = "|".join(sorted(CANONICAL_CATEGORIES))
    pq_re = re.compile(
        rf"^mat-vis-(?P<source>\w+)-(?P<tier>ktx2-\w+|mtlx(?:-\w+)?|\w+)"
        rf"-(?P<cat>{cat_alt})(?:-\d+)?\.parquet$"
    )
    rm_re = re.compile(
        rf"^(?P<source>\w+)-(?P<tier>ktx2-\w+|mtlx(?:-\w+)?|\w+)"
        rf"-(?P<cat>{cat_alt})(?:-\d+)?-rowmap\.json$"
    )

    parquets: dict[tuple[str, str], list[str]] = {}
    rowmaps: dict[tuple[str, str], list[str]] = {}

    for name in asset_names:
        m = pq_re.match(name)
        if m:
            source, tier, _cat = m.groups()
            parquets.setdefault((source, tier), []).append(name)
        m = rm_re.match(name)
        if m:
            source, tier, _cat = m.groups()
            rowmaps.setdefault((source, tier), []).append(name)

    for (source, tier), pq_files in sorted(parquets.items()):
        if tier not in manifest["tiers"]:
            manifest["tiers"][tier] = {"base_url": base_url, "sources": {}}
        manifest["tiers"][tier]["sources"][source] = {
            "parquet_files": sorted(pq_files),
            "rowmap_files": sorted(rowmaps.get((source, tier), [])),
        }

    log.info(
        "manifest rebuilt from release: %d tiers, %d source×tier combos",
        len(manifest["tiers"]),
        len(parquets),
    )
    return manifest


def write_manifest(manifest: dict, output_path: Path) -> Path:
    """Write manifest JSON to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    log.info("wrote %s", output_path)
    return output_path
