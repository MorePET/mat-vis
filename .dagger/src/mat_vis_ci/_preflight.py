"""Pre-flight prod-cut helpers (mat-vis#345).

Pure-Python predicates for the prod-cut preflight gate. Lives in its
own module so it can be unit-tested without the Dagger SDK in the
venv (mirrors the ``_bake_cli`` pattern). ``main.py`` (which decorates
``MatVisCi`` with Dagger's runtime types) imports from here; tests
import here directly.

Three checks compose the gate; each is a small pure function:

1. :func:`tst_has_release_manifest` — confirms the tst dataset
   carries the same release-tag we're about to push to prod. The
   "ALWAYS E2E on tst before prod" rule made mechanical.
2. :func:`compute_missing_cells` — set difference of
   ``previous_prod_cells - tst_cells - deprecated_cells``. Empty
   means tst's tier coverage at release-tag is a superset of
   previous prod's tier coverage modulo explicit deprecations.
3. :func:`compose_violations` — wraps both into a structured
   violation list the Dagger function reports back to the operator.

The deprecation-approval gate (the agent-resistant escape hatch via
GH issue labels) lives in workflow YAML — it requires the ``gh``
CLI authenticated with the workflow's ``GITHUB_TOKEN`` and a label
permission boundary that the workflow runtime enforces. By the time
this module's :func:`compose_violations` runs, ``deprecated_cells``
has already been verified by the workflow step.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def _hf_manifest_url(repo_id: str, release_tag: str) -> str:
    """Stable URL pattern matching :mod:`mat_vis_baker.hf_bake_per_file`'s
    write side."""
    return f"https://huggingface.co/datasets/{repo_id}/resolve/{release_tag}/release-manifest.json"


def fetch_release_manifest(repo_id: str, release_tag: str) -> dict[str, Any] | None:
    """Fetch a release-manifest.json from HF. Returns ``None`` on 404
    so callers can distinguish "tag doesn't exist" (legitimate) from
    "fetch errored" (re-raise).
    """
    url = _hf_manifest_url(repo_id, release_tag)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def manifest_cells(manifest: dict[str, Any]) -> set[tuple[str, str]]:
    """Extract the ``(source, tier)`` cell set from a v3 manifest.

    Defensive against missing keys — old tags / partial dispatches
    sometimes carry incomplete shapes. Empty/malformed → empty set
    (callers handle "no coverage" semantics explicitly).
    """
    out: set[tuple[str, str]] = set()
    for source, src_entry in (manifest.get("sources") or {}).items():
        if not isinstance(src_entry, dict):
            continue
        for tier in (src_entry.get("tiers") or {}).keys():
            out.add((str(source), str(tier)))
    return out


def tst_has_release_manifest(tst_repo_id: str, release_tag: str) -> bool:
    """True iff the tst dataset has a release-manifest.json at the
    requested tag. Implementation detail: a 200 fetch implies the
    bake actually pushed something at that revision.
    """
    return fetch_release_manifest(tst_repo_id, release_tag) is not None


def compute_missing_cells(
    tst_cells: set[tuple[str, str]],
    prev_prod_cells: set[tuple[str, str]],
    deprecated_cells: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Cells in previous prod that aren't on tst AND aren't explicitly
    deprecated. Empty set means tst is ready to ship (modulo whatever
    other gates run after this).

    Set algebra:
        missing = prev_prod_cells - tst_cells - deprecated_cells
    """
    return prev_prod_cells - tst_cells - deprecated_cells


def compose_violations(
    tst_repo_id: str,
    prod_repo_id: str,
    release_tag: str,
    previous_prod_tag: str,
    deprecated_cells: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Run the full preflight composition. Returns a list of structured
    violation dicts (empty = green; non-empty = abort the prod cut).

    Violation shapes:

    - ``kind="tst_missing_release_tag"``: gate (1) failed; tst doesn't
      have ``release_tag``. Operator action: dispatch a tst bake at
      that tag first.
    - ``kind="tier_coverage_regression"``: gate (2) failed; one or
      more cells from previous prod are absent from tst and not
      deprecated. Operator action: either bake the missing tier(s)
      onto tst OR file a deprecation issue with the
      ``tier-deprecation-approved`` label and re-dispatch with
      ``deprecate-cells`` listing them.

    Caller (Dagger function) raises on non-empty so the workflow
    fails before any prod write.
    """
    violations: list[dict[str, Any]] = []

    tst_manifest = fetch_release_manifest(tst_repo_id, release_tag)
    if tst_manifest is None:
        violations.append(
            {
                "kind": "tst_missing_release_tag",
                "tst_repo_id": tst_repo_id,
                "release_tag": release_tag,
                "remedy": (
                    f"Run a tst bake against {tst_repo_id} at {release_tag} first. "
                    "The 'ALWAYS E2E on tst before prod' rule applies."
                ),
            }
        )
        # Without a tst manifest there's nothing to compute coverage
        # against; short-circuit so downstream output stays focused.
        return violations

    prev_prod_manifest = fetch_release_manifest(prod_repo_id, previous_prod_tag)
    if prev_prod_manifest is None:
        # First-ever prod release, or previous tag was deleted. No
        # coverage to regress against — preflight passes the parity
        # check (mirrors validate_release.py's first-cut free-pass).
        return violations

    tst_cells = manifest_cells(tst_manifest)
    prev_cells = manifest_cells(prev_prod_manifest)
    missing = compute_missing_cells(tst_cells, prev_cells, deprecated_cells)
    if missing:
        violations.append(
            {
                "kind": "tier_coverage_regression",
                "tst_repo_id": tst_repo_id,
                "prod_repo_id": prod_repo_id,
                "release_tag": release_tag,
                "previous_prod_tag": previous_prod_tag,
                "missing_cells": sorted(missing),
                "remedy": (
                    "Either bake the missing (source, tier) cells onto tst at the "
                    "release-tag, OR open a GH issue per missing cell with the "
                    "`tier-deprecation-approved` label (maintainer-only) and re-dispatch "
                    "with `deprecate-cells` listing the deprecated cells."
                ),
            }
        )
    return violations


def parse_deprecate_cells(arg: str) -> set[tuple[str, str]]:
    """Parse the ``--deprecate-cells`` CLI/Dagger arg.

    Accepts a JSON list of ``[source, tier]`` pairs (or objects with
    ``source``/``tier`` keys). Empty/missing → empty set.

    Examples::

        '[]'                                            → set()
        '[["gpuopen", "128"], ["gpuopen", "256"]]'      → {("gpuopen", "128"), …}
        '[{"source":"gpuopen","tier":"128"}]'           → {("gpuopen", "128")}
    """
    if not arg or not arg.strip():
        return set()
    try:
        parsed = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--deprecate-cells must be valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"--deprecate-cells must be a JSON list, got {type(parsed).__name__}")

    out: set[tuple[str, str]] = set()
    for item in parsed:
        if isinstance(item, list) and len(item) == 2:
            out.add((str(item[0]), str(item[1])))
        elif isinstance(item, dict) and "source" in item and "tier" in item:
            out.add((str(item["source"]), str(item["tier"])))
        else:
            raise ValueError(
                f"--deprecate-cells entries must be [source, tier] pairs "
                f"or {{source, tier}} objects; got {item!r}"
            )
    return out
