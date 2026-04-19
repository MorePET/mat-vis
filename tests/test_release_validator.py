"""Release validator — the gate that would have blocked v2026.04.0 (#88).

Asserts two invariants against ``metrics/bake-metrics.parquet``:

1. **Regression**: current release's ``actual_count`` per (source, tier)
   must be ≥ ``REGRESSION_MIN_RATIO`` × previous release's count.
   Catches the gpuopen-1k 2234 → 10 case.

2. **Cross-tier parity**: for a given release × source, tiers must be
   within ``PARITY_MIN_RATIO`` of each other (unless explicitly waived).
   Catches the situation where 1k has 10 materials but 128 has 2234.

Invoked by ``release-validate.yml`` as a blocking step on release tags.
"""

from __future__ import annotations


import pytest

from mat_vis_baker.metrics import append_bake_metrics


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


@pytest.fixture
def metrics_file(tmp_path):
    return tmp_path / "bake-metrics.parquet"


# ── Regression gate ─────────────────────────────────────────────


def test_regression_detected_when_count_drops_below_threshold(metrics_file):
    from scripts.validate_release import find_regressions

    # Baseline: v2026.03.0 had gpuopen-1k = 2234
    append_bake_metrics(
        metrics_file,
        [_row(release_tag="v2026.03.0", source="gpuopen", tier="1k", actual_count=2234)],
    )
    # Current: v2026.04.0 has gpuopen-1k = 10 (99% drop — the actual bug)
    append_bake_metrics(
        metrics_file,
        [_row(release_tag="v2026.04.0", source="gpuopen", tier="1k", actual_count=10)],
    )

    regressions = find_regressions(metrics_file, current_tag="v2026.04.0", min_ratio=0.95)
    assert any(r["source"] == "gpuopen" and r["tier"] == "1k" for r in regressions)
    reg = next(r for r in regressions if r["source"] == "gpuopen")
    assert reg["actual_count"] == 10
    assert reg["previous_count"] == 2234
    assert reg["ratio"] < 0.01


def test_no_regression_when_count_is_stable(metrics_file):
    from scripts.validate_release import find_regressions

    append_bake_metrics(
        metrics_file,
        [_row(release_tag="v2026.03.0", source="ambientcg", tier="1k", actual_count=1965)],
    )
    append_bake_metrics(
        metrics_file,
        [_row(release_tag="v2026.04.0", source="ambientcg", tier="1k", actual_count=1968)],
    )

    regressions = find_regressions(metrics_file, current_tag="v2026.04.0", min_ratio=0.95)
    assert regressions == []


def test_minor_drop_within_tolerance_does_not_regress(metrics_file):
    from scripts.validate_release import find_regressions

    # 3% drop — within 5% tolerance
    append_bake_metrics(
        metrics_file,
        [_row(release_tag="v2026.03.0", source="polyhaven", tier="1k", actual_count=100)],
    )
    append_bake_metrics(
        metrics_file,
        [_row(release_tag="v2026.04.0", source="polyhaven", tier="1k", actual_count=97)],
    )

    regressions = find_regressions(metrics_file, current_tag="v2026.04.0", min_ratio=0.95)
    assert regressions == []


def test_regression_only_considers_aggregate_rows(metrics_file):
    """__all__ rows drive the regression gate; per-category rows inform
    diagnosis but aren't themselves the gate (categories can churn)."""
    from scripts.validate_release import find_regressions

    # Aggregate stable, one category swung hard: not a regression
    for tag, allc, woodc, stonec in [
        ("v2026.03.0", 1965, 500, 1465),
        ("v2026.04.0", 1965, 10, 1955),  # wood → stone reclassification
    ]:
        append_bake_metrics(
            metrics_file,
            [
                _row(
                    release_tag=tag,
                    source="ambientcg",
                    tier="1k",
                    category="__all__",
                    actual_count=allc,
                ),
                _row(
                    release_tag=tag,
                    source="ambientcg",
                    tier="1k",
                    category="wood",
                    actual_count=woodc,
                ),
                _row(
                    release_tag=tag,
                    source="ambientcg",
                    tier="1k",
                    category="stone",
                    actual_count=stonec,
                ),
            ],
        )

    regressions = find_regressions(metrics_file, current_tag="v2026.04.0", min_ratio=0.95)
    assert regressions == []


def test_no_previous_release_yields_empty_regressions(metrics_file):
    """First-ever release: no baseline → nothing to regress against."""
    from scripts.validate_release import find_regressions

    append_bake_metrics(
        metrics_file,
        [_row(release_tag="v2026.04.0", source="ambientcg", tier="1k", actual_count=1965)],
    )
    regressions = find_regressions(metrics_file, current_tag="v2026.04.0", min_ratio=0.95)
    assert regressions == []


# ── Cross-tier parity ──────────────────────────────────────────


def test_cross_tier_parity_detects_gpuopen_1k_gap(metrics_file):
    """gpuopen-128 = 2234, gpuopen-1k = 10 — these must not coexist."""
    from scripts.validate_release import find_tier_parity_violations

    for tier, count in [("128", 2234), ("512", 9), ("1k", 10), ("2k", 1612)]:
        append_bake_metrics(
            metrics_file,
            [
                _row(
                    release_tag="v2026.04.0",
                    source="gpuopen",
                    tier=tier,
                    actual_count=count,
                )
            ],
        )

    violations = find_tier_parity_violations(metrics_file, release_tag="v2026.04.0", min_ratio=0.80)
    assert any(v["source"] == "gpuopen" and v["tier"] in ("512", "1k") for v in violations)


def test_cross_tier_parity_passes_for_full_coverage(metrics_file):
    """If every tier has ~the same count, parity holds."""
    from scripts.validate_release import find_tier_parity_violations

    for tier, count in [("128", 1965), ("512", 1965), ("1k", 1960), ("2k", 1965)]:
        append_bake_metrics(
            metrics_file,
            [
                _row(
                    release_tag="v2026.04.0",
                    source="ambientcg",
                    tier=tier,
                    actual_count=count,
                )
            ],
        )

    violations = find_tier_parity_violations(metrics_file, release_tag="v2026.04.0", min_ratio=0.80)
    assert violations == []


def test_ktx2_mirror_tiers_can_be_excluded(metrics_file):
    """ktx2-* tiers may legitimately have fewer materials (toktx failures).
    Excluding them from parity keeps the gate focused on the PNG tiers."""
    from scripts.validate_release import find_tier_parity_violations

    rows = [
        _row(release_tag="v2026.04.0", source="polyhaven", tier="1k", actual_count=753),
        _row(release_tag="v2026.04.0", source="polyhaven", tier="ktx2-1k", actual_count=500),
    ]
    for r in rows:
        append_bake_metrics(metrics_file, [r])

    violations = find_tier_parity_violations(
        metrics_file,
        release_tag="v2026.04.0",
        min_ratio=0.80,
        exclude_tier_prefixes=("ktx2-",),
    )
    assert violations == []


# ── Main CLI entrypoint ─────────────────────────────────────────


def test_validate_exits_nonzero_on_regression(metrics_file, capsys):
    from scripts.validate_release import main

    append_bake_metrics(
        metrics_file,
        [_row(release_tag="v2026.03.0", source="gpuopen", tier="1k", actual_count=2234)],
    )
    append_bake_metrics(
        metrics_file,
        [_row(release_tag="v2026.04.0", source="gpuopen", tier="1k", actual_count=10)],
    )

    rc = main(["--metrics", str(metrics_file), "--tag", "v2026.04.0"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "gpuopen" in captured.out + captured.err


def test_validate_exits_zero_on_clean_release(metrics_file):
    from scripts.validate_release import main

    # Only one release, full parity — nothing to regress against, no violations
    for tier, count in [("128", 1965), ("1k", 1960), ("2k", 1965)]:
        append_bake_metrics(
            metrics_file,
            [
                _row(
                    release_tag="v2026.04.0",
                    source="ambientcg",
                    tier=tier,
                    actual_count=count,
                )
            ],
        )

    rc = main(["--metrics", str(metrics_file), "--tag", "v2026.04.0"])
    assert rc == 0
