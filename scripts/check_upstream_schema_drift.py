#!/usr/bin/env python3
"""Upstream schema-drift gate (ADR-0011 / mat-vis#152 phase-c).

Compares a candidate ``<source>.json`` catalog against the most recently
published one on the Hugging Face dataset. Three signals:

1. **NEW keys** under any record's ``upstream.raw``. Warn + exit 0. These
   are the interesting signal — hand-review before widening the
   allowlist; the pipeline keeps working.
2. **REMOVED keys** from any record's ``mat_vis.*`` curated blocks. Fail
   (exit 1). Layer-1 is semver-stable; an extractor that stops populating
   a curated field is a breaking change and must be caught in CI.
3. **PRESENCE REGRESSION** in ``mat_vis.*`` fields: if 95% of records
   used to populate ``mat_vis.pbr.roughness`` and now only 80% do, fail.
   Threshold is a hard >5 percentage-point drop vs. the previous release.

First-run pass: if the previous release doesn't carry ``upstream`` (i.e.
the source was last baked pre-Phase-C), log + exit 0. This is the only
free pass — every subsequent bake has a baseline to diff against.

Usage:
    uv run python scripts/check_upstream_schema_drift.py \\
        --source ambientcg \\
        --candidate ./dist/ambientcg.json

    # Or point at a URL (CI: candidate just pushed to HF at a new tag):
    uv run python scripts/check_upstream_schema_drift.py \\
        --source ambientcg \\
        --candidate-url https://huggingface.co/.../ambientcg.json

The previous release tag is discovered via the GH releases API unless
overridden via ``--previous-tag``. Network calls are stdlib urllib so the
script has zero runtime deps (the script runs under ``uv run`` which
resolves its own sandbox without a pyproject).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

HF_DATASET = "gerchowl/mat-vis"
HF_BASE = f"https://huggingface.co/datasets/{HF_DATASET}/resolve"
GH_API_LATEST = "https://api.github.com/repos/MorePET/mat-vis/releases/latest"

# Layer-1 curated blocks whose key-sets are semver-stable. When a record
# loses one of these keys we fail the gate — losing a key is a breaking
# change to the ``mat_vis.*`` contract.
MAT_VIS_STABLE_BLOCKS = (
    "physical",
    "pbr",
    "attribution",
    "dates",
)

# Presence-regression threshold: if field X was populated in P_prev% of
# records in the previous release and P_cand% in the candidate, we fail
# when P_prev - P_cand > 5 (i.e. >5pp drop). The threshold matches the
# ADR-0011 spec + gives us a deterministic tripwire.
PRESENCE_REGRESSION_PP = 5.0

log = logging.getLogger("schema-drift")


def _http_get(url: str, timeout: float = 30) -> bytes:
    """Minimal stdlib GET. Raises on non-200 / network errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "mat-vis-schema-drift"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed host
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {url}")
        return resp.read()


def _latest_release_tag() -> str | None:
    """GitHub ``releases/latest`` tag, or ``None`` if unavailable."""
    try:
        data = json.loads(_http_get(GH_API_LATEST))
        return data.get("tag_name")
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        log.warning("could not discover latest release tag: %s", exc)
        return None


def _fetch_previous_catalog(source: str, tag: str) -> list[dict] | None:
    """Load ``<source>.json`` from HF at ``tag``. ``None`` on 404 / error."""
    url = f"{HF_BASE}/{tag}/{source}.json"
    try:
        blob = _http_get(url)
        return json.loads(blob)
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        log.warning("could not fetch previous %s from %s: %s", source, url, exc)
        return None


def _load_candidate(candidate: str) -> list[dict]:
    """Load the candidate catalog from a local path OR URL."""
    if candidate.startswith(("http://", "https://")):
        return json.loads(_http_get(candidate))
    path = Path(candidate)
    if not path.exists():
        raise SystemExit(f"candidate not found: {path}")
    return json.loads(path.read_text())


def _collect_upstream_keys(catalog: list[dict]) -> set[str]:
    """Union of every ``upstream.raw`` top-level key across the catalog."""
    keys: set[str] = set()
    for entry in catalog:
        raw = (entry.get("upstream") or {}).get("raw") or {}
        if isinstance(raw, dict):
            keys.update(raw.keys())
    return keys


def _flatten_mat_vis_keys(mv: dict, prefix: str = "mat_vis") -> set[str]:
    """Flatten a single entry's ``mat_vis`` block to dotted keys.

    Only descends one level (into the curated sub-blocks listed in
    ``MAT_VIS_STABLE_BLOCKS``); deeper structures (e.g. list-valued
    ``pbr.color_rgb``) stay leaf — their presence is what we care about,
    not their internal shape.
    """
    out: set[str] = set()
    for k, v in mv.items():
        dotted = f"{prefix}.{k}"
        if k in MAT_VIS_STABLE_BLOCKS and isinstance(v, dict):
            for sub in v:
                out.add(f"{dotted}.{sub}")
        else:
            out.add(dotted)
    return out


def _collect_mat_vis_keys(catalog: list[dict]) -> set[str]:
    """Union of every flattened ``mat_vis.*`` key across the catalog."""
    keys: set[str] = set()
    for entry in catalog:
        mv = entry.get("mat_vis") or {}
        keys.update(_flatten_mat_vis_keys(mv))
    return keys


def _field_presence(catalog: list[dict]) -> dict[str, float]:
    """Per-key fraction of records where ``mat_vis.<path>`` is non-null.

    Denominator is total records with a ``mat_vis`` block — we only rate
    keys, not optional records.
    """
    counts: dict[str, int] = {}
    total = 0
    for entry in catalog:
        mv = entry.get("mat_vis") or {}
        if not mv:
            continue
        total += 1
        for block in MAT_VIS_STABLE_BLOCKS:
            sub = mv.get(block)
            if not isinstance(sub, dict):
                continue
            for leaf_k, leaf_v in sub.items():
                key = f"mat_vis.{block}.{leaf_k}"
                if leaf_v is None or leaf_v == [] or leaf_v == "":
                    continue
                counts[key] = counts.get(key, 0) + 1
    if total == 0:
        return {}
    return {k: (n / total) * 100.0 for k, n in counts.items()}


def run(source: str, candidate: str, *, previous_tag: str | None = None) -> int:
    """Entry point. Returns exit code (0 pass, 1 fail)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    candidate_catalog = _load_candidate(candidate)
    log.info("loaded %d candidate records from %s", len(candidate_catalog), candidate)

    tag = previous_tag or _latest_release_tag()
    if tag is None:
        log.info("no previous release tag discoverable — first-run free pass")
        return 0
    log.info("diffing against previous release: %s", tag)

    previous_catalog = _fetch_previous_catalog(source, tag)
    if previous_catalog is None:
        log.info("no previous %s.json at %s — first-run free pass", source, tag)
        return 0

    prev_has_upstream = any("upstream" in e for e in previous_catalog)
    if not prev_has_upstream:
        log.info(
            "previous release %s has no upstream block on %s — pre-v0.6.0 catalog, "
            "first-run free pass for the new Layer-2 contract",
            tag,
            source,
        )
        return 0

    failures: list[str] = []

    # 1. NEW keys under upstream.raw — warn only.
    prev_up = _collect_upstream_keys(previous_catalog)
    cand_up = _collect_upstream_keys(candidate_catalog)
    new_upstream = sorted(cand_up - prev_up)
    if new_upstream:
        log.warning(
            "upstream.raw: %d NEW key(s) appeared since %s: %s",
            len(new_upstream),
            tag,
            new_upstream,
        )

    # 2. REMOVED keys from mat_vis.* — fail.
    prev_mv = _collect_mat_vis_keys(previous_catalog)
    cand_mv = _collect_mat_vis_keys(candidate_catalog)
    removed_mv = sorted(prev_mv - cand_mv)
    if removed_mv:
        failures.append(f"mat_vis.* REMOVED keys (breaking): {removed_mv}")

    # 3. PRESENCE regression >5pp on any mat_vis.* field.
    prev_pres = _field_presence(previous_catalog)
    cand_pres = _field_presence(candidate_catalog)
    regressions: list[str] = []
    for key, prev_pct in prev_pres.items():
        cand_pct = cand_pres.get(key, 0.0)
        drop = prev_pct - cand_pct
        if drop > PRESENCE_REGRESSION_PP:
            regressions.append(f"{key}: {prev_pct:.1f}% → {cand_pct:.1f}% (drop {drop:.1f}pp)")
    if regressions:
        failures.append(
            f"mat_vis.* PRESENCE regression (> {PRESENCE_REGRESSION_PP}pp drop): {regressions}"
        )

    if failures:
        for f in failures:
            log.error(f)
        return 1

    log.info("schema-drift OK (new upstream keys: %d, removed mat_vis keys: 0)", len(new_upstream))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", required=True, help="Source name (ambientcg / polyhaven / ...)")
    parser.add_argument(
        "--candidate",
        required=True,
        help="Path or https:// URL to the candidate <source>.json",
    )
    parser.add_argument(
        "--previous-tag",
        default=None,
        help="Override the previous release tag (default: GH releases/latest)",
    )
    args = parser.parse_args()
    sys.exit(run(args.source, args.candidate, previous_tag=args.previous_tag))


if __name__ == "__main__":
    main()
