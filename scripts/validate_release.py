"""Release validator — enforces coverage invariants against the
per-file substrate metrics parquet (#263 phase C).

Four gates (all hard-failing):

1. **Regression gate** — current release's per-(source, tier) material
   total must be >= ``--min-ratio`` x previous release's total. Catches
   the v2026.04.0 ``gpuopen-1k 2234 -> 10`` scenario.

2. **Cross-tier parity** — for a given release x source, tier totals
   must be within ``--parity-min-ratio`` of the tier with the largest
   total. Catches uniformly regressed tiers when the previous release
   already had the bug (so the regression gate alone wouldn't fire).

3. **Manifest-asset reachability** (#293, --from-hf only) — every asset
   the ``release-manifest.json`` *declares* (source catalog, bundled
   ``mtlx``, each ``complete`` tier's ``.tier_complete`` sentinel) must
   HEAD-200 on HF. Catches the vertical-completeness class the count
   gates are blind to: "manifest claims X but X is 404 / never uploaded"
   (#290 pbr, #292 mtlx). Manifest-declared only → no false positives;
   only a definitive 404 fails (transient HF statuses are skipped).

4. **Tier completeness** (#436, --from-hf + ``--check-completeness``) — every
   ``(source, tier)`` cell the release **matrix** DECLARES (``release_matrix``
   bake + ``derive_matrix`` downscale + ``ktx2_matrix`` transcode cells) must
   be present and ``complete`` in the manifest. Closes the "wanted (matrix) vs
   got (manifest)" gap the other gates miss: gate 1 only fires on tiers that
   shrank vs the *previous* release, gate 2 EXCLUDES ``ktx2-`` tiers, and gate
   3 only checks manifest-*declared* assets — so a matrix cell that silently
   never baked (the ``ktx2-512`` never-derived #436 scenario) is invisible to
   all three. **Opt-in**: it's a whole-release invariant, valid only after
   every phase (bake + derive + ktx2) has run — enabled in release-validate,
   not in bake.yml's per-phase post-bake validate (which sees only the native
   bake tier and would false-fire on the not-yet-derived tiers).

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
    "find_manifest_asset_violations",
    "find_regressions",
    "find_regressions_from_hf",
    "find_tier_completeness_violations",
    "find_tier_parity_violations",
    "find_tier_parity_violations_from_hf",
    "load_aggregated_counts",
    "load_waivers",
    "main",
    "wanted_cells_for_line",
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

    mat-vis#344: iterate the UNION of (source, tier) keys across both
    current and previous, not just current's keys. A tier present in
    previous but completely absent from current (``actual_count=0``)
    is the worst regression class — every consumer of that tier
    silently breaks. The pre-#344 code skipped these via
    ``if current is None: continue``.
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
        prior = [r for r in group if r["release_tag"] < current_tag]
        if not prior:
            continue  # first-ever release; nothing to regress against
        previous = prior[-1]
        if previous["actual_count"] <= 0:
            continue  # previous was empty (free-pass per existing semantics)
        # mat-vis#344: tier-missing-from-current → actual_count = 0,
        # ratio = 0, which violates any min_ratio > 0.
        curr_count = current["actual_count"] if current is not None else 0
        ratio = curr_count / previous["actual_count"]
        if ratio < min_ratio:
            regressions.append(
                {
                    "source": source,
                    "tier": tier,
                    "current_tag": current_tag,
                    "previous_tag": previous["release_tag"],
                    "actual_count": curr_count,
                    "previous_count": previous["actual_count"],
                    "ratio": ratio,
                    # mat-vis#344: distinguishes "tier shrunk" from
                    # "tier vanished entirely" for operator triage.
                    "kind": "tier_missing" if current is None else "count_drop",
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


def _fetch_release_manifest(api: Any, repo_id: str, release_tag: str) -> dict:
    """Fetch + parse ``release-manifest.json`` from HF. ``{}`` if missing."""
    try:
        p = api.hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=release_tag,
            filename="release-manifest.json",
        )
        return json.loads(Path(p).read_text())
    except Exception:  # noqa: BLE001 — missing/unparseable manifest → empty
        return {}


def _head_status(url: str) -> int:
    """HEAD ``url`` and return the final HTTP status (following redirects,
    e.g. HF's LFS 302). ``0`` on a network-level failure (treated as
    inconclusive by callers, never a violation)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 — DNS/conn/timeout → inconclusive
        return 0


def find_manifest_asset_violations(
    api: Any,
    repo_id: str,
    release_tag: str,
    *,
    head_fn: Any = None,
) -> list[dict[str, Any]]:
    """#293: assert every asset the release-manifest *declares* is actually
    reachable on HF (HEAD → 200). Catches the vertical-completeness bug class
    the count gates are blind to — "manifest claims X but X is 404 / was never
    uploaded" (#290 pbr, #292 mtlx).

    Manifest-declared only, so **no false positives**: it checks the source
    catalog (``<src>.json``), the optional bundled ``mtlx`` (``<src>-mtlx.json``,
    #292), and the ``.tier_complete`` sentinel of every tier the manifest marks
    ``complete``. Only a definitive **404** is a violation; transient/network
    statuses (429/5xx/0) are inconclusive and skipped so an HF hiccup can't
    spuriously red the daily cron.

    ``head_fn`` is injectable for testing (default :func:`_head_status`).
    """
    manifest = _fetch_release_manifest(api, repo_id, release_tag)
    if not manifest:
        return []
    head = head_fn or _head_status
    base = f"https://huggingface.co/datasets/{repo_id}/resolve/{release_tag}"

    violations: list[dict[str, Any]] = []
    for source, entry in (manifest.get("sources") or {}).items():
        entry = entry or {}
        assets: list[tuple[str, str | None, str]] = []
        if entry.get("catalog"):
            assets.append(("catalog", None, str(entry["catalog"])))
        if entry.get("mtlx"):
            assets.append(("mtlx", None, str(entry["mtlx"])))
        for tier, tinfo in (entry.get("tiers") or {}).items():
            if (tinfo or {}).get("complete"):
                assets.append(("tier_complete", tier, f"{source}/{tier}/.tier_complete"))

        for feature, tier, path in assets:
            url = f"{base}/{path}"
            status = head(url)
            if status == 404:
                violations.append(
                    {"source": source, "feature": feature, "tier": tier, "url": url}
                )
    return violations


# ── #436: matrix-vs-manifest tier completeness (wanted vs got) ──


def _line_for_tag(tag: str) -> str | None:
    """Extract the CalVer line prefix (``vYYYY.MM``) from a release tag.

    ``v2026.04.3`` → ``v2026.04``; ``v2026.04.99-tst-full-369`` → ``v2026.04``.
    Returns ``None`` if the tag doesn't start with a ``vYYYY.MM`` prefix (the
    completeness gate can't map it to a matrix line, so it's skipped).
    """
    import re

    m = re.match(r"^(v\d{4}\.\d{2})", tag)
    return m.group(1) if m else None


def wanted_cells_for_line(line: str) -> set[tuple[str, str]]:
    """Union of ``(source, tier)`` cells every phase of the release declares
    for ``line`` — the canonical "wanted" set the manifest is reconciled
    against.

    Spans all three declaration phases so a gap in any of them is caught:

    - ``release_matrix`` — bake cells (``(source, tier)``, the fetched tier);
    - ``derive_matrix``  — PNG downscale cells (512 / 256 / 128, via
      ``produces``);
    - ``ktx2_matrix``    — transcode cells (``ktx2-<tier>``, via ``produces``).

    Returns an empty set if the line is unknown to a matrix (``KeyError``) or
    the baker package isn't importable (``ImportError``); the caller treats
    empty as "nothing to reconcile".
    """
    cells: set[tuple[str, str]] = set()
    try:
        from mat_vis_baker.release_matrix import get_release as _get_bake

        for c in _get_bake(line).cells:
            cells.add((c.source, c.tier))
    except (KeyError, ImportError):
        pass
    for _mod in ("derive_matrix", "ktx2_matrix"):
        try:
            import importlib

            get_release = importlib.import_module(f"mat_vis_baker.{_mod}").get_release
            for c in get_release(line).cells:
                cells.add((c.produces.source, c.produces.tier))
        except (KeyError, ImportError):
            pass
    return cells


def find_tier_completeness_violations(
    manifest: dict,
    wanted_cells: Any,
) -> list[dict[str, Any]]:
    """#436: assert every matrix-declared ``(source, tier)`` cell is present
    and ``complete`` in the release manifest.

    Pure function — the caller supplies the fetched ``manifest`` and the
    ``wanted_cells`` iterable (see :func:`wanted_cells_for_line`). Emits one
    violation per cell that is missing or incomplete:

    - ``source_missing``   — the manifest has no entry for the source at all;
    - ``tier_missing``     — source present, but the matrix-declared tier is
      absent from its ``tiers`` map (the ``ktx2-512`` #436 case);
    - ``tier_incomplete``  — tier present but not marked ``complete``.
    """
    sources = (manifest.get("sources") or {}) if manifest else {}
    violations: list[dict[str, Any]] = []
    for source, tier in sorted(set(wanted_cells)):
        src_entry = sources.get(source) or {}
        tinfo = (src_entry.get("tiers") or {}).get(tier)
        if tinfo is None:
            kind = "tier_missing" if source in sources else "source_missing"
            violations.append({"source": source, "tier": tier, "kind": kind})
        elif not (tinfo or {}).get("complete"):
            violations.append({"source": source, "tier": tier, "kind": "tier_incomplete"})
    return violations


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

    # mat-vis#344: iterate the UNION of keys, not just current's. A
    # tier present in previous but missing from current is the
    # worst-case regression — every consumer of that tier silently
    # breaks. Pre-#344 the loop iterated current_baked.items() and
    # `prev_ids = previous_baked.get(...); if not prev_ids: continue`,
    # so prev-only keys never entered the loop.
    all_keys = set(current_baked.keys()) | set(previous_baked.keys())
    regressions: list[dict[str, Any]] = []
    for source, tier in sorted(all_keys):
        prev_ids = previous_baked.get((source, tier)) or set()
        curr_ids = current_baked.get((source, tier)) or set()
        prev_count = len(prev_ids)
        if prev_count <= 0:
            continue  # nothing to regress against (newly-added tier)
        curr_count = len(curr_ids)
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
                    # mat-vis#344: tier-vanished is worth flagging
                    # distinctly so the operator's triage starts in the
                    # right place ("did upstream go away?" vs "did the
                    # bake plan get pruned?").
                    "kind": (
                        "tier_missing" if (source, tier) not in current_baked else "count_drop"
                    ),
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
    p.add_argument(
        "--check-completeness",
        action="store_true",
        help=(
            "#436: enable the matrix-vs-manifest tier-completeness gate. OFF by "
            "default because it's a WHOLE-RELEASE invariant — it must only run "
            "after every phase (bake + derive + ktx2) has produced its tiers. "
            "Enable it in release-validate (the finished-release validator), NOT "
            "in bake.yml's per-phase post-bake validate, which sees only the "
            "freshly-baked native tier and would false-fire on the not-yet-"
            "derived tiers."
        ),
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
    # #293: vertical-completeness — every manifest-declared asset must be
    # reachable (catches "declared but 404" that count gates miss).
    asset_violations = find_manifest_asset_violations(
        api, repo_id=args.repo_id, release_tag=args.release_tag
    )
    # #436: matrix-vs-manifest tier completeness — every (source, tier) the
    # release matrix DECLARES must be present + complete in the manifest.
    # Reconciles the "wanted" (matrix) side against "got" (manifest). OPT-IN
    # (--check-completeness): a WHOLE-RELEASE invariant, valid only after every
    # phase (bake + derive + ktx2) has produced its tiers — so it runs in
    # release-validate, NOT bake.yml's per-phase post-bake validate (which sees
    # only the native bake tier and would false-fire).
    completeness_violations: list[dict[str, Any]] = []
    if args.check_completeness:
        line = _line_for_tag(args.release_tag)
        if line:
            wanted = wanted_cells_for_line(line)
            if wanted:
                manifest = _fetch_release_manifest(api, args.repo_id, args.release_tag)
                completeness_violations = find_tier_completeness_violations(manifest, wanted)
            else:
                print(
                    f"validate-release: no matrix cells for line {line!r} "
                    f"(tier-completeness gate skipped)",
                    file=sys.stderr,
                )
    return _report(args, regressions, violations, asset_violations, completeness_violations)


def _report(
    args: argparse.Namespace,
    regressions: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    asset_violations: list[dict[str, Any]] | None = None,
    completeness_violations: list[dict[str, Any]] | None = None,
) -> int:
    """Shared output writer + return-code computation. Centralised so
    both --metrics and --from-hf modes emit the same operator-facing
    text format."""
    asset_violations = asset_violations or []
    completeness_violations = completeness_violations or []
    if not regressions and not violations and not asset_violations and not completeness_violations:
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

    if asset_violations:
        print(f"\n=== manifest-declared assets missing (404) in {args.release_tag} ===")
        for a in asset_violations:
            where = f"{a['source']}/{a['feature']}"
            if a.get("tier"):
                where += f"/{a['tier']}"
            print(f"  {where}: {a['url']}")

    if completeness_violations:
        print(
            f"\n=== matrix-declared tiers missing/incomplete in {args.release_tag} "
            f"(wanted vs got, #436) ==="
        )
        for c in completeness_violations:
            print(f"  {c['source']}/{c['tier']}: {c['kind']}")

    return 1


if __name__ == "__main__":
    sys.exit(main())
