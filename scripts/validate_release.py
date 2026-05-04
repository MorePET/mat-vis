"""Release validator — enforces coverage invariants against the
per-file substrate metrics parquet (#263 phase C).

Two gates (both hard-failing):

1. **Regression gate** — current release's per-(source, tier) material
   total must be >= ``--min-ratio`` x previous release's total. Catches
   the v2026.04.0 ``gpuopen-1k 2234 -> 10`` scenario.

2. **Cross-tier parity** — for a given release x source, tier totals
   must be within ``--parity-min-ratio`` of the tier with the largest
   total. Catches uniformly regressed tiers when the previous release
   already had the bug (so the regression gate alone wouldn't fire).

Schema-autodetect: the underlying loader handles both the v0.5.x
``bake-metrics.parquet`` (one row per release-tag with an
``actual_count`` column) and the v0.6+ ``per-file-metrics.parquet``
(many rows per release-tag — one per HF batch commit, summed per
``(release_tag, source, tier)`` to derive the material total).

Optionally cross-checks against the live HF substrate via
:func:`baked_ids_from_release_manifest` — useful as a sanity check
that the metrics parquet matches the actual on-HF state.

Usage::

    python -m scripts.validate_release \\
        --metrics metrics/per-file-metrics.parquet \\
        --release-tag v2026.04.2 \\
        [--repo-id gerchowl/mat-vis] \\
        [--min-ratio 0.95] \\
        [--parity-min-ratio 0.80] \\
        [--exclude-tier-prefix ktx2-]

Exits 0 on success, 1 on any violation, 2 on a setup error (missing
metrics file). Wired as a blocking step in
``.github/workflows/release-validate.yml`` and as a final job in
``bake.yml``.

See #88 for the original QA design and #263 for the per-file port.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

DEFAULT_MIN_RATIO = 0.95
DEFAULT_PARITY_MIN_RATIO = 0.80
DEFAULT_EXCLUDE_TIER_PREFIXES = ("ktx2-",)


__all__ = [
    "baked_ids_from_release_manifest",
    "find_catalog_violations",
    "find_regressions",
    "find_regressions_from_hf",
    "find_tier_parity_violations",
    "find_tier_parity_violations_from_hf",
    "load_aggregated_counts",
    "load_waivers",
    "main",
]


# ── Loader (handles both v0.5.x bake-metrics + v0.6+ per-file-metrics) ──


def load_aggregated_counts(path: Path) -> list[dict[str, Any]]:
    """Return one record per ``(release_tag, source, tier)`` with an
    ``actual_count`` field — autodetects the input parquet's schema.

    For v0.6+ per-file metrics rows, sums ``materials_committed`` over
    all ``operation='bake'`` batches with the same key. Derive ops are
    excluded so the validator compares like with like (a tier's
    material total reflects what the bake produced; derives copy
    materials, not add them).

    For the legacy v0.5.x ``bake-metrics.parquet`` (with a ``category``
    column), filters to ``category='__all__'`` and treats each row as
    the aggregate for its key. Lets the validator query historical
    pre-v0.6 metrics without a migration step.
    """
    table = pq.read_table(path)
    schema_names = set(table.schema.names)

    if "materials_committed" in schema_names:
        # v0.6+ per-file metrics: sum bake-batch counts per key.
        rows = table.to_pylist()
        agg: dict[tuple[str, str, str], dict[str, Any]] = {}
        for r in rows:
            if r.get("operation") != "bake":
                continue
            key = (r["release_tag"], r["source"], r["tier"])
            slot = agg.setdefault(
                key,
                {
                    "release_tag": r["release_tag"],
                    "source": r["source"],
                    "tier": r["tier"],
                    "actual_count": 0,
                    # Newest timestamp wins as the row's representative —
                    # used as a tiebreaker for re-bakes within a tag.
                    "timestamp": r.get("timestamp_utc", ""),
                },
            )
            slot["actual_count"] += int(r["materials_committed"])
            ts = r.get("timestamp_utc", "")
            if ts > slot["timestamp"]:
                slot["timestamp"] = ts
        return list(agg.values())

    if "category" in schema_names:
        # Legacy v0.5.x: __all__ rows are the per-(source, tier) aggregate.
        rows = table.to_pylist()
        return [r for r in rows if r.get("category") == "__all__"]

    raise ValueError(
        f"unrecognised metrics schema in {path}: {sorted(schema_names)} "
        "(expected per-file 'materials_committed' or v0.5.x 'category')"
    )


def find_regressions(
    metrics_path: Path,
    *,
    current_tag: str,
    min_ratio: float = DEFAULT_MIN_RATIO,
) -> list[dict[str, Any]]:
    """Return ``(source, tier)`` pairs where the current release's
    material count dropped below ``min_ratio`` x the previous release.

    Previous release = the newest ``release_tag`` strictly less than
    ``current_tag`` (lexicographic — fine for CalVer ``vYYYY.MM.N``).
    No previous row → no regression possible (first-ever release).
    """
    rows = load_aggregated_counts(metrics_path)

    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        key = (r["source"], r["tier"])
        by_key.setdefault(key, []).append(r)

    regressions: list[dict[str, Any]] = []
    for (source, tier), group in by_key.items():
        group.sort(key=lambda r: (r["release_tag"], r.get("timestamp", "")))
        current = next((r for r in group if r["release_tag"] == current_tag), None)
        if current is None:
            continue
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
    """Return tiers whose count is far below the source's leader.
    Asymmetry beyond ``min_ratio`` means something is wrong with that
    specific tier's bake — the v2026.04.0 gpuopen-1k pattern."""
    rows = load_aggregated_counts(metrics_path)
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


# ── Layer 2: live HF cross-check ────────────────────────────────


def baked_ids_from_release_manifest(
    api: Any,
    repo_id: str,
    release_tag: str,
) -> dict[tuple[str, str], set[str]]:
    """Return ``{(source, tier): {material_id, ...}}`` by reading the
    ``release-manifest.json`` from HF then enumerating the per-file tree
    under each manifested ``<source>/<tier>/`` prefix.

    Used by the optional ``--repo-id`` mode of the validator to
    cross-check the metrics parquet against what's actually on HF —
    catches the case where the metrics file says one thing but the
    substrate disagrees (e.g. a re-bake without a metrics append, or
    a metrics append without the underlying commit).

    Returns an empty dict if the manifest can't be fetched (e.g. a
    release tag that doesn't exist) — the caller decides whether to
    treat that as a fatal error or skip the cross-check.
    """
    # Manifest first: it tells us which (source, tier) pairs to enumerate.
    try:
        manifest_path = api.hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=release_tag,
            filename="release-manifest.json",
        )
        manifest = json.loads(Path(manifest_path).read_text())
    except Exception:  # noqa: BLE001 — missing manifest → empty result
        return {}

    out: dict[tuple[str, str], set[str]] = {}
    sources = manifest.get("sources", {}) or {}
    for source, src_entry in sources.items():
        tiers = (src_entry or {}).get("tiers", {}) or {}
        for tier in tiers:
            prefix = f"{source}/{tier}/"
            mids: set[str] = set()
            try:
                for entry in api.list_repo_tree(
                    repo_id=repo_id,
                    repo_type="dataset",
                    revision=release_tag,
                    path_in_repo=prefix.rstrip("/"),
                    recursive=True,
                ):
                    path = getattr(entry, "path", None)
                    if not path or not path.startswith(prefix):
                        continue
                    rel = path[len(prefix) :]  # noqa: E203
                    parts = rel.split("/", 1)
                    # Ignore .tier_complete sentinels and other top-level
                    # files that aren't material directories.
                    if len(parts) == 2 and parts[0] and not parts[0].startswith("."):
                        mids.add(parts[0])
            except Exception:  # noqa: BLE001 — partial result is still useful
                pass
            out[(source, tier)] = mids
    return out


# ── Live HF gate (no metrics parquet needed) ──────────────────


def find_regressions_from_hf(
    api: Any,
    *,
    repo_id: str,
    current_tag: str,
    previous_tag: str,
    min_ratio: float = DEFAULT_MIN_RATIO,
) -> list[dict[str, Any]]:
    """Compare per-(source, tier) material counts between two releases
    by querying the live HF substrate. Same gate as
    :func:`find_regressions` but operates on the source of truth (the
    actual files on HF) instead of the metrics parquet — useful when
    the parquet hasn't been auto-committed yet, or as a cross-check
    against tampering / drift.

    Caller picks ``previous_tag`` (e.g. the most-recent prior release
    tag from ``git tag --sort=-v:refname``). If the previous tag's
    manifest is missing, returns an empty list — first-ever release
    has nothing to regress against.
    """
    current_baked = baked_ids_from_release_manifest(api, repo_id, current_tag)
    previous_baked = baked_ids_from_release_manifest(api, repo_id, previous_tag)
    if not previous_baked:
        return []

    regressions: list[dict[str, Any]] = []
    for (source, tier), curr_ids in current_baked.items():
        prev_ids = previous_baked.get((source, tier))
        if not prev_ids:
            continue
        prev_count = len(prev_ids)
        curr_count = len(curr_ids)
        if prev_count <= 0:
            continue
        ratio = curr_count / prev_count
        if ratio < min_ratio:
            regressions.append(
                {
                    "source": source,
                    "tier": tier,
                    "current_tag": current_tag,
                    "previous_tag": previous_tag,
                    "actual_count": curr_count,
                    "previous_count": prev_count,
                    "ratio": ratio,
                }
            )
    return regressions


def find_tier_parity_violations_from_hf(
    api: Any,
    *,
    repo_id: str,
    release_tag: str,
    min_ratio: float = DEFAULT_PARITY_MIN_RATIO,
    exclude_tier_prefixes: tuple[str, ...] = DEFAULT_EXCLUDE_TIER_PREFIXES,
) -> list[dict[str, Any]]:
    """Cross-tier parity gate against the live HF substrate (no metrics
    parquet)."""
    baked = baked_ids_from_release_manifest(api, repo_id, release_tag)
    by_source: dict[str, list[tuple[str, int]]] = {}
    for (source, tier), ids in baked.items():
        if any(tier.startswith(p) for p in exclude_tier_prefixes):
            continue
        by_source.setdefault(source, []).append((tier, len(ids)))

    violations: list[dict[str, Any]] = []
    for source, tiers in by_source.items():
        if len(tiers) < 2:
            continue
        leader = max(c for _, c in tiers)
        if leader <= 0:
            continue
        for tier, count in tiers:
            ratio = count / leader
            if ratio < min_ratio:
                violations.append(
                    {
                        "source": source,
                        "tier": tier,
                        "release_tag": release_tag,
                        "actual_count": count,
                        "leader_count": leader,
                        "ratio": ratio,
                    }
                )
    return violations


# ── Phase 2 carry-over: upstream-catalog contract (unused today) ──


def load_waivers(path: Path) -> dict[tuple[str, str], set[str]]:
    """Parse ``waived.yaml`` → ``{(source, tier): {id, ...}}``.

    Missing or empty file returns ``{}`` — waivers are optional.
    """
    path = Path(path)
    if not path.exists():
        return {}
    raw = path.read_text()
    if not raw.strip():
        return {}

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
    """Bare-bones YAML subset parser (source -> tier -> list)."""
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
    """Compare baked IDs to ``upstream \\ waivers`` per (source, tier)."""
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


# ── CLI ─────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns 0 clean, 1 on any violation, 2 on setup
    error (missing file / invalid arg combo).

    Two modes:

    - **Parquet mode** (``--metrics PATH``): regression + parity gates
      read from the per-file metrics parquet committed to the repo.
      Used by the scheduled drift monitor and operator dispatches.

    - **Live HF mode** (``--from-hf --repo-id``): regression gate
      compares the current tag against ``--previous-tag`` by querying
      the HF substrate directly. Used by the bake.yml post-bake gate
      because the metrics parquet may not be committed yet.
    """
    p = argparse.ArgumentParser(prog="validate-release")
    p.add_argument(
        "--metrics",
        default=None,
        help="path to per-file-metrics.parquet (parquet mode; mutually exclusive with --from-hf)",
    )
    p.add_argument(
        "--from-hf",
        action="store_true",
        help=(
            "Live HF mode: query the substrate directly via "
            "release-manifest.json. Requires --repo-id and --previous-tag."
        ),
    )
    # --release-tag is the canonical name; --tag stays as a back-compat alias.
    p.add_argument(
        "--release-tag",
        "--tag",
        dest="release_tag",
        required=True,
        help="release tag to validate (e.g. v2026.04.2)",
    )
    p.add_argument(
        "--previous-tag",
        default=None,
        help="previous release tag for --from-hf comparison (omit on first-ever release)",
    )
    p.add_argument(
        "--repo-id",
        default=None,
        help="HF dataset repo for --from-hf mode",
    )
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

    if args.from_hf:
        if not args.repo_id:
            print("ERROR: --from-hf requires --repo-id", file=sys.stderr)
            return 2
        return _run_from_hf(args)

    if not args.metrics:
        print("ERROR: --metrics PATH or --from-hf is required", file=sys.stderr)
        return 2

    metrics_path = Path(args.metrics)
    if not metrics_path.exists():
        print(f"ERROR: metrics file not found: {metrics_path}", file=sys.stderr)
        return 2

    regressions = find_regressions(
        metrics_path, current_tag=args.release_tag, min_ratio=args.min_ratio
    )
    violations = find_tier_parity_violations(
        metrics_path,
        release_tag=args.release_tag,
        min_ratio=args.parity_min_ratio,
        exclude_tier_prefixes=tuple(args.exclude_tier_prefix),
    )

    return _report(args, regressions, violations)


def _run_from_hf(args: argparse.Namespace) -> int:
    """Live HF mode body. Imports HfApi lazily so the parquet-mode code
    path doesn't pull huggingface_hub when it isn't needed."""
    from huggingface_hub import HfApi  # local import keeps the surface lean

    api = HfApi()
    if args.previous_tag:
        regressions = find_regressions_from_hf(
            api,
            repo_id=args.repo_id,
            current_tag=args.release_tag,
            previous_tag=args.previous_tag,
            min_ratio=args.min_ratio,
        )
    else:
        regressions = []
    violations = find_tier_parity_violations_from_hf(
        api,
        repo_id=args.repo_id,
        release_tag=args.release_tag,
        min_ratio=args.parity_min_ratio,
        exclude_tier_prefixes=tuple(args.exclude_tier_prefix),
    )
    return _report(args, regressions, violations)


def _report(
    args: argparse.Namespace,
    regressions: list[dict[str, Any]],
    violations: list[dict[str, Any]],
) -> int:
    """Shared output writer + return-code computation. Centralised so
    both --metrics and --from-hf modes emit the same operator-facing
    text format."""
    if not regressions and not violations:
        mode = "from-hf" if args.from_hf else "parquet"
        print(f"validate-release {args.release_tag} ({mode}): clean")
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
        print(f"\n=== cross-tier parity violations in {args.release_tag} ===")
        for v in violations:
            print(
                f"  {v['source']}/{v['tier']}: {v['actual_count']} "
                f"(leader={v['leader_count']}, ratio={v['ratio']:.3f}, "
                f"min={args.parity_min_ratio})"
            )

    return 1


if __name__ == "__main__":
    sys.exit(main())
