"""Release validator — enforces coverage invariants against
``metrics/bake-metrics.parquet`` before a release is blessed.

Two gates (both hard-failing):

1. **Regression gate** — current release's ``actual_count`` per
   ``(source, tier)`` must be ≥ ``--min-ratio`` × previous release's
   count. This is the check that catches the gpuopen-1k 2234 → 10
   scenario that shipped in v2026.04.0.

2. **Cross-tier parity** — for a given release × source, tier counts
   must be within ``--parity-min-ratio`` of the tier with the largest
   count. Catches cases where a single tier regressed uniformly and
   the previous release already had the bug (so the regression gate
   alone wouldn't catch it).

Usage::

    python -m scripts.validate_release \\
        --metrics metrics/bake-metrics.parquet \\
        --tag v2026.04.1 \\
        [--min-ratio 0.95] \\
        [--parity-min-ratio 0.80] \\
        [--exclude-tier-prefix ktx2-]

Exits 0 on success, 1 on any violation. Wired as a blocking step in
``.github/workflows/release-validate.yml``.

See #88 for the QA design.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

DEFAULT_MIN_RATIO = 0.95
DEFAULT_PARITY_MIN_RATIO = 0.80
DEFAULT_EXCLUDE_TIER_PREFIXES = ("ktx2-",)


__all__ = [
    "baked_ids_from_rowmap_dir",
    "find_catalog_violations",
    "find_regressions",
    "find_tier_parity_violations",
    "load_waivers",
    "main",
]


def _load_aggregate_rows(path: Path) -> list[dict[str, Any]]:
    """Load only ``category='__all__'`` rows — these are what the gates
    operate on (per-category rows are informational, not gating)."""
    table = pq.read_table(path)
    rows = table.to_pylist()
    return [r for r in rows if r.get("category") == "__all__"]


def find_regressions(
    metrics_path: Path,
    *,
    current_tag: str,
    min_ratio: float = DEFAULT_MIN_RATIO,
) -> list[dict[str, Any]]:
    """Return a list of (source, tier) pairs where the current release's
    ``actual_count`` dropped below ``min_ratio`` × the previous release.

    Previous release = the newest ``release_tag`` older than
    ``current_tag`` (by timestamp) that has a row for the same (source, tier).
    If no previous row exists, nothing is reported — a first-ever release
    has nothing to regress against.
    """
    rows = _load_aggregate_rows(metrics_path)

    # Group rows by (source, tier) with timestamps so we can find the
    # "previous" row relative to current_tag.
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        key = (r["source"], r["tier"])
        by_key.setdefault(key, []).append(r)

    regressions: list[dict[str, Any]] = []
    for (source, tier), group in by_key.items():
        # Sort by (release_tag, timestamp) — release_tag is the primary
        # ordering (CalVer/SemVer), timestamp is the tiebreaker for
        # re-bakes within the same tag.
        group.sort(key=lambda r: (r["release_tag"], r["timestamp"]))
        current = next((r for r in group if r["release_tag"] == current_tag), None)
        if current is None:
            continue
        # Previous = most recent row for a DIFFERENT, earlier tag.
        prior = [r for r in group if r["release_tag"] < current_tag]
        if not prior:
            continue
        previous = prior[-1]
        if previous["actual_count"] <= 0:
            continue
        ratio = current["actual_count"] / previous["actual_count"]
        if ratio < min_ratio:
            regressions.append(
                {
                    "source": source,
                    "tier": tier,
                    "current_tag": current_tag,
                    "previous_tag": previous["release_tag"],
                    "actual_count": current["actual_count"],
                    "previous_count": previous["actual_count"],
                    "ratio": ratio,
                }
            )
    return regressions


def find_tier_parity_violations(
    metrics_path: Path,
    *,
    release_tag: str,
    min_ratio: float = DEFAULT_PARITY_MIN_RATIO,
    exclude_tier_prefixes: tuple[str, ...] = DEFAULT_EXCLUDE_TIER_PREFIXES,
) -> list[dict[str, Any]]:
    """Return tiers whose count is far below the leader within the same
    source. Asymmetry beyond ``min_ratio`` means something is wrong with
    that specific tier's bake — the bug the gpuopen-1k case represents.
    """
    rows = _load_aggregate_rows(metrics_path)
    rows = [r for r in rows if r["release_tag"] == release_tag]
    rows = [r for r in rows if not any(r["tier"].startswith(p) for p in exclude_tier_prefixes)]

    by_source: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)

    violations: list[dict[str, Any]] = []
    for source, tiers in by_source.items():
        if len(tiers) < 2:
            continue
        leader = max(r["actual_count"] for r in tiers)
        if leader <= 0:
            continue
        for r in tiers:
            ratio = r["actual_count"] / leader
            if ratio < min_ratio:
                violations.append(
                    {
                        "source": source,
                        "tier": r["tier"],
                        "release_tag": release_tag,
                        "actual_count": r["actual_count"],
                        "leader_count": leader,
                        "ratio": ratio,
                    }
                )
    return violations


# ── Phase 2: upstream-catalog contract ──────────────────────────


def load_waivers(path: Path) -> dict[tuple[str, str], set[str]]:
    """Parse ``waived.yaml`` → ``{(source, tier): {id, ...}}``.

    Missing or empty file returns ``{}`` — waivers are optional. YAML
    shape::

        polyhaven:
          "2k":
            - id_not_available_upstream_at_2k
        gpuopen:
          "1k":
            - special_case
    """
    path = Path(path)
    if not path.exists():
        return {}
    raw = path.read_text()
    if not raw.strip():
        return {}

    # PyYAML is the only external dep introduced here; fall back to a
    # minimal parser if it's not installed (keeps the validator usable
    # in stripped-down CI containers).
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw) or {}
    except ImportError:  # pragma: no cover — tested envs install pyyaml
        data = _minimal_yaml(raw)

    out: dict[tuple[str, str], set[str]] = {}
    for source, tiers in (data or {}).items():
        for tier, ids in (tiers or {}).items():
            key = (str(source), str(tier))
            out[key] = set(ids or [])
    return out


def _minimal_yaml(s: str) -> dict:
    """Bare-bones YAML subset parser for waived.yaml (source → tier → list).

    Not a full YAML; only handles the exact two-level-plus-list shape we
    document. Used only when PyYAML isn't installed.
    """
    out: dict = {}
    current_source = None
    current_tier = None
    for line in s.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0 and stripped.endswith(":"):
            current_source = stripped[:-1].strip().strip('"')
            out[current_source] = {}
            current_tier = None
        elif indent == 2 and stripped.endswith(":"):
            current_tier = stripped[:-1].strip().strip('"')
            out[current_source][current_tier] = []
        elif stripped.startswith("- ") and current_tier is not None:
            out[current_source][current_tier].append(stripped[2:].strip().strip('"'))
    return out


def find_catalog_violations(
    *,
    upstream: dict[str, set[str]],
    baked_per_tier: dict[tuple[str, str], set[str]],
    waivers: dict[tuple[str, str], set[str]],
) -> list[dict[str, Any]]:
    """Compare baked IDs to ``upstream \\ waivers`` per (source, tier).

    Returns one record per violating (source, tier) with the missing
    and extra IDs. Sources not present in ``upstream`` are skipped —
    the snapshot is incomplete for that source, not the bake.
    """
    violations: list[dict[str, Any]] = []
    for (source, tier), baked in sorted(baked_per_tier.items()):
        if source not in upstream:
            continue
        expected = upstream[source] - waivers.get((source, tier), set())
        missing = expected - baked
        extras = baked - upstream[source]
        if missing or extras:
            violations.append(
                {
                    "source": source,
                    "tier": tier,
                    "missing": missing,
                    "extras": extras,
                }
            )
    return violations


def baked_ids_from_rowmap_dir(rowmap_dir: Path) -> dict[tuple[str, str], set[str]]:
    """Scan ``rowmap_dir`` for ``{source}-{tier}-{category}-rowmap.json``
    files, merge per (source, tier), return ``{(source, tier): {ids}}``.

    Used at validation time to produce ``baked_per_tier`` for
    :func:`find_catalog_violations`.
    """
    import json as _json
    import re

    pattern = re.compile(r"^(?P<source>[a-z0-9]+)-(?P<tier>[a-z0-9-]+?)-[a-z0-9]+-rowmap\.json$")
    out: dict[tuple[str, str], set[str]] = {}
    for rmp in Path(rowmap_dir).glob("*-rowmap.json"):
        m = pattern.match(rmp.name)
        if not m:
            continue
        key = (m.group("source"), m.group("tier"))
        data = _json.loads(rmp.read_text())
        ids = set(data.get("materials", {}).keys())
        out.setdefault(key, set()).update(ids)
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns 0 on clean validation, non-zero on any
    violation."""
    p = argparse.ArgumentParser(prog="validate-release")
    p.add_argument("--metrics", required=True, help="path to bake-metrics.parquet")
    p.add_argument("--tag", required=True, help="release tag to validate")
    p.add_argument(
        "--min-ratio",
        type=float,
        default=DEFAULT_MIN_RATIO,
        help=f"regression threshold (default {DEFAULT_MIN_RATIO})",
    )
    p.add_argument(
        "--parity-min-ratio",
        type=float,
        default=DEFAULT_PARITY_MIN_RATIO,
        help=f"cross-tier parity threshold (default {DEFAULT_PARITY_MIN_RATIO})",
    )
    p.add_argument(
        "--exclude-tier-prefix",
        action="append",
        default=list(DEFAULT_EXCLUDE_TIER_PREFIXES),
        help="tier prefixes to skip in parity check (repeatable)",
    )
    args = p.parse_args(argv)

    metrics_path = Path(args.metrics)
    if not metrics_path.exists():
        print(f"ERROR: metrics file not found: {metrics_path}", file=sys.stderr)
        return 2

    regressions = find_regressions(metrics_path, current_tag=args.tag, min_ratio=args.min_ratio)
    violations = find_tier_parity_violations(
        metrics_path,
        release_tag=args.tag,
        min_ratio=args.parity_min_ratio,
        exclude_tier_prefixes=tuple(args.exclude_tier_prefix),
    )

    if not regressions and not violations:
        print(f"validate-release {args.tag}: clean")
        return 0

    if regressions:
        print("\n=== regressions vs previous release ===")
        for r in regressions:
            print(
                f"  {r['source']}/{r['tier']}: {r['actual_count']} "
                f"(was {r['previous_count']} in {r['previous_tag']}, "
                f"ratio={r['ratio']:.3f}, min={args.min_ratio})"
            )

    if violations:
        print(f"\n=== cross-tier parity violations in {args.tag} ===")
        for v in violations:
            print(
                f"  {v['source']}/{v['tier']}: {v['actual_count']} "
                f"(leader={v['leader_count']}, ratio={v['ratio']:.3f}, "
                f"min={args.parity_min_ratio})"
            )

    return 1


if __name__ == "__main__":
    sys.exit(main())
