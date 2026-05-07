"""Tests for the prod-cut preflight helpers (mat-vis#345).

The Dagger module lives outside the main package, so the helpers live
in ``.dagger/src/mat_vis_ci/_preflight.py`` (a pure-Python module
identical in style to ``_bake_cli.py``). We import it via file path
so the suite runs under the default ``uv run pytest`` invocation
without a Dagger engine.

Coverage:

- ``parse_deprecate_cells`` — JSON list / object form / edge cases
- ``manifest_cells`` — extract cell set from v3 manifest
- ``compute_missing_cells`` — set arithmetic for the parity gate
- ``compose_violations`` — full preflight composition with mocked HF
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

PREFLIGHT_PATH = (
    Path(__file__).resolve().parent.parent / ".dagger" / "src" / "mat_vis_ci" / "_preflight.py"
)


@pytest.fixture(scope="module")
def preflight():
    spec = importlib.util.spec_from_file_location("dagger_preflight", PREFLIGHT_PATH)
    if spec is None or spec.loader is None:
        pytest.skip("could not locate .dagger/src/mat_vis_ci/_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── parse_deprecate_cells ─────────────────────────────────────────


class TestParseDeprecateCells:
    def test_empty_string_yields_empty_set(self, preflight):
        assert preflight.parse_deprecate_cells("") == set()

    def test_empty_json_list_yields_empty_set(self, preflight):
        assert preflight.parse_deprecate_cells("[]") == set()

    def test_pair_form(self, preflight):
        assert preflight.parse_deprecate_cells('[["gpuopen", "128"]]') == {("gpuopen", "128")}

    def test_object_form(self, preflight):
        assert preflight.parse_deprecate_cells('[{"source":"gpuopen","tier":"128"}]') == {
            ("gpuopen", "128")
        }

    def test_mixed_forms(self, preflight):
        out = preflight.parse_deprecate_cells(
            '[["gpuopen","128"], {"source":"gpuopen","tier":"256"}]'
        )
        assert out == {("gpuopen", "128"), ("gpuopen", "256")}

    def test_invalid_json_raises(self, preflight):
        with pytest.raises(ValueError, match="valid JSON"):
            preflight.parse_deprecate_cells("not json")

    def test_non_list_raises(self, preflight):
        with pytest.raises(ValueError, match="must be a JSON list"):
            preflight.parse_deprecate_cells('{"source": "gpuopen"}')

    def test_malformed_entry_raises(self, preflight):
        with pytest.raises(ValueError, match="must be"):
            preflight.parse_deprecate_cells('["just-a-string"]')


# ── manifest_cells ────────────────────────────────────────────────


def _v3_manifest(sources_tiers: dict[str, list[str]]) -> dict:
    """Build a minimal v3 manifest: source → list of tiers."""
    return {
        "schema_version": 3,
        "release_tag": "v2026.04.X",
        "sources": {
            src: {
                "catalog": f"{src}.json",
                "tiers": {tier: {"complete": True} for tier in tiers},
            }
            for src, tiers in sources_tiers.items()
        },
    }


class TestManifestCells:
    def test_single_source_single_tier(self, preflight):
        m = _v3_manifest({"gpuopen": ["1k"]})
        assert preflight.manifest_cells(m) == {("gpuopen", "1k")}

    def test_multi_source_multi_tier(self, preflight):
        m = _v3_manifest(
            {
                "gpuopen": ["128", "256", "512", "1k"],
                "polyhaven": ["1k"],
                "physicallybased": ["scalar"],
            }
        )
        cells = preflight.manifest_cells(m)
        assert ("gpuopen", "128") in cells
        assert ("polyhaven", "1k") in cells
        assert ("physicallybased", "scalar") in cells
        assert len(cells) == 6

    def test_empty_manifest_yields_empty(self, preflight):
        assert preflight.manifest_cells({}) == set()
        assert preflight.manifest_cells({"sources": {}}) == set()

    def test_malformed_source_skipped(self, preflight):
        m = {"sources": {"gpuopen": "bogus", "polyhaven": {"tiers": {"1k": {}}}}}
        assert preflight.manifest_cells(m) == {("polyhaven", "1k")}


# ── compute_missing_cells ─────────────────────────────────────────


class TestComputeMissingCells:
    def test_tst_superset_of_prev_no_missing(self, preflight):
        tst = {("gpuopen", "1k"), ("gpuopen", "512")}
        prev = {("gpuopen", "1k")}
        assert preflight.compute_missing_cells(tst, prev, set()) == set()

    def test_tst_subset_misses_diff(self, preflight):
        tst = {("gpuopen", "1k")}
        prev = {("gpuopen", "1k"), ("gpuopen", "512")}
        assert preflight.compute_missing_cells(tst, prev, set()) == {("gpuopen", "512")}

    def test_deprecated_cells_excluded_from_missing(self, preflight):
        """Even when tst lacks a cell prev had, it's not a violation
        if the operator declared the cell as deprecated."""
        tst = {("gpuopen", "1k")}
        prev = {("gpuopen", "1k"), ("gpuopen", "128")}
        deprecated = {("gpuopen", "128")}
        assert preflight.compute_missing_cells(tst, prev, deprecated) == set()

    def test_only_some_deprecations_satisfy(self, preflight):
        tst = {("gpuopen", "1k")}
        prev = {("gpuopen", "1k"), ("gpuopen", "128"), ("gpuopen", "256")}
        deprecated = {("gpuopen", "128")}
        # 256 missing AND not deprecated → still a violation
        assert preflight.compute_missing_cells(tst, prev, deprecated) == {("gpuopen", "256")}

    def test_first_ever_release_no_prev(self, preflight):
        """Empty prev → empty missing (free pass for first cut)."""
        tst = {("gpuopen", "1k")}
        assert preflight.compute_missing_cells(tst, set(), set()) == set()


# ── compose_violations (end-to-end with mocked fetches) ──────────


class TestComposeViolations:
    """Patch ``fetch_release_manifest`` so we can simulate the full
    composition without HF round-trips."""

    def _patch_fetch(self, preflight, by_url: dict[tuple[str, str], dict | None]):
        """Helper: monkeypatch fetch_release_manifest to return per
        ``(repo_id, tag)`` answers."""

        def fake(repo_id: str, tag: str) -> dict | None:
            return by_url.get((repo_id, tag))

        return patch.object(preflight, "fetch_release_manifest", side_effect=fake)

    def test_clean_when_tst_supersets_prev_prod(self, preflight):
        """Tst has 1k+512+128; prev prod had 1k+512+128. Clean."""
        tst_m = _v3_manifest({"gpuopen": ["128", "512", "1k"]})
        prev_m = _v3_manifest({"gpuopen": ["128", "512", "1k"]})
        with self._patch_fetch(
            preflight,
            {
                ("gerchowl/mat-vis-tst", "v2026.04.4"): tst_m,
                ("gerchowl/mat-vis", "v2026.04.3"): prev_m,
            },
        ):
            v = preflight.compose_violations(
                tst_repo_id="gerchowl/mat-vis-tst",
                prod_repo_id="gerchowl/mat-vis",
                release_tag="v2026.04.4",
                previous_prod_tag="v2026.04.3",
                deprecated_cells=set(),
            )
        assert v == []

    def test_violates_when_tst_lacks_release_tag(self, preflight):
        """Gate (1) — tst doesn't have the release_tag we want to push."""
        prev_m = _v3_manifest({"gpuopen": ["1k"]})
        with self._patch_fetch(
            preflight,
            {
                ("gerchowl/mat-vis-tst", "v2026.04.4"): None,  # 404
                ("gerchowl/mat-vis", "v2026.04.3"): prev_m,
            },
        ):
            v = preflight.compose_violations(
                tst_repo_id="gerchowl/mat-vis-tst",
                prod_repo_id="gerchowl/mat-vis",
                release_tag="v2026.04.4",
                previous_prod_tag="v2026.04.3",
                deprecated_cells=set(),
            )
        assert len(v) == 1
        assert v[0]["kind"] == "tst_missing_release_tag"
        assert "ALWAYS E2E" in v[0]["remedy"]

    def test_violates_when_tst_misses_prev_tier(self, preflight):
        """Gate (2) — prev prod had 512, tst has only 1k. Violation."""
        tst_m = _v3_manifest({"gpuopen": ["1k"]})
        prev_m = _v3_manifest({"gpuopen": ["1k", "512"]})
        with self._patch_fetch(
            preflight,
            {
                ("gerchowl/mat-vis-tst", "v2026.04.4"): tst_m,
                ("gerchowl/mat-vis", "v2026.04.3"): prev_m,
            },
        ):
            v = preflight.compose_violations(
                tst_repo_id="gerchowl/mat-vis-tst",
                prod_repo_id="gerchowl/mat-vis",
                release_tag="v2026.04.4",
                previous_prod_tag="v2026.04.3",
                deprecated_cells=set(),
            )
        assert len(v) == 1
        assert v[0]["kind"] == "tier_coverage_regression"
        assert v[0]["missing_cells"] == [("gpuopen", "512")]

    def test_deprecation_unblocks_missing_tier(self, preflight):
        """Gate (3) — operator declared (gpuopen, 512) as deprecated
        with a maintainer-approved label. Preflight now passes."""
        tst_m = _v3_manifest({"gpuopen": ["1k"]})
        prev_m = _v3_manifest({"gpuopen": ["1k", "512"]})
        with self._patch_fetch(
            preflight,
            {
                ("gerchowl/mat-vis-tst", "v2026.04.4"): tst_m,
                ("gerchowl/mat-vis", "v2026.04.3"): prev_m,
            },
        ):
            v = preflight.compose_violations(
                tst_repo_id="gerchowl/mat-vis-tst",
                prod_repo_id="gerchowl/mat-vis",
                release_tag="v2026.04.4",
                previous_prod_tag="v2026.04.3",
                deprecated_cells={("gpuopen", "512")},
            )
        assert v == []

    def test_first_ever_prod_release_passes(self, preflight):
        """previous_prod_tag points at a non-existent tag (404). The
        parity check free-passes — first cut on a new line has nothing
        to regress against."""
        tst_m = _v3_manifest({"gpuopen": ["1k"]})
        with self._patch_fetch(
            preflight,
            {
                ("gerchowl/mat-vis-tst", "v2026.05.0"): tst_m,
                ("gerchowl/mat-vis", "v2026.04.99"): None,  # doesn't exist
            },
        ):
            v = preflight.compose_violations(
                tst_repo_id="gerchowl/mat-vis-tst",
                prod_repo_id="gerchowl/mat-vis",
                release_tag="v2026.05.0",
                previous_prod_tag="v2026.04.99",
                deprecated_cells=set(),
            )
        assert v == []

    def test_violation_short_circuits_after_tst_missing(self, preflight):
        """If tst doesn't have the tag at all, don't bother computing
        coverage diff — nothing to compare against."""
        with self._patch_fetch(
            preflight,
            {
                ("gerchowl/mat-vis-tst", "v9.9.9"): None,
                ("gerchowl/mat-vis", "v0"): None,
            },
        ):
            v = preflight.compose_violations(
                tst_repo_id="gerchowl/mat-vis-tst",
                prod_repo_id="gerchowl/mat-vis",
                release_tag="v9.9.9",
                previous_prod_tag="v0",
                deprecated_cells=set(),
            )
        # Exactly one violation (the tst-missing one); coverage check skipped.
        assert len(v) == 1
        assert v[0]["kind"] == "tst_missing_release_tag"
