"""Generate release-manifest.json for client auto-discovery.

Schema: docs/specs/release-manifest-schema.json
See issue #22 for context.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("mat-vis-baker.manifest")

GITHUB_BASE = "https://github.com/MorePET/mat-vis/releases/download"


def generate_manifest(
    output_dir: Path,
    release_tag: str,
    sources: list[str],
    tiers: list[str],
) -> dict:
    """Build a release manifest from the bake output directory.

    Scans for parquet and rowmap files to populate the manifest.
    """
    manifest: dict = {
        "version": 1,
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
            # Find partitioned parquet files for this source+tier
            pattern = f"mat-vis-{source}-{tier}-*.parquet"
            pq_files = sorted(output_dir.glob(pattern))
            if not pq_files:
                # Try non-partitioned (legacy)
                single = output_dir / f"mat-vis-{source}-{tier}.parquet"
                if single.exists():
                    pq_files = [single]

            if not pq_files:
                continue

            # Find rowmap files
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


def write_manifest(manifest: dict, output_path: Path) -> Path:
    """Write manifest JSON to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    log.info("wrote %s", output_path)
    return output_path
