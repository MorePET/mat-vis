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
    find_tier_parity_violations,
    find_tier_parity_violations_from_hf,
    load_aggregated_counts,
    main,
)


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
