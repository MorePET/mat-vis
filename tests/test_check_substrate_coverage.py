"""Substrate coverage checker tests (#293).

Covers:

1. ``detect_release_line`` — tag parsing and validation.
2. ``wanted_tiers`` — release DAG cell extraction, thumb injection.
3. ``build_coverage_report`` — violation detection, waiver suppression,
   scalar manifest-only checks.
4. CLI exit codes — clean → 0, violations → 1, bad tag → 2, JSON flag.
"""

from __future__ import annotations

import json

import pytest

from scripts.check_substrate_coverage import (
    CoverageReport,
    build_coverage_report,
    detect_release_line,
    main,
    wanted_material_ids,
    wanted_tiers,
)


# ── detect_release_line ──────────────────────────────────────────


def test_detect_release_line_from_full_tst_tag():
    assert detect_release_line("v2026.04.99-tst-full-369") == "v2026.04"


def test_detect_release_line_from_patch_tag():
    assert detect_release_line("v2026.04.3") == "v2026.04"


def test_detect_release_line_from_bare_line():
    """Just the two-part prefix should work too."""
    assert detect_release_line("v2026.04") == "v2026.04"


def test_detect_release_line_unknown_raises():
    with pytest.raises(ValueError, match="not in known lines"):
        detect_release_line("v9999.99.0")


def test_detect_release_line_unparseable_raises():
    with pytest.raises(ValueError, match="cannot parse"):
        detect_release_line("bad-tag")


# ── wanted_tiers ─────────────────────────────────────────────────


def test_wanted_tiers_matches_release_dag():
    """wanted_tiers without thumb must match release_dag().all_artifacts()."""
    from mat_vis_baker.release_registry import release_dag

    dag = release_dag("v2026.04")
    expected = {(a.source, a.tier) for a in dag.all_artifacts()}
    actual = wanted_tiers("v2026.04", include_thumb=False)
    assert actual == expected


def test_wanted_tiers_without_thumb_has_no_thumb():
    cells = wanted_tiers("v2026.04", include_thumb=False)
    thumb_cells = {(s, t) for s, t in cells if t == "thumb"}
    assert thumb_cells == set()


def test_wanted_tiers_with_thumb_adds_all_sources():
    from mat_vis_baker.sources import KNOWN_SOURCES

    cells = wanted_tiers("v2026.04", include_thumb=True)
    thumb_cells = {s for s, t in cells if t == "thumb"}
    assert thumb_cells == KNOWN_SOURCES


# ── wanted_material_ids ──────────────────────────────────────────


def test_wanted_material_ids_from_catalog_json(tmp_path):
    """Load from a pre-captured upstream-catalog JSON."""
    catalog = {
        "release_tag": "v2026.04.0",
        "sources": {
            "ambientcg": {"count": 3, "ids": ["Bricks001", "Metal001", "Wood001"]},
            "gpuopen": {"count": 2, "ids": ["uuid_a", "uuid_b"]},
        },
    }
    p = tmp_path / "upstream-catalog.json"
    p.write_text(json.dumps(catalog))

    result = wanted_material_ids(["ambientcg", "gpuopen"], upstream_catalog_path=p)
    assert result["ambientcg"] == {"Bricks001", "Metal001", "Wood001"}
    assert result["gpuopen"] == {"uuid_a", "uuid_b"}


def test_wanted_material_ids_missing_source_in_catalog(tmp_path):
    """Missing source in the catalog should return empty set + warning."""
    catalog = {"sources": {"ambientcg": {"count": 1, "ids": ["mat_a"]}}}
    p = tmp_path / "upstream-catalog.json"
    p.write_text(json.dumps(catalog))

    result = wanted_material_ids(["ambientcg", "polyhaven"], upstream_catalog_path=p)
    assert result["ambientcg"] == {"mat_a"}
    assert result["polyhaven"] == set()


# ── build_coverage_report ────────────────────────────────────────


def _make_report(
    *,
    wanted: set[tuple[str, str]] | None = None,
    wanted_ids: dict[str, set[str]] | None = None,
    actual: dict[tuple[str, str], set[str]] | None = None,
    waivers: dict[tuple[str, str], set[str]] | None = None,
    manifest: dict | None = None,
) -> CoverageReport:
    """Build a report from test data with sensible defaults."""
    return build_coverage_report(
        release_tag="v2026.04.99-tst",
        repo_id="gerchowl/mat-vis-tst",
        release_line="v2026.04",
        wanted=wanted or set(),
        wanted_ids=wanted_ids or {},
        actual=actual or {},
        waivers=waivers or {},
        manifest=manifest,
    )


def test_clean_report():
    """All materials present → no violations."""
    ids = {"mat_a", "mat_b", "mat_c"}
    report = _make_report(
        wanted={("ambientcg", "1k")},
        wanted_ids={"ambientcg": ids},
        actual={("ambientcg", "1k"): ids},
    )
    assert report.is_clean
    assert report.total_violations == 0
    assert report.results[0].status == "ok"


def test_missing_tier_detected():
    """A tier in the matrix but absent from HF is flagged."""
    report = _make_report(
        wanted={("ambientcg", "1k"), ("ambientcg", "512")},
        wanted_ids={"ambientcg": {"mat_a"}},
        actual={("ambientcg", "1k"): {"mat_a"}},
        # ambientcg/512 not in actual → missing_tier
    )
    assert not report.is_clean
    r_512 = next(r for r in report.results if r.tier == "512")
    assert r_512.status == "missing_tier"
    assert r_512.actual_count is None


def test_missing_materials_detected():
    """Materials in upstream but not on HF are flagged."""
    upstream = {"mat_a", "mat_b", "mat_c"}
    on_hf = {"mat_a"}
    report = _make_report(
        wanted={("ambientcg", "1k")},
        wanted_ids={"ambientcg": upstream},
        actual={("ambientcg", "1k"): on_hf},
    )
    assert not report.is_clean
    r = report.results[0]
    assert r.status == "incomplete"
    assert r.missing_ids == frozenset({"mat_b", "mat_c"})
    assert r.expected_count == 3
    assert r.actual_count == 1


def test_extra_materials_reported():
    """Materials on HF but not in upstream are reported."""
    upstream = {"mat_a"}
    on_hf = {"mat_a", "mat_extra"}
    report = _make_report(
        wanted={("gpuopen", "1k")},
        wanted_ids={"gpuopen": upstream},
        actual={("gpuopen", "1k"): on_hf},
    )
    r = report.results[0]
    assert r.extra_ids == frozenset({"mat_extra"})
    assert r.status == "incomplete"


def test_waiver_suppresses_violation():
    """Waived material IDs do not count as missing."""
    upstream = {"mat_a", "mat_b", "mat_failed"}
    on_hf = {"mat_a", "mat_b"}
    waivers = {("gpuopen", "1k"): {"mat_failed"}}
    report = _make_report(
        wanted={("gpuopen", "1k")},
        wanted_ids={"gpuopen": upstream},
        actual={("gpuopen", "1k"): on_hf},
        waivers=waivers,
    )
    assert report.is_clean
    r = report.results[0]
    assert r.missing_ids == frozenset()
    assert r.expected_count == 2  # 3 upstream - 1 waived


def test_physicallybased_scalar_manifest_only():
    """Scalar tier for physicallybased uses manifest-only check."""
    manifest = {
        "sources": {
            "physicallybased": {
                "catalog": "physicallybased.json",
                "tiers": {"scalar": {"complete": True}},
            }
        }
    }
    report = _make_report(
        wanted={("physicallybased", "scalar")},
        wanted_ids={"physicallybased": {"gold", "silver"}},
        actual={},  # no per-file tree for scalar
        manifest=manifest,
    )
    r = report.results[0]
    assert r.status == "manifest_only"
    assert not r.has_violation


def test_physicallybased_scalar_missing_from_manifest():
    """Scalar tier not declared in manifest → flagged."""
    manifest = {"sources": {"physicallybased": {"tiers": {}}}}
    report = _make_report(
        wanted={("physicallybased", "scalar")},
        wanted_ids={"physicallybased": {"gold"}},
        actual={},
        manifest=manifest,
    )
    r = report.results[0]
    assert r.status == "missing_tier"
    assert r.has_violation


def test_physicallybased_thumb_not_manifest_only():
    """Thumb tier for physicallybased is NOT scalar → normal material check."""
    report = _make_report(
        wanted={("physicallybased", "thumb")},
        wanted_ids={"physicallybased": {"gold", "silver"}},
        actual={("physicallybased", "thumb"): {"gold", "silver"}},
    )
    r = report.results[0]
    assert r.status == "ok"


def test_unexpected_tiers_reported():
    """Tiers on HF that aren't in the wanted set are reported."""
    report = _make_report(
        wanted={("ambientcg", "1k")},
        wanted_ids={"ambientcg": {"mat_a"}},
        actual={
            ("ambientcg", "1k"): {"mat_a"},
            ("ambientcg", "2k"): {"mat_a"},  # not in wanted
        },
    )
    assert ("ambientcg", "2k") in report.unexpected_tiers


def test_to_dict_is_json_serializable():
    """Report.to_dict() must produce JSON-serializable output."""
    report = _make_report(
        wanted={("ambientcg", "1k")},
        wanted_ids={"ambientcg": {"mat_a", "mat_b"}},
        actual={("ambientcg", "1k"): {"mat_a"}},
    )
    d = report.to_dict()
    serialized = json.dumps(d)
    assert '"missing_count": 1' in serialized


# ── CLI (main) ───────────────────────────────────────────────────


def _make_scan_hf(baked, manifest=None):
    """Build a mock ``_scan_hf`` returning the given baked data + manifest."""
    if manifest is None:
        manifest = {
            "sources": {
                "physicallybased": {"tiers": {"scalar": {"complete": True}}}
            }
        }

    def _scan_hf(repo_id, release_tag):
        return baked, manifest

    return _scan_hf


def test_main_clean_exits_zero(tmp_path, monkeypatch):
    """Clean substrate → exit 0."""
    ids = {"mat_a", "mat_b"}
    catalog = {
        "sources": {
            "ambientcg": {"count": 2, "ids": sorted(ids)},
            "polyhaven": {"count": 2, "ids": sorted(ids)},
            "gpuopen": {"count": 2, "ids": sorted(ids)},
            "physicallybased": {"count": 2, "ids": sorted(ids)},
        }
    }
    catalog_path = tmp_path / "upstream.json"
    catalog_path.write_text(json.dumps(catalog))

    # Build actual data covering all cells from the release DAG
    from mat_vis_baker.release_registry import release_dag

    dag = release_dag("v2026.04")
    baked = {}
    for a in dag.all_artifacts():
        if a.tier == "scalar":
            continue
        baked[(a.source, a.tier)] = ids

    import scripts.check_substrate_coverage as mod

    monkeypatch.setattr(mod, "_scan_hf", _make_scan_hf(baked))

    rc = main([
        "--release-tag", "v2026.04.99-tst",
        "--repo-id", "gerchowl/mat-vis-tst",
        "--upstream-catalog", str(catalog_path),
    ])
    assert rc == 0


def test_main_violations_exit_one(tmp_path, monkeypatch):
    """Missing materials → exit 1."""
    catalog = {
        "sources": {
            "ambientcg": {"count": 3, "ids": ["a", "b", "c"]},
            "polyhaven": {"count": 1, "ids": ["x"]},
            "gpuopen": {"count": 1, "ids": ["y"]},
            "physicallybased": {"count": 1, "ids": ["z"]},
        }
    }
    catalog_path = tmp_path / "upstream.json"
    catalog_path.write_text(json.dumps(catalog))

    # Only ambientcg/1k exists and is missing "c"
    baked = {("ambientcg", "1k"): {"a", "b"}}

    import scripts.check_substrate_coverage as mod

    monkeypatch.setattr(mod, "_scan_hf", _make_scan_hf(baked))

    rc = main([
        "--release-tag", "v2026.04.99-tst",
        "--upstream-catalog", str(catalog_path),
    ])
    assert rc == 1


def test_main_bad_tag_exits_two():
    """Unknown release line → exit 2."""
    rc = main(["--release-tag", "v9999.99.0"])
    assert rc == 2


def test_main_json_output(tmp_path, monkeypatch, capsys):
    """--json flag produces valid JSON to stdout."""
    catalog = {
        "sources": {
            "ambientcg": {"count": 1, "ids": ["a"]},
            "polyhaven": {"count": 1, "ids": ["a"]},
            "gpuopen": {"count": 1, "ids": ["a"]},
            "physicallybased": {"count": 1, "ids": ["a"]},
        }
    }
    catalog_path = tmp_path / "upstream.json"
    catalog_path.write_text(json.dumps(catalog))

    from mat_vis_baker.release_registry import release_dag

    dag = release_dag("v2026.04")
    baked = {}
    for a in dag.all_artifacts():
        if a.tier == "scalar":
            continue
        baked[(a.source, a.tier)] = {"a"}

    import scripts.check_substrate_coverage as mod

    monkeypatch.setattr(mod, "_scan_hf", _make_scan_hf(baked))

    main([
        "--release-tag", "v2026.04.99-tst",
        "--upstream-catalog", str(catalog_path),
        "--json",
    ])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "total_violations" in data
    assert "results" in data
