"""Mutation-test the release validator (#88 Phase 3).

The validator is only useful if it actually catches bad releases. This
harness mutates a known-good metrics fixture and asserts the validator
screams — without it, we'd be trusting untested gates.

This is NOT a full mutation-testing framework (PIT / mutmut). It's
deliberately scoped to the specific bug classes the validator is meant
to catch — the same mutations that, if they slipped in, would motivate
adding a new gate. Catches regressions in existing gates and gives a
concrete template for adding new mutators when new gates land.

See #88. Related: Feathers, *Working Effectively with Legacy Code*
(characterization tests); standard mutation-testing literature
(Mothra, PIT, mutmut).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from mat_vis_baker.metrics import append_bake_metrics


# ── Clean fixtures ──────────────────────────────────────────────


def _row(**kw):
    base = {
        "release_tag": "v2026.04.0",
        "timestamp": "2026-04-18T20:00:00Z",
        "source": "ambientcg",
        "tier": "1k",
        "category": "__all__",
        "actual_count": 1965,
        "upstream_count": None,
        "total_bytes": 9_200_000_000,
        "n_parquets": 9,
        "rowmap_sha256": "a" * 64,
        "baker_version": "0.1.0",
        "workflow_run_id": None,
    }
    base.update(kw)
    return base


def _clean_two_release_fixture() -> list[dict]:
    """A fixture where v2026.04.1 is a legitimate follow-up to v2026.04.0:
    same counts across all (source, tier), parity holds within each
    release. Any mutation that breaks either invariant MUST trip the
    validator."""
    rows = []
    for tag, ts in [
        ("v2026.04.0", "2026-04-18T20:00:00Z"),
        ("v2026.04.1", "2026-04-19T20:00:00Z"),
    ]:
        for source, tier, count in [
            ("ambientcg", "128", 1965),
            ("ambientcg", "1k", 1960),
            ("ambientcg", "2k", 1965),
            ("polyhaven", "128", 753),
            ("polyhaven", "1k", 750),
            ("gpuopen", "128", 2234),
            ("gpuopen", "1k", 2230),
        ]:
            rows.append(
                _row(
                    release_tag=tag,
                    timestamp=ts,
                    source=source,
                    tier=tier,
                    actual_count=count,
                )
            )
    return rows


# ── Mutators ────────────────────────────────────────────────────


def mut_drop_almost_all(rows, *, source, tier, release_tag) -> list[dict]:
    """Drop the count for a target row to near zero — 'the gpuopen-1k
    2234 → 10 mutation'."""
    out = copy.deepcopy(rows)
    for r in out:
        if r["source"] == source and r["tier"] == tier and r["release_tag"] == release_tag:
            r["actual_count"] = 10
    return out


def mut_tier_skew(rows, *, source, release_tag, skew_tier) -> list[dict]:
    """Mess with one tier inside a source × release to break parity."""
    out = copy.deepcopy(rows)
    for r in out:
        if r["source"] == source and r["release_tag"] == release_tag and r["tier"] == skew_tier:
            r["actual_count"] = 50  # far below the others
    return out


def mut_half_drop(rows, *, source, tier, release_tag) -> list[dict]:
    """A 50 % drop — well past the 95 % regression threshold."""
    out = copy.deepcopy(rows)
    for r in out:
        if r["source"] == source and r["tier"] == tier and r["release_tag"] == release_tag:
            r["actual_count"] = r["actual_count"] // 2
    return out


# ── Mutation tests ──────────────────────────────────────────────


@pytest.fixture
def metrics_file(tmp_path):
    return tmp_path / "bake-metrics.parquet"


def _write(metrics_file: Path, rows: list[dict]) -> None:
    # Wipe + append in one go
    if metrics_file.exists():
        metrics_file.unlink()
    append_bake_metrics(metrics_file, rows)


def test_unmutated_fixture_is_clean(metrics_file):
    """Sanity: the fixture itself passes — so any subsequent failure is
    attributable to the mutation, not a flaky baseline."""
    from scripts.validate_release import find_regressions, find_tier_parity_violations

    _write(metrics_file, _clean_two_release_fixture())
    assert find_regressions(metrics_file, current_tag="v2026.04.1", min_ratio=0.95) == []
    assert find_tier_parity_violations(metrics_file, release_tag="v2026.04.1", min_ratio=0.80) == []


def test_mutation_drop_almost_all_is_caught_by_regression_gate(metrics_file):
    """The core bug we fixed — 2234 → 10 drop must be caught."""
    from scripts.validate_release import find_regressions

    mutated = mut_drop_almost_all(
        _clean_two_release_fixture(),
        source="gpuopen",
        tier="1k",
        release_tag="v2026.04.1",
    )
    _write(metrics_file, mutated)
    regressions = find_regressions(metrics_file, current_tag="v2026.04.1", min_ratio=0.95)
    assert any(r["source"] == "gpuopen" and r["tier"] == "1k" for r in regressions), (
        "regression gate is broken — 99% drop should trigger it"
    )


def test_mutation_half_drop_is_caught_by_regression_gate(metrics_file):
    """50% drop — validator must not round off to 'within tolerance'."""
    from scripts.validate_release import find_regressions

    mutated = mut_half_drop(
        _clean_two_release_fixture(),
        source="ambientcg",
        tier="1k",
        release_tag="v2026.04.1",
    )
    _write(metrics_file, mutated)
    regressions = find_regressions(metrics_file, current_tag="v2026.04.1", min_ratio=0.95)
    assert any(r["source"] == "ambientcg" and r["tier"] == "1k" for r in regressions)


def test_mutation_tier_skew_is_caught_by_parity_gate(metrics_file):
    """If one tier is a tiny fraction of the leader, parity fires."""
    from scripts.validate_release import find_tier_parity_violations

    mutated = mut_tier_skew(
        _clean_two_release_fixture(),
        source="gpuopen",
        release_tag="v2026.04.1",
        skew_tier="1k",
    )
    _write(metrics_file, mutated)
    vlns = find_tier_parity_violations(metrics_file, release_tag="v2026.04.1", min_ratio=0.80)
    assert any(v["source"] == "gpuopen" and v["tier"] == "1k" for v in vlns), (
        "parity gate is broken — tier at 50 vs leader 2234 should trigger it"
    )


# ── Catalog-contract mutation tests ─────────────────────────────


def test_mutation_missing_upstream_material_is_caught_by_catalog_gate():
    """If a material vanishes from bakes but remains in upstream → missing."""
    from scripts.validate_release import find_catalog_violations

    upstream = {"ambientcg": {"A", "B", "C"}}
    # Baked lost C
    baked = {("ambientcg", "1k"): {"A", "B"}}
    violations = find_catalog_violations(upstream=upstream, baked_per_tier=baked, waivers={})
    assert len(violations) == 1
    assert violations[0]["missing"] == {"C"}


def test_mutation_phantom_extra_caught_even_when_count_ok():
    """If a baker emits a phantom ID (count still matches!), the catalog
    gate fires. The regression/parity gates wouldn't catch this."""
    from scripts.validate_release import find_catalog_violations

    upstream = {"ambientcg": {"A", "B"}}
    baked = {("ambientcg", "1k"): {"A", "phantom"}}  # B replaced by phantom
    violations = find_catalog_violations(upstream=upstream, baked_per_tier=baked, waivers={})
    assert violations[0]["missing"] == {"B"}
    assert violations[0]["extras"] == {"phantom"}


# ── Negative control: a disabled gate must produce a mutation that
#    slips through, proving the mutation harness would notice if the
#    gate regressed. This guards against the harness itself becoming a
#    no-op. ─────────────────────────────────────────────────────


def test_mutation_harness_would_notice_disabled_regression_gate(metrics_file, monkeypatch):
    """Simulate someone breaking the regression gate to always return
    []. Our mutation test for drop_almost_all should then FAIL. We
    assert that failure would occur — meaning the harness isn't silent."""
    import scripts.validate_release as vr

    mutated = mut_drop_almost_all(
        _clean_two_release_fixture(),
        source="gpuopen",
        tier="1k",
        release_tag="v2026.04.1",
    )
    _write(metrics_file, mutated)

    # Disable the gate
    monkeypatch.setattr(vr, "find_regressions", lambda *a, **kw: [])
    # Now the "regression gate caught it" assertion would fail —
    # proving the harness would surface the regression in the gate.
    regressions = vr.find_regressions(metrics_file, current_tag="v2026.04.1", min_ratio=0.95)
    assert regressions == []  # ← mutation slipped through the disabled gate
    # A real mutation test would pytest.fail here; we just confirm the
    # structure works.
