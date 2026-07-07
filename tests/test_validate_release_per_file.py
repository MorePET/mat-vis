"""Per-file substrate release validator (#263 phase C).

Covers:

1. Schema autodetect — :func:`load_aggregated_counts` reduces a per-file
   metrics parquet to one row per ``(release_tag, source, tier)`` by
   summing ``materials_committed`` over bake-batches; derive batches
   are excluded.
2. Regression scenario — synthetic metrics with v2026.04.0 holding
   ``gpuopen-1k=2234`` then v2026.04.1 dropping to 10 → regression
   gate fires (the v2026.04.0 reproduction case from #88).
3. Tier parity — synthetic 1k=1000 / 2k=200 → parity gate fires.
4. Live HF cross-check — :func:`baked_ids_from_release_manifest` against
   a mocked HfApi returns the expected per-(source, tier) id sets.
5. CLI exit codes — clean run → 0, regression run → 1, missing file → 2.
6. Both the new ``--release-tag`` and the legacy ``--tag`` CLI alias
   route to the same code path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


from mat_vis_baker.per_file_metrics import record_batch
from scripts.validate_release import (
    baked_ids_from_release_manifest,
    find_manifest_asset_violations,
    find_regressions,
    find_regressions_from_hf,
    find_tier_completeness_violations,
    find_tier_parity_violations,
    find_tier_parity_violations_from_hf,
    load_aggregated_counts,
    main,
    wanted_cells_for_line,
)
from scripts.validate_release import _line_for_tag


# ── helpers ────────────────────────────────────────────────────


def _seed(metrics_path: Path, *batches):
    """Seed a per-file metrics parquet with N batches.

    Each ``batches`` entry is a kwargs dict layered over a sane default
    so tests focus on the fields they're varying.
    """
    base = dict(
        release_tag="v2026.04.1",
        source="ambientcg",
        tier="1k",
        operation="bake",
        files_committed=7,
        bytes_committed=1_000_000,
        hf_commit_oid="cafef00d" * 5,
        repo_id="gerchowl/mat-vis",
        timestamp_utc="2026-04-18T20:00:00Z",
    )
    for i, b in enumerate(batches, start=1):
        kw = {**base, **b}
        kw.setdefault("batch_seq", i)
        record_batch(metrics_path, **kw)


# ── load_aggregated_counts ────────────────────────────────────


def test_load_aggregated_counts_sums_bake_batches_per_key(tmp_path):
    """Three bake batches at the same (release, source, tier) should
    sum to one aggregate with materials_committed totals added."""
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {"materials_committed": 100},
        {"materials_committed": 200},
        {"materials_committed": 300},
    )
    rows = load_aggregated_counts(p)
    assert len(rows) == 1
    r = rows[0]
    assert r["actual_count"] == 600
    assert r["release_tag"] == "v2026.04.1"
    assert (r["source"], r["tier"]) == ("ambientcg", "1k")


def test_load_aggregated_counts_excludes_derive_operations(tmp_path):
    """Derive batches must NOT inflate the bake material total — they
    copy materials, they don't add new ones to the source coverage."""
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {"materials_committed": 100, "operation": "bake"},
        {"materials_committed": 999, "operation": "derive_resize"},
        {"materials_committed": 999, "operation": "derive_ktx2"},
    )
    rows = load_aggregated_counts(p)
    assert len(rows) == 1
    assert rows[0]["actual_count"] == 100


def test_load_aggregated_counts_partitions_per_release_source_tier(tmp_path):
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {"materials_committed": 50, "release_tag": "v2026.04.0"},
        {"materials_committed": 70, "release_tag": "v2026.04.0", "tier": "2k"},
        {"materials_committed": 80, "release_tag": "v2026.04.1"},
        {"materials_committed": 90, "release_tag": "v2026.04.1", "source": "polyhaven"},
    )
    rows = load_aggregated_counts(p)
    keyed = {(r["release_tag"], r["source"], r["tier"]): r["actual_count"] for r in rows}
    assert keyed == {
        ("v2026.04.0", "ambientcg", "1k"): 50,
        ("v2026.04.0", "ambientcg", "2k"): 70,
        ("v2026.04.1", "ambientcg", "1k"): 80,
        ("v2026.04.1", "polyhaven", "1k"): 90,
    }


# ── regression gate (the v2026.04.0 reproduction) ─────────────


def test_regression_detected_when_count_drops_below_threshold(tmp_path):
    """The original v2026.04.0 bug: gpuopen-1k went 2234 → 10. The
    regression gate must catch this on the per-file substrate."""
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        # Baseline release v2026.04.0 had gpuopen-1k = 2234 (split
        # across two synthetic batches to verify summation works).
        {
            "release_tag": "v2026.04.0",
            "source": "gpuopen",
            "tier": "1k",
            "materials_committed": 1000,
        },
        {
            "release_tag": "v2026.04.0",
            "source": "gpuopen",
            "tier": "1k",
            "materials_committed": 1234,
        },
        # New release v2026.04.1: gpuopen-1k regressed to 10.
        {
            "release_tag": "v2026.04.1",
            "source": "gpuopen",
            "tier": "1k",
            "materials_committed": 10,
        },
    )

    regressions = find_regressions(p, current_tag="v2026.04.1", min_ratio=0.95)
    assert len(regressions) == 1
    r = regressions[0]
    assert r["source"] == "gpuopen"
    assert r["tier"] == "1k"
    assert r["actual_count"] == 10
    assert r["previous_count"] == 2234
    assert r["ratio"] < 0.01


def test_regression_quiet_when_count_is_stable(tmp_path):
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {"release_tag": "v2026.04.0", "materials_committed": 1965},
        {"release_tag": "v2026.04.1", "materials_committed": 1968},
    )
    assert find_regressions(p, current_tag="v2026.04.1", min_ratio=0.95) == []


def test_regression_quiet_when_no_previous_release(tmp_path):
    p = tmp_path / "per-file-metrics.parquet"
    _seed(p, {"release_tag": "v2026.04.0", "materials_committed": 1965})
    assert find_regressions(p, current_tag="v2026.04.0", min_ratio=0.95) == []


# ── tier parity gate ──────────────────────────────────────────


def test_tier_parity_violation_detected(tmp_path):
    """1k=1000 mat / 2k=200 mat in the same release × source: 2k is
    20% of the leader, well below the 80% parity threshold."""
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {"tier": "1k", "materials_committed": 1000},
        {"tier": "2k", "materials_committed": 200},
    )
    violations = find_tier_parity_violations(p, release_tag="v2026.04.1", min_ratio=0.80)
    assert len(violations) == 1
    v = violations[0]
    assert v["tier"] == "2k"
    assert v["actual_count"] == 200
    assert v["leader_count"] == 1000
    assert v["ratio"] == 0.2


def test_tier_parity_quiet_when_full_coverage(tmp_path):
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {"tier": "128", "materials_committed": 1965},
        {"tier": "1k", "materials_committed": 1960},
        {"tier": "2k", "materials_committed": 1965},
    )
    assert find_tier_parity_violations(p, release_tag="v2026.04.1", min_ratio=0.80) == []


def test_tier_parity_excludes_ktx2_tiers_by_default(tmp_path):
    """ktx2-* tiers may legitimately have fewer materials (toktx
    failures) and shouldn't drag the parity gate."""
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {"tier": "1k", "materials_committed": 1000},
        {"tier": "ktx2-1k", "materials_committed": 500},
    )
    violations = find_tier_parity_violations(p, release_tag="v2026.04.1", min_ratio=0.80)
    assert violations == []


# ── live HF cross-check ───────────────────────────────────────


def test_baked_ids_from_release_manifest_round_trips(tmp_path):
    """Mocked HfApi: hf_hub_download returns a manifest pointing at
    two (source, tier) pairs; list_repo_tree returns the materials
    under each. Verify the function aggregates correctly."""
    api = MagicMock()
    manifest = {
        "schema_version": 3,
        "release_tag": "v2026.04.2",
        "sources": {
            "polyhaven": {"catalog": "polyhaven.json", "tiers": {"1k": {"complete": True}}},
            "gpuopen": {"catalog": "gpuopen.json", "tiers": {"1k": {"complete": True}}},
        },
    }
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    api.hf_hub_download.return_value = str(manifest_path)

    def _tree(*, repo_id, repo_type, revision, path_in_repo, recursive):
        if path_in_repo == "polyhaven/1k":
            for p in (
                "polyhaven/1k/wood_a/color.png",
                "polyhaven/1k/wood_a/normal.png",
                "polyhaven/1k/stone_b/color.png",
                "polyhaven/1k/.tier_complete",  # ignored — not a material
            ):
                yield SimpleNamespace(path=p)
        elif path_in_repo == "gpuopen/1k":
            for p in (
                "gpuopen/1k/metal_x/color.png",
                "gpuopen/1k/metal_y/color.png",
                "gpuopen/1k/metal_z/color.png",
            ):
                yield SimpleNamespace(path=p)

    api.list_repo_tree.side_effect = _tree

    out = baked_ids_from_release_manifest(api, "gerchowl/mat-vis", "v2026.04.2")
    assert out == {
        ("polyhaven", "1k"): {"wood_a", "stone_b"},
        ("gpuopen", "1k"): {"metal_x", "metal_y", "metal_z"},
    }


def test_baked_ids_returns_empty_when_manifest_missing(tmp_path):
    api = MagicMock()
    api.hf_hub_download.side_effect = RuntimeError("404")
    out = baked_ids_from_release_manifest(api, "gerchowl/mat-vis", "v9999.99.0")
    assert out == {}


# ── CLI ───────────────────────────────────────────────────────


def test_main_returns_zero_on_clean_release(tmp_path):
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {"tier": "1k", "materials_committed": 1965},
        {"tier": "2k", "materials_committed": 1960},
    )
    rc = main(["--metrics", str(p), "--release-tag", "v2026.04.1"])
    assert rc == 0


def test_main_returns_one_on_regression(tmp_path, capsys):
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {
            "release_tag": "v2026.04.0",
            "source": "gpuopen",
            "tier": "1k",
            "materials_committed": 2234,
        },
        {
            "release_tag": "v2026.04.1",
            "source": "gpuopen",
            "tier": "1k",
            "materials_committed": 10,
        },
    )
    rc = main(["--metrics", str(p), "--release-tag", "v2026.04.1"])
    assert rc == 1
    out = capsys.readouterr()
    assert "gpuopen" in out.out + out.err


def test_main_returns_two_on_missing_metrics_file(tmp_path):
    p = tmp_path / "does-not-exist.parquet"
    rc = main(["--metrics", str(p), "--release-tag", "v2026.04.1"])
    assert rc == 2


def test_main_accepts_legacy_tag_alias(tmp_path):
    """The pre-#263 CLI used --tag; the new canonical name is
    --release-tag. The alias must keep working so existing operator
    runbooks don't break overnight."""
    p = tmp_path / "per-file-metrics.parquet"
    _seed(p, {"materials_committed": 1965})
    rc = main(["--metrics", str(p), "--tag", "v2026.04.1"])
    assert rc == 0


def test_main_returns_two_when_neither_metrics_nor_from_hf_given():
    rc = main(["--release-tag", "v2026.04.1"])
    assert rc == 2


def test_main_returns_two_when_from_hf_lacks_repo_id():
    rc = main(["--from-hf", "--release-tag", "v2026.04.1"])
    assert rc == 2


# ── live HF gate (no parquet) ─────────────────────────────────


def _hf_api_with(per_release: dict[str, dict[tuple[str, str], set[str]]]) -> MagicMock:
    """Build a mocked HfApi that, for each release tag in
    ``per_release``, serves a manifest covering the included
    (source, tier) pairs and a tree containing the listed material
    ids. Returns the mocked api object."""
    api = MagicMock()

    manifests: dict[str, dict] = {}
    for tag, baked in per_release.items():
        sources_block: dict[str, dict] = {}
        for (src, tier), _ids in baked.items():
            sources_block.setdefault(src, {"catalog": f"{src}.json", "tiers": {}})
            sources_block[src]["tiers"][tier] = {"complete": True}
        manifests[tag] = {
            "schema_version": 3,
            "release_tag": tag,
            "sources": sources_block,
        }

    def _download(*, repo_id, repo_type, revision, filename):
        if filename != "release-manifest.json":
            raise FileNotFoundError(filename)
        if revision not in manifests:
            raise RuntimeError(f"404 — no manifest for {revision}")
        # Write to a tmp path the function can read.
        import tempfile

        path = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
        path.write(json.dumps(manifests[revision]))
        path.close()
        return path.name

    api.hf_hub_download.side_effect = _download

    def _tree(*, repo_id, repo_type, revision, path_in_repo, recursive):
        baked = per_release.get(revision, {})
        # path_in_repo is "<source>/<tier>"; strip it to find the bucket.
        for (src, tier), ids in baked.items():
            if path_in_repo.rstrip("/") == f"{src}/{tier}":
                for mid in ids:
                    yield SimpleNamespace(path=f"{src}/{tier}/{mid}/color.png")
                return

    api.list_repo_tree.side_effect = _tree
    return api


def test_find_regressions_from_hf_catches_v2026_04_0_class():
    """Reproduce the v2026.04.0 regression against the live HF gate:
    previous tag had gpuopen-1k=2234 mids, current has 10 → regression.
    """
    prev_ids = {f"mat_{i}" for i in range(2234)}
    curr_ids = {f"mat_{i}" for i in range(10)}
    api = _hf_api_with(
        {
            "v2026.04.0": {("gpuopen", "1k"): prev_ids},
            "v2026.04.1": {("gpuopen", "1k"): curr_ids},
        }
    )

    regressions = find_regressions_from_hf(
        api,
        repo_id="gerchowl/mat-vis",
        current_tag="v2026.04.1",
        previous_tag="v2026.04.0",
        min_ratio=0.95,
    )
    assert len(regressions) == 1
    r = regressions[0]
    assert r["source"] == "gpuopen"
    assert r["tier"] == "1k"
    assert r["actual_count"] == 10
    assert r["previous_count"] == 2234
    assert r["ratio"] < 0.01


def test_find_regressions_from_hf_quiet_when_no_previous_manifest():
    """First-ever release: previous-tag manifest 404s → empty
    regressions list (nothing to regress against)."""
    api = _hf_api_with({"v2026.04.0": {("ambientcg", "1k"): {"mat_a", "mat_b"}}})
    regressions = find_regressions_from_hf(
        api,
        repo_id="gerchowl/mat-vis",
        current_tag="v2026.04.0",
        previous_tag="v2025.99.99",  # doesn't exist
    )
    assert regressions == []


def test_find_tier_parity_violations_from_hf_detects_gap():
    """Live HF parity gate: 1k=1000 / 2k=200 in the same release →
    2k flagged."""
    one_k = {f"mat_{i}" for i in range(1000)}
    two_k = {f"mat_{i}" for i in range(200)}
    api = _hf_api_with(
        {
            "v2026.04.1": {
                ("ambientcg", "1k"): one_k,
                ("ambientcg", "2k"): two_k,
            }
        }
    )

    violations = find_tier_parity_violations_from_hf(
        api,
        repo_id="gerchowl/mat-vis",
        release_tag="v2026.04.1",
        min_ratio=0.80,
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["tier"] == "2k"
    assert v["actual_count"] == 200
    assert v["leader_count"] == 1000


# ── mat-vis#344: tier-missing-from-current is the worst regression class ──


def test_regression_detects_tier_completely_missing_from_current(tmp_path):
    """A tier that existed in the previous release but is gone from
    the current one is the worst-case regression — every consumer of
    that tier breaks immediately. Pre-#344 the metrics path skipped
    these (only iterated rows where current existed)."""
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        # v2026.04.0 had gpuopen at 1k AND 512.
        {
            "release_tag": "v2026.04.0",
            "source": "gpuopen",
            "tier": "1k",
            "materials_committed": 454,
        },
        {
            "release_tag": "v2026.04.0",
            "source": "gpuopen",
            "tier": "512",
            "materials_committed": 454,
        },
        # v2026.04.1 has only 1k (the matrix-only-declares-1k bug).
        {
            "release_tag": "v2026.04.1",
            "source": "gpuopen",
            "tier": "1k",
            "materials_committed": 454,
        },
    )

    regressions = find_regressions(p, current_tag="v2026.04.1", min_ratio=0.95)
    # 1k is fine; 512 is the violation.
    assert len(regressions) == 1
    r = regressions[0]
    assert r["source"] == "gpuopen"
    assert r["tier"] == "512"
    assert r["actual_count"] == 0
    assert r["previous_count"] == 454
    assert r["ratio"] == 0.0
    assert r["kind"] == "tier_missing"


def test_existing_count_drop_still_marked_as_count_drop(tmp_path):
    """The existing v2026.04.0 reproduction (1k stays present but
    drops count) keeps its semantics — kind=count_drop, not
    tier_missing."""
    p = tmp_path / "per-file-metrics.parquet"
    _seed(
        p,
        {
            "release_tag": "v2026.04.0",
            "source": "gpuopen",
            "tier": "1k",
            "materials_committed": 2234,
        },
        {
            "release_tag": "v2026.04.1",
            "source": "gpuopen",
            "tier": "1k",
            "materials_committed": 10,
        },
    )
    [r] = find_regressions(p, current_tag="v2026.04.1", min_ratio=0.95)
    assert r["kind"] == "count_drop"


def test_regression_from_hf_detects_tier_completely_missing(tmp_path):
    """Same blind-spot fix on the --from-hf path. Mock baked_ids so
    previous has gpuopen at {1k, 512} and current has only {1k}."""
    from unittest.mock import patch

    api = MagicMock()
    current = {("gpuopen", "1k"): {f"m{i}" for i in range(454)}}
    previous = {
        ("gpuopen", "1k"): {f"m{i}" for i in range(454)},
        ("gpuopen", "512"): {f"m{i}" for i in range(454)},
    }
    with patch(
        "scripts.validate_release.baked_ids_from_release_manifest",
        side_effect=lambda _api, _repo, tag: current if tag == "v2026.04.1" else previous,
    ):
        regressions = find_regressions_from_hf(
            api,
            repo_id="gerchowl/mat-vis",
            current_tag="v2026.04.1",
            previous_tag="v2026.04.0",
            min_ratio=0.95,
        )

    assert len(regressions) == 1
    r = regressions[0]
    assert r["source"] == "gpuopen"
    assert r["tier"] == "512"
    assert r["actual_count"] == 0
    assert r["kind"] == "tier_missing"


# ── #293: manifest-declared asset reachability ──────────────────


def _manifest_api(tmp_path, manifest):
    """MagicMock HfApi whose hf_hub_download yields a written manifest file."""
    mfile = tmp_path / "release-manifest.json"
    mfile.write_text(json.dumps(manifest))
    api = MagicMock()
    api.hf_hub_download.return_value = str(mfile)
    return api


def test_manifest_assets_flags_declared_but_missing(tmp_path):
    # gpuopen declares catalog + mtlx + a complete 1k tier; only the mtlx 404s.
    manifest = {
        "schema_version": 3,
        "sources": {
            "gpuopen": {
                "catalog": "gpuopen.json",
                "mtlx": "gpuopen-mtlx.json",
                "tiers": {"1k": {"complete": True}},
            },
            "ambientcg": {"catalog": "ambientcg.json", "tiers": {"1k": {"complete": True}}},
        },
    }
    api = _manifest_api(tmp_path, manifest)
    vs = find_manifest_asset_violations(
        api,
        "gerchowl/mat-vis",
        "v1",
        head_fn=lambda url: 404 if url.endswith("gpuopen-mtlx.json") else 200,
    )
    assert len(vs) == 1
    assert vs[0]["source"] == "gpuopen" and vs[0]["feature"] == "mtlx"
    assert "gpuopen-mtlx.json" in vs[0]["url"]


def test_manifest_assets_flags_missing_tier_sentinel(tmp_path):
    manifest = {"sources": {"polyhaven": {"catalog": "polyhaven.json", "tiers": {"1k": {"complete": True}}}}}
    api = _manifest_api(tmp_path, manifest)
    vs = find_manifest_asset_violations(
        api, "r", "v1", head_fn=lambda url: 404 if url.endswith(".tier_complete") else 200
    )
    assert len(vs) == 1 and vs[0]["feature"] == "tier_complete" and vs[0]["tier"] == "1k"


def test_manifest_assets_transient_status_not_a_violation(tmp_path):
    # 429/5xx/0 are inconclusive — must not spuriously red the cron.
    manifest = {"sources": {"gpuopen": {"catalog": "gpuopen.json", "tiers": {"1k": {"complete": True}}}}}
    api = _manifest_api(tmp_path, manifest)
    for transient in (429, 503, 0):
        assert find_manifest_asset_violations(api, "r", "v1", head_fn=lambda u: transient) == []


def test_manifest_assets_clean_when_all_present(tmp_path):
    manifest = {
        "sources": {
            "gpuopen": {"catalog": "gpuopen.json", "mtlx": "gpuopen-mtlx.json", "tiers": {"1k": {"complete": True}}}
        }
    }
    api = _manifest_api(tmp_path, manifest)
    assert find_manifest_asset_violations(api, "r", "v1", head_fn=lambda u: 200) == []


def test_manifest_assets_missing_manifest_is_empty(tmp_path):
    api = MagicMock()
    api.hf_hub_download.side_effect = Exception("404")
    assert find_manifest_asset_violations(api, "r", "v1", head_fn=lambda u: 404) == []


# ── #436: tier-completeness (matrix wanted vs manifest got) ──────


def test_line_for_tag_extracts_calver_prefix():
    assert _line_for_tag("v2026.04.3") == "v2026.04"
    assert _line_for_tag("v2026.04.99-tst-full-369") == "v2026.04"
    assert _line_for_tag("v2026.12.0") == "v2026.12"
    assert _line_for_tag("nightly") is None
    assert _line_for_tag("main") is None


def test_tier_completeness_flags_matrix_tier_absent_from_manifest():
    """The #436 case: matrix declares gpuopen/ktx2-512 but the manifest omits
    it → tier_missing (invisible to the other three gates)."""
    manifest = {
        "sources": {
            "gpuopen": {"catalog": "gpuopen.json", "tiers": {"1k": {"complete": True}}}
        }
    }
    wanted = {("gpuopen", "1k"), ("gpuopen", "ktx2-512")}
    vs = find_tier_completeness_violations(manifest, wanted)
    assert vs == [{"source": "gpuopen", "tier": "ktx2-512", "kind": "tier_missing"}]


def test_tier_completeness_flags_incomplete_tier():
    manifest = {
        "sources": {"gpuopen": {"tiers": {"1k": {"complete": True}, "512": {"complete": False}}}}
    }
    wanted = {("gpuopen", "1k"), ("gpuopen", "512")}
    vs = find_tier_completeness_violations(manifest, wanted)
    assert vs == [{"source": "gpuopen", "tier": "512", "kind": "tier_incomplete"}]


def test_tier_completeness_flags_source_missing():
    manifest = {"sources": {"gpuopen": {"tiers": {"1k": {"complete": True}}}}}
    wanted = {("polyhaven", "1k")}
    vs = find_tier_completeness_violations(manifest, wanted)
    assert vs == [{"source": "polyhaven", "tier": "1k", "kind": "source_missing"}]


def test_tier_completeness_clean_when_all_declared_complete():
    manifest = {
        "sources": {
            "gpuopen": {"tiers": {"1k": {"complete": True}, "ktx2-512": {"complete": True}}}
        }
    }
    wanted = {("gpuopen", "1k"), ("gpuopen", "ktx2-512")}
    assert find_tier_completeness_violations(manifest, wanted) == []


def test_tier_completeness_empty_manifest_flags_all_wanted():
    wanted = {("gpuopen", "1k"), ("polyhaven", "ktx2-1k")}
    vs = find_tier_completeness_violations({}, wanted)
    assert {(v["source"], v["tier"]) for v in vs} == wanted
    assert all(v["kind"] == "source_missing" for v in vs)


def test_wanted_cells_for_line_spans_all_three_phases():
    """The wanted set must union bake (release_matrix), derive (derive_matrix)
    and ktx2 (ktx2_matrix) cells — a gap in any phase would let a whole class
    of missing tier slip through. Guards the exact #436 tier (ktx2-512) plus a
    derived PNG tier and the fetched bake tier."""
    wanted = wanted_cells_for_line("v2026.04")
    assert wanted, "v2026.04 should be a known release line"
    # ktx2 phase — the #436 tier.
    assert ("gpuopen", "ktx2-512") in wanted
    # derive phase — a downscaled PNG tier (not a fetched/bake tier).
    derive_tiers = {t for (_s, t) in wanted if t in {"512", "256", "128"}}
    assert derive_tiers, "derive (downscale) tiers must be part of the wanted set"
    # bake phase — the fetched tier.
    assert ("gpuopen", "1k") in wanted


def test_wanted_cells_for_unknown_line_is_empty():
    assert wanted_cells_for_line("v1999.01") == set()


def test_completeness_gate_is_opt_in(monkeypatch):
    """#436 gate is a WHOLE-RELEASE invariant — it must stay OFF unless
    --check-completeness is passed, so bake.yml's per-phase post-bake validate
    (native tier only) doesn't false-fire on not-yet-derived tiers."""
    import scripts.validate_release as vr

    calls: list[int] = []
    monkeypatch.setattr(vr, "find_regressions_from_hf", lambda *a, **k: [])
    monkeypatch.setattr(vr, "find_tier_parity_violations_from_hf", lambda *a, **k: [])
    monkeypatch.setattr(vr, "find_manifest_asset_violations", lambda *a, **k: [])
    monkeypatch.setattr(vr, "wanted_cells_for_line", lambda line: {("gpuopen", "ktx2-512")})
    monkeypatch.setattr(vr, "_fetch_release_manifest", lambda *a, **k: {"sources": {}})
    monkeypatch.setattr(
        vr, "find_tier_completeness_violations", lambda *a, **k: calls.append(1) or []
    )
    monkeypatch.setattr("huggingface_hub.HfApi", lambda *a, **k: object())

    # Default (no flag) — completeness must NOT run.
    rc = vr.main(["--from-hf", "--release-tag", "v2026.04.2", "--repo-id", "r"])
    assert rc == 0
    assert calls == []

    # Opt-in — completeness runs.
    rc = vr.main(["--from-hf", "--release-tag", "v2026.04.2", "--repo-id", "r", "--check-completeness"])
    assert rc == 0
    assert calls == [1]


# -- #293-P1: pbr coverage regression gate --


def test_pbr_populated_detects_real_vs_allnull():
    from scripts.validate_release import _pbr_populated

    assert _pbr_populated({"color_rgb": [0.5, 0.5, 0.5], "ior": None}) is True
    assert _pbr_populated({"metalness": 0.0}) is True  # 0.0 is a real value
    assert _pbr_populated({"is_conductor": False}) is True
    assert _pbr_populated({"color_rgb": None, "ior": None}) is False  # #290 all-null
    assert _pbr_populated({}) is False
    assert _pbr_populated(None) is False


def _stub_coverage(monkeypatch, mapping):
    import scripts.validate_release as vr

    monkeypatch.setattr(
        vr, "source_pbr_coverage", lambda api, repo, tag: mapping.get(tag, {})
    )


def test_pbr_coverage_flags_collapse(monkeypatch):
    from scripts.validate_release import find_pbr_coverage_regressions

    _stub_coverage(monkeypatch, {"prev": {"gpuopen": (454, 454)}, "cur": {"gpuopen": (0, 454)}})
    v = find_pbr_coverage_regressions(None, repo_id="r", current_tag="cur", previous_tag="prev")
    assert len(v) == 1
    assert v[0]["source"] == "gpuopen"
    assert v[0]["ratio"] == 0.0
    assert v[0]["current_frac"] == 0.0


def test_pbr_coverage_stable_is_clean(monkeypatch):
    from scripts.validate_release import find_pbr_coverage_regressions

    _stub_coverage(monkeypatch, {"prev": {"gpuopen": (454, 454)}, "cur": {"gpuopen": (454, 454)}})
    assert find_pbr_coverage_regressions(None, repo_id="r", current_tag="cur", previous_tag="prev") == []


def test_pbr_coverage_tolerates_single_material_gap(monkeypatch):
    from scripts.validate_release import find_pbr_coverage_regressions

    # ambientcg 99.9% vs 100% → ratio 0.999 > 0.95 default → clean.
    _stub_coverage(
        monkeypatch,
        {"prev": {"ambientcg": (1957, 1957)}, "cur": {"ambientcg": (1956, 1957)}},
    )
    assert find_pbr_coverage_regressions(None, repo_id="r", current_tag="cur", previous_tag="prev") == []


def test_pbr_coverage_free_pass_when_prev_zero(monkeypatch):
    from scripts.validate_release import find_pbr_coverage_regressions

    # Previous release had 0% (e.g. pre-PBR stale) → nothing to regress against.
    _stub_coverage(monkeypatch, {"prev": {"gpuopen": (0, 454)}, "cur": {"gpuopen": (0, 454)}})
    assert find_pbr_coverage_regressions(None, repo_id="r", current_tag="cur", previous_tag="prev") == []


def test_pbr_coverage_waiver_skips_source(monkeypatch):
    from scripts.validate_release import find_pbr_coverage_regressions

    _stub_coverage(monkeypatch, {"prev": {"gpuopen": (454, 454)}, "cur": {"gpuopen": (0, 454)}})
    v = find_pbr_coverage_regressions(
        None, repo_id="r", current_tag="cur", previous_tag="prev", waivers={"gpuopen"}
    )
    assert v == []


def test_pbr_coverage_no_previous_is_empty(monkeypatch):
    from scripts.validate_release import find_pbr_coverage_regressions

    _stub_coverage(monkeypatch, {"cur": {"gpuopen": (0, 454)}})  # no "prev" entry
    assert find_pbr_coverage_regressions(None, repo_id="r", current_tag="cur", previous_tag="prev") == []


# -- #293-P1 review nits: provenance exclusion + transient-safety --


def test_pbr_populated_excludes_provenance():
    from scripts.validate_release import _pbr_populated

    # Only a *_source provenance string set, all measured scalars null → NOT
    # covered (must not mask an all-scalar-null gap).
    assert _pbr_populated(
        {"metalness_source": "graph_estimate", "metalness": None, "color_rgb": None}
    ) is False
    # Provenance alongside a real measured value → covered.
    assert _pbr_populated({"metalness_source": "graph_estimate", "metalness": 0.9}) is True


def test_source_pbr_coverage_skips_unfetchable_catalog(monkeypatch):
    import scripts.validate_release as vr

    monkeypatch.setattr(
        vr,
        "_fetch_release_manifest",
        lambda a, r, t: {
            "sources": {
                "gpuopen": {"catalog": "gpuopen.json"},
                "polyhaven": {"catalog": "polyhaven.json"},
            }
        },
    )

    def fake_cat(api, repo, tag, catalog):
        # gpuopen catalog blips (transient) → []; polyhaven fetches fine.
        return [] if catalog == "gpuopen.json" else [{"mat_vis": {"pbr": {"metalness": 0.5}}}]

    monkeypatch.setattr(vr, "_fetch_source_catalog", fake_cat)
    cov = vr.source_pbr_coverage(None, "r", "t")
    assert "gpuopen" not in cov  # unmeasurable → skipped, not (0, 0)
    assert cov["polyhaven"] == (1, 1)


def test_pbr_coverage_transient_current_no_false_regression(monkeypatch):
    from scripts.validate_release import find_pbr_coverage_regressions

    # Current catalog for gpuopen blipped → omitted from cur coverage; prev had
    # 100%. Must NOT flag a regression (the network-blip-reds-the-cron bug).
    _stub_coverage(monkeypatch, {"prev": {"gpuopen": (454, 454)}, "cur": {}})
    assert find_pbr_coverage_regressions(
        None, repo_id="r", current_tag="cur", previous_tag="prev"
    ) == []
