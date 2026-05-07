#!/usr/bin/env python3
"""Upstream content-drift gate (mat-vis#295).

Per-material ``upstream.raw`` equality check between consecutive
releases on the HF substrate. Catches the silent-edit class:

- AMD changes Aluminum Brushed's metalness from 1.0 → 0.95 in their
  upstream catalog without renaming or removing the material.
- polyhaven re-uploads a normal map with the same id but a new
  texture.
- ambientcg silently revises a material's category or dimensions.

The existing schema-drift gate (`check_upstream_schema_drift.py`)
catches NEW or REMOVED keys but is structurally blind to these:
*the keys stay the same, the values change*. Without this gate, every
gap-fill bake on top of an existing tag silently picks up the live
upstream — soft-forking the release.

Per #306: cells to check come from `mat_vis_baker.release_matrix`
(the canonical source-of-truth for what a release line contains).
This script just gets `<source>` from CLI; the workflow loops
sources from the matrix's cells.

Algorithm:
  1. Fetch the candidate `<source>.json` (just baked, just pushed
     to HF at `--release-tag`).
  2. Fetch the previous release's `<source>.json` from HF.
  3. For each material id present in BOTH, hash a stable
     representation of `upstream.raw` (excluding per-bake volatile
     fields per the source-specific allowlist below).
  4. Fail if any per-material hash diverges, modulo the waiver YAML.

Free pass on first cut: if the previous release's catalog 404s or
lacks the `upstream` block (pre-Phase-C records), log + exit 0.
Mirrors the schema-drift gate's first-run behavior.

Usage:
    uv run python scripts/check_upstream_content_drift.py \\
        --source ambientcg \\
        --candidate https://huggingface.co/.../v2026.04.3/ambientcg.json \\
        --previous https://huggingface.co/.../v2026.04.2/ambientcg.json

    # With a waiver YAML for known-OK upstream edits:
    ... --waivers .github/content-drift-waivers.yaml

Waiver YAML shape:
    ambientcg:
      - id: "Rock064"
        reason: "AMD legitimately re-published with corrected category"
        accepted_by: "lars"
        date: "2026-05-08"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("content-drift-gate")
logging.basicConfig(level=logging.INFO, format="%(message)s")


# Per-source: keys inside ``upstream.raw`` that change every bake
# without representing real content drift (download counts, etc.).
# Excluded from the stable hash. Empty set = hash raw as-is.
#
# IMPORTANT: this is NOT for upstream's own content timestamps
# (`updated_date`, `created_date`, `published_date`) — those DO change
# when upstream actually edits a material, which IS the drift we
# want to catch. Only true per-bake nondeterminism goes here.
_VOLATILE_UPSTREAM_RAW_FIELDS: dict[str, frozenset[str]] = {
    "ambientcg": frozenset({"downloadCount", "popularityScore"}),
    "polyhaven": frozenset({"download_count"}),
    "gpuopen": frozenset(),
    "physicallybased": frozenset(),
}


def stable_hash_upstream_raw(raw: dict[str, Any], volatile: frozenset[str]) -> str:
    """SHA-256 over ``upstream.raw`` with volatile fields stripped.

    JSON serialization is sort_keys=True + separators=(",", ":") so
    dict ordering / whitespace can never produce false positives.
    """
    filtered = {k: v for k, v in raw.items() if k not in volatile}
    blob = json.dumps(filtered, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def fetch_catalog(url: str) -> list[dict[str, Any]] | None:
    """Fetch a `<source>.json` catalog. Returns ``None`` on 404."""
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def diff_content(
    source: str,
    prev: list[dict[str, Any]],
    cand: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Per-material upstream.raw hash diff. Returns drift records."""
    volatile = _VOLATILE_UPSTREAM_RAW_FIELDS.get(source, frozenset())

    prev_by_id = {e["id"]: e for e in prev if "id" in e}
    cand_by_id = {e["id"]: e for e in cand if "id" in e}
    common = prev_by_id.keys() & cand_by_id.keys()

    drifted: list[dict[str, str]] = []
    for mid in sorted(common):
        prev_raw = (prev_by_id[mid].get("upstream") or {}).get("raw")
        cand_raw = (cand_by_id[mid].get("upstream") or {}).get("raw")
        if prev_raw is None or cand_raw is None:
            # Pre-Phase-C records on either side — skip rather than
            # false-positive on the schema migration itself.
            continue
        h_prev = stable_hash_upstream_raw(prev_raw, volatile)
        h_cand = stable_hash_upstream_raw(cand_raw, volatile)
        if h_prev != h_cand:
            drifted.append({"id": mid, "prev_hash": h_prev, "cand_hash": h_cand})
    return drifted


def load_waivers(path: Path | None, source: str) -> set[str]:
    """Parse waiver YAML → set of waived material ids for ``source``.

    YAML shape (per module docstring):
        <source>:
          - id: <material-id>
            reason: <text>
            ...
    """
    if path is None or not path.exists():
        return set()
    raw = path.read_text()
    if not raw.strip():
        return set()

    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw) or {}
    except ImportError:  # pragma: no cover — tested envs install pyyaml
        log.warning("PyYAML not installed; waivers ignored")
        return set()

    entries = (data or {}).get(source) or []
    return {str(e["id"]) for e in entries if isinstance(e, dict) and "id" in e}


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--source", required=True, help="Source name (e.g. ambientcg)")
    p.add_argument("--candidate", required=True, help="URL to candidate <source>.json")
    p.add_argument("--previous", required=True, help="URL to previous <source>.json")
    p.add_argument(
        "--waivers",
        type=Path,
        default=None,
        help="Optional YAML file with per-id waivers for known-OK upstream edits",
    )
    p.add_argument(
        "--max-report",
        type=int,
        default=20,
        help="Max drifted material ids to print on failure (default 20)",
    )
    args = p.parse_args()

    log.info("content-drift gate: source=%s", args.source)
    log.info("  candidate: %s", args.candidate)
    log.info("  previous:  %s", args.previous)

    cand = fetch_catalog(args.candidate)
    if cand is None:
        log.error("FAIL: candidate %s returned 404", args.candidate)
        return 2

    prev = fetch_catalog(args.previous)
    if prev is None:
        log.info(
            "  OK no previous catalog at %s — first cut on this line, free pass", args.previous
        )
        return 0

    drifted = diff_content(args.source, prev, cand)
    waived_ids = load_waivers(args.waivers, args.source)
    blocking = [d for d in drifted if d["id"] not in waived_ids]
    waived_count = len(drifted) - len(blocking)

    if not blocking:
        log.info(
            "  OK %s: %d materials, no content drift (waived: %d)",
            args.source,
            len(cand),
            waived_count,
        )
        return 0

    log.error(
        "FAIL content-drift: %d material(s) drifted in %s (showing first %d)",
        len(blocking),
        args.source,
        min(args.max_report, len(blocking)),
    )
    for d in blocking[: args.max_report]:
        log.error("  %s: %s -> %s", d["id"], d["prev_hash"][:12], d["cand_hash"][:12])
    if len(blocking) > args.max_report:
        log.error("  ... and %d more", len(blocking) - args.max_report)
    log.error(
        "If these are legitimate upstream edits, add to the waiver YAML; "
        "otherwise investigate the upstream change."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
