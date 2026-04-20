#!/usr/bin/env python
"""Freeze an HF dataset revision — write a static ``release-manifest.json``.

Runs once per release, after all bakes + derives have settled. Single
writer → no race by definition. Clients don't need this file (they
build the manifest from the tree listing at read time), but it's a
convenience index for non-HF-aware tools / `curl` users / the HF
dataset viewer.

Usage:

    HF_TOKEN=$(security find-generic-password -a "$USER" -s huggingface-token -w) \\
      .venv/bin/python scripts/freeze_release.py v2026.04.1 \\
        --repo-id gerchowl/mat-vis
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import requests
from huggingface_hub import CommitOperationAdd, HfApi

from mat_vis_baker.manifest import build_manifest_from_tree, write_manifest

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
log = logging.getLogger("freeze-release")


def _fetch_tree(repo_id: str, revision: str, token: str | None) -> list[str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(
        f"https://huggingface.co/api/datasets/{repo_id}/tree/{revision}?recursive=true",
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    return [e["path"] for e in r.json() if e.get("type") == "file"]


def _fetch_materials_counts(
    repo_id: str, revision: str, tree_paths: list[str], token: str | None
) -> dict[str, int]:
    """Download each top-level ``<src>.json`` catalog and return its length."""
    counts: dict[str, int] = {}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for path in tree_paths:
        if path.endswith(".json") and "-rowmap" not in path and "/" not in path:
            if path == "release-manifest.json":
                continue
            src = path[:-5]
            r = requests.get(
                f"https://huggingface.co/datasets/{repo_id}/resolve/{revision}/{path}",
                headers=headers,
                timeout=60,
            )
            r.raise_for_status()
            counts[src] = len(r.json())
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(prog="freeze_release")
    ap.add_argument("revision", help="Branch or tag to freeze (e.g. v2026.04.1)")
    ap.add_argument("--repo-id", default="gerchowl/mat-vis")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("HF_TOKEN")
    log.info("freezing %s@%s", args.repo_id, args.revision)

    tree_paths = _fetch_tree(args.repo_id, args.revision, token)
    log.info("tree: %d files", len(tree_paths))

    counts = _fetch_materials_counts(args.repo_id, args.revision, tree_paths, token)
    log.info("materials counts: %s", counts)

    manifest = build_manifest_from_tree(
        release_tag=args.revision,
        tree_paths=tree_paths,
        materials_counts=counts,
    )
    log.info(
        "manifest: %d sources, %d tars total",
        len(manifest["sources"]),
        sum(len(s.get("tiers") or {}) for s in manifest["sources"].values()),
    )

    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "release-manifest.json"
        write_manifest(manifest, path)
        api = HfApi(token=token)
        info = api.create_commit(
            repo_id=args.repo_id,
            repo_type="dataset",
            operations=[
                CommitOperationAdd(path_in_repo="release-manifest.json", path_or_fileobj=str(path))
            ],
            commit_message=f"chore(data): freeze {args.revision} — write static release-manifest.json",
            revision=args.revision,
        )
        log.info("commit: %s", getattr(info, "oid", "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
