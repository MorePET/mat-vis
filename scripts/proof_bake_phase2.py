#!/usr/bin/env python
"""Phase 2 proof bake: physicallybased → HF as v2026.05.0-rc1.

End-to-end proof of the v0.5.0 substrate for the smallest source
(scalar-only, no textures → no tar, just the catalog + manifest).
Pushes `physicallybased.json` + `release-manifest.json` atomically to
`gerchowl/mat-vis` under the pre-release revision `v2026.05.0-rc1`.

Usage:

    HF_TOKEN=$(security find-generic-password -a "$USER" -s huggingface-token -w) \\
      .venv/bin/python scripts/proof_bake_phase2.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path

# Import from the in-tree baker package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mat_vis_baker.hf_push import push_to_hf  # noqa: E402
from mat_vis_baker.index_builder import build_index  # noqa: E402
from mat_vis_baker.sources import physicallybased  # noqa: E402

REPO_ID = "gerchowl/mat-vis"
REVISION = "v2026.05.0-rc1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
log = logging.getLogger("proof-phase-2")


def main() -> int:
    log.info("fetching physicallybased upstream…")
    records = physicallybased.fetch()
    log.info("got %d records", len(records))

    index = build_index(records, source="physicallybased")
    manifest = {
        "version": 2,
        "release_tag": REVISION,
        "sources": {
            "physicallybased": {
                "catalog": "physicallybased.json",
                "materials_count": len(index),
            }
        },
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        catalog_path = tmp_dir / "physicallybased.json"
        manifest_path = tmp_dir / "release-manifest.json"

        catalog_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

        log.info("pushing to %s@%s…", REPO_ID, REVISION)
        sha = push_to_hf(
            repo_id=REPO_ID,
            files=[
                (manifest_path, "release-manifest.json"),
                (catalog_path, "physicallybased.json"),
            ],
            revision=REVISION,
            commit_message=("feat(data): v2026.05.0-rc1 — phase-2 proof bake (physicallybased)"),
        )

    base = f"https://huggingface.co/datasets/{REPO_ID}/resolve/{REVISION}"
    log.info("commit sha: %s", sha or "(none)")
    log.info("manifest:   %s/release-manifest.json", base)
    log.info("catalog:    %s/physicallybased.json", base)
    log.info("entries:    %d", len(index))
    return 0


if __name__ == "__main__":
    sys.exit(main())
