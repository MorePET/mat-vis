#!/usr/bin/env python3
"""Verify each declared deprecation has a maintainer-approved issue.

mat-vis#345's agent-resistant escape hatch: a workflow step calls
this script with the JSON list of ``(source, tier)`` cells the
operator wants to mark as deprecated. The script queries the
``MorePET/mat-vis`` issue tracker via ``gh`` for issues bearing
the ``tier-deprecation-approved`` label and verifies one such issue
matches each declared cell (by title containing the
``(source, tier)`` cell text).

Why this is agent-resistant:

    The ``tier-deprecation-approved`` label can only be applied or
    removed by repo maintainers (``triage`` role or higher).
    Agents / contributor accounts can:
      - open issues, edit text, comment
      - open PRs that reference labels in code
    But CANNOT apply protected labels at the repo level — that's
    enforced by GitHub's permission model, not by this script.
    So the gate's signal is a permission boundary GitHub itself
    enforces, not something an agent can fake by writing code.

Usage::

    python scripts/verify_deprecation_issues.py '[["gpuopen", "128"]]'

Exit codes:

    0  every declared cell has a matching approved issue
    1  one or more cells lack an approved issue (operator must
       open one + get a maintainer to apply the label)
    2  setup error (gh CLI missing, network failure, malformed input)

Designed to run as a GH Actions workflow step (no extra deps; gh
is preinstalled on hosted runners; ``GITHUB_TOKEN`` provides auth).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

LABEL = "tier-deprecation-approved"


def _gh_issues_with_label(label: str) -> list[dict]:
    """Query open issues with the given label. Returns list of
    ``{number, title, author}`` dicts. Re-raises on gh failure."""
    if shutil.which("gh") is None:
        print("error: gh CLI not on PATH", file=sys.stderr)
        sys.exit(2)
    try:
        out = subprocess.check_output(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--label",
                label,
                "--limit",
                "200",
                "--json",
                "number,title,author",
            ],
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"error: gh issue list failed: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        print(f"error: gh output malformed: {exc}", file=sys.stderr)
        sys.exit(2)


def _matches(issue: dict, source: str, tier: str) -> bool:
    """An issue matches a (source, tier) cell if its title contains
    BOTH the source name and the tier in close proximity. Loose match
    is intentional — operators write titles like
    ``deprecate (gpuopen, 128) for v2026.05`` or
    ``Drop gpuopen 128 — superseded by ktx2-1k`` and we don't want to
    rigid-template them.

    Hardening (P1 of #345): could require a structured body field or
    a specific title prefix. Out of scope for the initial gate.
    """
    title = (issue.get("title") or "").lower()
    return source.lower() in title and tier.lower() in title


def _parse_cells(arg: str) -> list[tuple[str, str]]:
    """Mirror ``_preflight.parse_deprecate_cells`` (kept here as a
    standalone copy so this script has zero internal imports — runs
    on a fresh checkout in any GH Actions runner).
    """
    if not arg.strip():
        return []
    parsed = json.loads(arg)
    if not isinstance(parsed, list):
        raise ValueError(f"input must be JSON list; got {type(parsed).__name__}")
    out: list[tuple[str, str]] = []
    for item in parsed:
        if isinstance(item, list) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
        elif isinstance(item, dict) and "source" in item and "tier" in item:
            out.append((str(item["source"]), str(item["tier"])))
        else:
            raise ValueError(f"unrecognised cell shape: {item!r}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(
            "usage: verify_deprecation_issues.py '<json-list-of-cells>'",
            file=sys.stderr,
        )
        return 2
    try:
        cells = _parse_cells(argv[0])
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error parsing input: {exc}", file=sys.stderr)
        return 2

    if not cells:
        print("no deprecations declared — preflight pass-through")
        return 0

    issues = _gh_issues_with_label(LABEL)
    print(f"found {len(issues)} open issue(s) with label '{LABEL}'")

    missing: list[tuple[str, str]] = []
    for source, tier in cells:
        if not any(_matches(i, source, tier) for i in issues):
            missing.append((source, tier))

    if missing:
        print(
            f"FAIL: {len(missing)} declared deprecation(s) lack a maintainer-approved issue.",
            file=sys.stderr,
        )
        for source, tier in missing:
            print(f"  - ({source}, {tier})", file=sys.stderr)
        print(
            f"\nOpen a GH issue for each — title must mention the source AND tier — and ask a "
            f"maintainer to apply the '{LABEL}' label. The label is gated on the repo's "
            "triage role, which contributor / agent accounts don't have.",
            file=sys.stderr,
        )
        return 1

    print(f"all {len(cells)} declared deprecation(s) have approved issues")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
