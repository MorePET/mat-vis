#!/usr/bin/env python
"""Empirical probe of HF Hub limits relevant to the per-file substrate (#182).

Two sharp edges we need to measure before committing to option C:

1. **Per-commit file count ceiling**: doc says 25,000 LFS files / 1 GB
   regular payload per commit, recommends 50-100 files to stay under
   the 60-second HTTP timeout. Actual latency at 5k / 10k / 25k.

2. **Tree listing performance at scale**: paginated 50/page, recursive
   supported. Latency scaling with repo size.

Runs against ``gerchowl/mat-vis-tst`` on a throwaway branch that gets
deleted at exit. Does NOT touch production. Requires HF_TOKEN.

Output: per-probe metrics dumped as JSON to ``/tmp/hf-probe-{n}.json``
so the spike decision has real numbers not rumour.

Usage::

    HF_TOKEN=$(cat ~/.cache/huggingface/token) \\
      uv run python scripts/probe-hf-per-file-limits.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

try:
    from huggingface_hub import HfApi
    from huggingface_hub.utils import RepositoryNotFoundError
except ImportError:
    sys.exit("missing huggingface_hub — run inside `uv run`")

REPO = "gerchowl/mat-vis-tst"
PROBE_BRANCH = "probe-182-per-file"
OUT = Path("/tmp")


def _auth_api() -> HfApi:
    tok = (
        os.environ.get("HF_TOKEN")
        or Path("~/.cache/huggingface/token").expanduser().read_text().strip()
    )
    return HfApi(token=tok)


def _mk_dummy_payload(kb: int = 1) -> bytes:
    """Small synthetic payload — stays under the 10 MB LFS threshold so
    we exercise the regular-file path (cheaper for quick probes)."""
    return (b"A" * 1024) * kb


def probe_commit_size(api: HfApi, n_files: int) -> dict:
    """Push N synthetic files in one atomic commit; record wall-clock."""
    from huggingface_hub import CommitOperationAdd

    payload = _mk_dummy_payload(1)
    ops = [
        CommitOperationAdd(
            path_in_repo=f"probe-{n_files}/f{i:05d}.bin",
            path_or_fileobj=payload,
        )
        for i in range(n_files)
    ]
    t0 = time.monotonic()
    try:
        info = api.create_commit(
            repo_id=REPO,
            repo_type="dataset",
            operations=ops,
            commit_message=f"probe #182: {n_files} files in one commit",
            revision=PROBE_BRANCH,
        )
        elapsed = time.monotonic() - t0
        return {
            "n_files": n_files,
            "success": True,
            "elapsed_s": round(elapsed, 2),
            "commit_sha": getattr(info, "oid", "")[:12],
        }
    except Exception as e:
        return {
            "n_files": n_files,
            "success": False,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": f"{type(e).__name__}: {e}"[:300],
        }


def probe_tree_listing(api: HfApi, revision: str) -> dict:
    """Time one recursive tree listing of the probe branch."""
    t0 = time.monotonic()
    try:
        tree = list(api.list_repo_tree(repo_id=REPO, revision=revision, recursive=True))
        elapsed = time.monotonic() - t0
        return {
            "success": True,
            "file_count": sum(1 for e in tree if e.__class__.__name__ == "RepoFile"),
            "entry_count": len(tree),
            "elapsed_s": round(elapsed, 2),
        }
    except Exception as e:
        return {
            "success": False,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": f"{type(e).__name__}: {e}"[:300],
        }


def cleanup(api: HfApi) -> None:
    """Delete the probe branch so the scratch repo stays tidy."""
    try:
        api.delete_branch(repo_id=REPO, repo_type="dataset", branch=PROBE_BRANCH)
        print(f"cleanup: deleted branch {PROBE_BRANCH}", file=sys.stderr)
    except Exception as e:
        print(f"cleanup warning: {e}", file=sys.stderr)


def main() -> int:
    api = _auth_api()

    # Ensure the probe branch exists from main.
    try:
        api.create_branch(
            repo_id=REPO,
            repo_type="dataset",
            branch=PROBE_BRANCH,
            revision="main",
            exist_ok=True,
        )
        print(f"probe branch ready: {PROBE_BRANCH}", file=sys.stderr)
    except RepositoryNotFoundError:
        print(f"repo {REPO} not accessible — check HF_TOKEN", file=sys.stderr)
        return 1

    results: dict = {"commit_size_probes": [], "tree_listing_probes": []}

    # Escalating commit sizes — stop early on first failure.
    for n in (100, 1_000, 5_000, 10_000, 25_000):
        print(f"probing: {n}-file commit...", file=sys.stderr)
        r = probe_commit_size(api, n)
        results["commit_size_probes"].append(r)
        print(f"  → {r}", file=sys.stderr)
        if not r["success"]:
            print("first failure reached; stopping commit-size escalation.", file=sys.stderr)
            break

    # Tree listing of whatever state we ended up in.
    print("probing: recursive tree listing...", file=sys.stderr)
    r = probe_tree_listing(api, PROBE_BRANCH)
    results["tree_listing_probes"].append(r)
    print(f"  → {r}", file=sys.stderr)

    out = OUT / "hf-probe-182.json"
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nresults written to {out}", file=sys.stderr)

    cleanup(api)
    return 0


if __name__ == "__main__":
    sys.exit(main())
