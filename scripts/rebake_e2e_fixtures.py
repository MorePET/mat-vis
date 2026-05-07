#!/usr/bin/env python3
"""Restore the preserved E2E fixture set on Hugging Face.

mat-vis#358 follow-up: cache lifecycle E2E tests pin against a
dedicated, frozen tag (``v0.0.0-e2e-fixtures``) on
``gerchowl/mat-vis-tst``. When the preflight in ``_e2e_fixtures.py``
reports the set is missing (someone deleted the tag, HF storage
cleanup, etc.), this script re-creates it via one ``bake.yml``
dispatch.

The fixture set is intentionally tiny: 2 materials × 1 tier × ~5
channels = ~5 MB total. ``--limit=2`` on the bake matches the curated
material ids declared in :mod:`tests.e2e._e2e_fixtures`.

Usage::

    python scripts/rebake_e2e_fixtures.py        # dry-run; print plan
    python scripts/rebake_e2e_fixtures.py --go    # actually dispatch

The dispatch hits ``gh workflow run bake.yml`` (no credentials needed
in this script — the user's existing ``gh`` auth is used). After the
dispatch completes, re-run the preflight to confirm fixtures land.

Why a separate script + dedicated tag rather than reusing v2026.04.X:

- **Stable across substrate evolution**: prod tags get re-baked on
  every release; tests would break on every cut.
- **Recoverable**: one script invocation restores; no cross-team
  coordination needed.
- **Tiny**: limit=2 keeps HF bandwidth budget happy.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Re-import the fixture declaration so the script and the tests
# always agree on the source/tier/material set.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "e2e"))
from _e2e_fixtures import (  # noqa: E402
    E2E_FIXTURE_MATERIALS,
    E2E_FIXTURE_SOURCE,
    E2E_FIXTURE_TIER,
    E2E_FIXTURES_REPO,
    E2E_FIXTURES_TAG,
    preflight_fixtures_present,
)


def _run(cmd: list[str], *, dry: bool) -> int:
    print("  $", " ".join(cmd))
    if dry:
        return 0
    return subprocess.call(cmd)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--go",
        action="store_true",
        help="Actually dispatch the workflow (default: dry-run / preflight only).",
    )
    p.add_argument(
        "--line",
        default="v2026.04",
        help="Release line to use for matrix expansion (the bake reads cells "
        "from mat_vis_baker.release_matrix; the line just selects which "
        "(source, tier) pairs are valid).",
    )
    args = p.parse_args()

    print(f"E2E fixtures: {E2E_FIXTURES_REPO}@{E2E_FIXTURES_TAG}")
    print(f"  source: {E2E_FIXTURE_SOURCE}")
    print(f"  tier:   {E2E_FIXTURE_TIER}")
    print(f"  materials: {', '.join(E2E_FIXTURE_MATERIALS)}")

    print("\nPreflight check:")
    ok, missing = preflight_fixtures_present()
    if ok:
        print("  ✓ all fixtures present on HF — no rebake needed.")
        if not args.go:
            return 0
        print("  --go set; re-baking anyway (e.g. to refresh ETags).")
    else:
        print(f"  ✗ {len(missing)} fixture(s) missing:")
        for m in missing[:10]:
            print(f"    - {m}")

    print("\nPlan:")

    if shutil.which("gh") is None:
        print("error: `gh` CLI not on PATH; install GitHub CLI to dispatch", file=sys.stderr)
        return 2

    cmd = [
        "gh",
        "workflow",
        "run",
        "bake.yml",
        "-f",
        f"line={args.line}",
        "-f",
        f"filter-source={E2E_FIXTURE_SOURCE}",
        "-f",
        f"filter-tier={E2E_FIXTURE_TIER}",
        "-f",
        f"release-tag={E2E_FIXTURES_TAG}",
        "-f",
        f"repo-id={E2E_FIXTURES_REPO}",
        "-f",
        f"limit={len(E2E_FIXTURE_MATERIALS)}",
    ]

    rc = _run(cmd, dry=not args.go)
    if not args.go:
        print("\n(dry-run; re-run with --go to actually dispatch)")
        return 0
    if rc != 0:
        print(f"\nworkflow dispatch failed (rc={rc})", file=sys.stderr)
        return rc

    print(
        "\nDispatched. Wait for the workflow to complete (`gh run list "
        "--workflow=bake.yml --limit 1`), then re-run preflight:"
    )
    print(
        "  python -c 'from tests.e2e._e2e_fixtures import preflight_fixtures_present; "
        "print(preflight_fixtures_present())'"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
