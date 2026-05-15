"""Check substrate coverage — wanted vs actual on HF.

Compares the canonical release matrix (which (source, tier) cells
should exist) and upstream material catalogs (which material IDs
each source advertises) against what actually lives on the HF
substrate at a given release tag.

Two coverage levels:

1. **Tier-level:** does each expected (source, tier) cell exist on
   HF with the expected material count?
2. **Material-level:** for each cell, which specific IDs are missing
   or unexpected?

Usage::

    python -m scripts.check_substrate_coverage \\
        --release-tag v2026.04.99-tst-full-369 \\
        --repo-id gerchowl/mat-vis-tst \\
        [--release-line v2026.04] \\
        [--include-thumb] \\
        [--waiver-file waived.yaml] \\
        [--upstream-catalog PATH] \\
        [--json]

Reuses existing infrastructure:
- ``validate_release.baked_ids_from_release_manifest`` (actual IDs on HF)
- ``validate_release.find_catalog_violations`` (diff logic)
- ``validate_release.load_waivers`` (approved exceptions)
- ``release_registry.release_dag`` (canonical tier shape)
- ``snapshot_upstream_catalog._ids_for_source`` (upstream IDs)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("check-substrate-coverage")

__all__ = [
    "CoverageReport",
    "TierResult",
    "build_coverage_report",
    "detect_release_line",
    "wanted_material_ids",
    "wanted_tiers",
]


# ── data classes ───────────────────────────────────────────────────


@dataclass
class TierResult:
    """Coverage result for one (source, tier) cell."""

    source: str
    tier: str
    expected_count: int | None  # None for manifest-only checks (scalar)
    actual_count: int | None  # None if tier missing from HF entirely
    missing_ids: frozenset[str] = field(default_factory=frozenset)
    extra_ids: frozenset[str] = field(default_factory=frozenset)
    status: str = "ok"  # ok | missing_tier | incomplete | manifest_only

    @property
    def has_violation(self) -> bool:
        return self.status not in ("ok", "manifest_only")


@dataclass
class CoverageReport:
    """Aggregated coverage report across all cells."""

    release_tag: str
    repo_id: str
    release_line: str
    results: list[TierResult] = field(default_factory=list)
    unexpected_tiers: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_violations(self) -> int:
        return sum(1 for r in self.results if r.has_violation) + len(
            self.unexpected_tiers
        )

    @property
    def is_clean(self) -> bool:
        return self.total_violations == 0

    def to_dict(self) -> dict:
        return {
            "release_tag": self.release_tag,
            "repo_id": self.repo_id,
            "release_line": self.release_line,
            "total_violations": self.total_violations,
            "results": [
                {
                    "source": r.source,
                    "tier": r.tier,
                    "status": r.status,
                    "expected_count": r.expected_count,
                    "actual_count": r.actual_count,
                    "missing_count": len(r.missing_ids),
                    "extra_count": len(r.extra_ids),
                    "missing_ids": sorted(r.missing_ids),
                    "extra_ids": sorted(r.extra_ids),
                }
                for r in self.results
            ],
            "unexpected_tiers": [
                {"source": s, "tier": t} for s, t in self.unexpected_tiers
            ],
        }

    def print_summary(self) -> None:
        hdr = (
            f"check-substrate-coverage {self.release_tag} "
            f"(line={self.release_line}, repo={self.repo_id})"
        )
        print(hdr)
        print("=" * len(hdr))
        print()

        # Tier-level overview
        print("tier-level coverage:")
        for r in sorted(self.results, key=lambda r: (r.source, r.tier)):
            exp = r.expected_count if r.expected_count is not None else "—"
            act = r.actual_count if r.actual_count is not None else "—"
            tag = _status_tag(r.status)
            print(f"  {r.source}/{r.tier:10s}  {exp:>5} expected  {act:>5} actual  {tag}")
        print()

        # Material-level violations
        violations = [r for r in self.results if r.has_violation]
        if violations:
            print("material-level violations:")
            for r in sorted(violations, key=lambda r: (r.source, r.tier)):
                parts = []
                if r.missing_ids:
                    parts.append(f"{len(r.missing_ids)} missing")
                if r.extra_ids:
                    parts.append(f"{len(r.extra_ids)} extras")
                print(f"  {r.source}/{r.tier}: {', '.join(parts)}")
                if r.missing_ids:
                    ids = sorted(r.missing_ids)
                    preview = ids[:10]
                    tail = f" ... and {len(ids) - 10} more" if len(ids) > 10 else ""
                    print(f"    missing: {preview}{tail}")
                if r.extra_ids:
                    ids = sorted(r.extra_ids)
                    preview = ids[:10]
                    tail = f" ... and {len(ids) - 10} more" if len(ids) > 10 else ""
                    print(f"    extras:  {preview}{tail}")
            print()

        if self.unexpected_tiers:
            print("unexpected tiers (on HF but not in release matrix):")
            for s, t in sorted(self.unexpected_tiers):
                print(f"  {s}/{t}")
            print()

        # Summary
        print("summary:")
        print(f"  cells checked:  {len(self.results)}")
        print(f"  violations:     {self.total_violations}")
        print(f"  exit:           {0 if self.is_clean else 1}")


def _status_tag(status: str) -> str:
    return {
        "ok": "OK",
        "manifest_only": "OK (manifest-only)",
        "missing_tier": "MISSING TIER",
        "incomplete": "INCOMPLETE",
    }.get(status, status.upper())


# ── core logic ─────────────────────────────────────────────────────


def detect_release_line(tag: str) -> str:
    """Extract release line from a tag, e.g. ``v2026.04.3`` -> ``v2026.04``."""
    from mat_vis_baker.release_registry import known_lines

    parts = tag.split(".")
    if len(parts) < 2:
        raise ValueError(f"cannot parse release line from tag {tag!r}")
    line = ".".join(parts[:2])
    if line not in known_lines():
        raise ValueError(
            f"release line {line!r} (from tag {tag!r}) not in "
            f"known lines: {known_lines()}"
        )
    return line


def wanted_tiers(
    line: str,
    *,
    include_thumb: bool = False,
) -> set[tuple[str, str]]:
    """Return the set of ``(source, tier)`` cells expected for a release line."""
    from mat_vis_baker.release_registry import release_dag
    from mat_vis_baker.sources import KNOWN_SOURCES

    dag = release_dag(line)
    cells = {(a.source, a.tier) for a in dag.all_artifacts()}
    if include_thumb:
        for source in sorted(KNOWN_SOURCES):
            cells.add((source, "thumb"))
    return cells


def wanted_material_ids(
    sources: list[str],
    *,
    upstream_catalog_path: Path | None = None,
) -> dict[str, set[str]]:
    """Return ``{source: {material_id, ...}}`` from upstream.

    If ``upstream_catalog_path`` is given, loads from a pre-captured
    snapshot JSON (the shape produced by ``snapshot_upstream_catalog``).
    Otherwise, hits live upstream APIs via ``_ids_for_source``.
    """
    if upstream_catalog_path is not None:
        data = json.loads(upstream_catalog_path.read_text())
        out: dict[str, set[str]] = {}
        for src in sources:
            entry = data.get("sources", {}).get(src)
            if entry:
                out[src] = set(entry["ids"])
            else:
                log.warning(
                    "source %s not in upstream catalog %s", src, upstream_catalog_path
                )
                out[src] = set()
        return out

    from scripts.snapshot_upstream_catalog import _ids_for_source

    return {src: set(_ids_for_source(src)) for src in sources}


def _manifest_declares_tier(
    manifest: dict,
    source: str,
    tier: str,
) -> bool:
    """Check whether release-manifest.json declares a (source, tier) cell."""
    src_entry = manifest.get("sources", {}).get(source)
    if not src_entry:
        return False
    return tier in (src_entry.get("tiers") or {})


def build_coverage_report(
    *,
    release_tag: str,
    repo_id: str,
    release_line: str,
    wanted: set[tuple[str, str]],
    wanted_ids: dict[str, set[str]],
    actual: dict[tuple[str, str], set[str]],
    waivers: dict[tuple[str, str], set[str]],
    manifest: dict | None = None,
) -> CoverageReport:
    """Compare wanted vs actual and build the coverage report."""
    from mat_vis_baker.sources import SCALAR_SOURCES

    report = CoverageReport(
        release_tag=release_tag,
        repo_id=repo_id,
        release_line=release_line,
    )

    for source, tier in sorted(wanted):
        # Scalar sources: manifest-only check (no per-file tree)
        if source in SCALAR_SOURCES and tier != "thumb":
            declared = False
            if manifest:
                declared = _manifest_declares_tier(manifest, source, tier)
            report.results.append(
                TierResult(
                    source=source,
                    tier=tier,
                    expected_count=None,
                    actual_count=None,
                    status="manifest_only" if declared else "missing_tier",
                )
            )
            continue

        actual_ids = actual.get((source, tier))

        # Tier entirely missing from HF
        if actual_ids is None:
            expected_count = len(wanted_ids.get(source, set()))
            report.results.append(
                TierResult(
                    source=source,
                    tier=tier,
                    expected_count=expected_count,
                    actual_count=None,
                    status="missing_tier",
                )
            )
            continue

        # Material-level diff
        upstream_for_source = wanted_ids.get(source, set())
        waived = waivers.get((source, tier), set())
        expected = upstream_for_source - waived
        missing = expected - actual_ids
        extras = actual_ids - upstream_for_source

        expected_count = len(expected)
        actual_count = len(actual_ids)

        if missing or extras:
            status = "incomplete"
        else:
            status = "ok"

        report.results.append(
            TierResult(
                source=source,
                tier=tier,
                expected_count=expected_count,
                actual_count=actual_count,
                missing_ids=frozenset(missing),
                extra_ids=frozenset(extras),
                status=status,
            )
        )

    # Unexpected tiers: on HF but not in the wanted set
    for source, tier in sorted(actual.keys()):
        if (source, tier) not in wanted:
            report.unexpected_tiers.append((source, tier))

    return report


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns 0 clean, 1 violations, 2 setup error."""
    p = argparse.ArgumentParser(
        prog="check-substrate-coverage",
        description="Compare wanted vs actual substrate coverage on HF.",
    )
    p.add_argument("--release-tag", required=True, help="CalVer release tag")
    p.add_argument(
        "--repo-id",
        default="gerchowl/mat-vis-tst",
        help="HF dataset repo (default: %(default)s)",
    )
    p.add_argument(
        "--release-line",
        default="",
        help="Release line (default: auto-detect from tag)",
    )
    p.add_argument(
        "--include-thumb",
        action="store_true",
        help="Include thumb tier in coverage check",
    )
    p.add_argument(
        "--waiver-file",
        type=Path,
        default=None,
        help="Path to waived.yaml for approved exceptions",
    )
    p.add_argument(
        "--upstream-catalog",
        type=Path,
        default=None,
        help=(
            "Path to pre-captured upstream-catalog.json "
            "(default: hit live upstream APIs)"
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output machine-readable JSON",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: %(message)s",
    )

    # ── resolve release line ──
    try:
        line = args.release_line or detect_release_line(args.release_tag)
    except ValueError as e:
        log.error("setup error: %s", e)
        return 2

    # ── wanted tiers ──
    try:
        w_tiers = wanted_tiers(line, include_thumb=args.include_thumb)
    except (KeyError, ValueError) as e:
        log.error("setup error resolving release matrix: %s", e)
        return 2

    # ── wanted material IDs ──
    sources_needed = sorted({s for s, _ in w_tiers})
    log.info("fetching upstream material IDs for: %s", ", ".join(sources_needed))
    try:
        w_ids = wanted_material_ids(
            sources_needed,
            upstream_catalog_path=args.upstream_catalog,
        )
    except Exception as e:
        log.error("setup error fetching upstream catalog: %s", e)
        return 2

    for src, ids in sorted(w_ids.items()):
        log.info("  %s: %d upstream materials", src, len(ids))

    # ── actual state on HF ──
    log.info("scanning HF substrate at %s @ %s ...", args.repo_id, args.release_tag)
    try:
        actual, manifest = _scan_hf(args.repo_id, args.release_tag)
    except Exception as e:
        log.error("setup error scanning HF: %s", e)
        return 2

    for (src, tier), ids in sorted(actual.items()):
        log.info("  %s/%s: %d materials on HF", src, tier, len(ids))

    # ── waivers ──
    waivers = _load_waivers(args.waiver_file) if args.waiver_file else {}

    # ── build report ──
    report = build_coverage_report(
        release_tag=args.release_tag,
        repo_id=args.repo_id,
        release_line=line,
        wanted=w_tiers,
        wanted_ids=w_ids,
        actual=actual,
        waivers=waivers,
        manifest=manifest,
    )

    # ── output ──
    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        report.print_summary()
        # Hint for gpuopen failed-material noise
        gpuopen_missing = [
            r
            for r in report.results
            if r.source == "gpuopen" and r.missing_ids and not args.waiver_file
        ]
        if gpuopen_missing:
            print(
                "hint: gpuopen has known bake failures. Create a waiver file "
                "(--waiver-file) to suppress expected missing materials."
            )

    return 0 if report.is_clean else 1


def _load_waivers(path: Path | None) -> dict[tuple[str, str], set[str]]:
    """Parse ``waived.yaml`` → ``{(source, tier): {id, ...}}``.

    Minimal re-implementation of ``validate_release.load_waivers`` to
    avoid importing validate_release (which top-level imports pyarrow).
    """
    if path is None or not path.exists():
        return {}
    raw = path.read_text()
    if not raw.strip():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw) or {}
    except ImportError:
        data = _minimal_yaml(raw)

    out: dict[tuple[str, str], set[str]] = {}
    for source, tiers in (data or {}).items():
        for tier, ids in (tiers or {}).items():
            out[(str(source), str(tier))] = set(ids or [])
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


def _scan_hf(
    repo_id: str,
    release_tag: str,
) -> tuple[dict[tuple[str, str], set[str]], dict | None]:
    """Fetch actual material IDs and manifest from HF.

    Combined entry point so tests can monkeypatch a single function
    to avoid importing huggingface_hub / pyarrow.
    """
    from huggingface_hub import HfApi

    api = HfApi()
    actual = _actual_ids_from_hf(api, repo_id, release_tag)
    manifest = _fetch_manifest(api, repo_id, release_tag)
    return actual, manifest


def _actual_ids_from_hf(
    api: Any,
    repo_id: str,
    release_tag: str,
) -> dict[tuple[str, str], set[str]]:
    """Fetch ``{(source, tier): {material_id, ...}}`` from HF.

    Reads ``release-manifest.json`` to discover (source, tier) pairs,
    then enumerates the per-file tree under each. Mirrors the logic in
    ``validate_release.baked_ids_from_release_manifest`` but avoids
    importing validate_release (which top-level imports pyarrow).
    """
    try:
        manifest_path = api.hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=release_tag,
            filename="release-manifest.json",
        )
        manifest = json.loads(Path(manifest_path).read_text())
    except Exception:  # noqa: BLE001
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
                    rel = path[len(prefix):]  # noqa: E203
                    parts = rel.split("/", 1)
                    if len(parts) == 2 and parts[0] and not parts[0].startswith("."):
                        mids.add(parts[0])
            except Exception:  # noqa: BLE001
                pass
            out[(source, tier)] = mids
    return out


def _fetch_manifest(
    api: Any,
    repo_id: str,
    release_tag: str,
) -> dict | None:
    """Fetch release-manifest.json for scalar tier checks."""
    try:
        manifest_path = api.hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=release_tag,
            filename="release-manifest.json",
        )
        return json.loads(Path(manifest_path).read_text())
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    sys.exit(main())
