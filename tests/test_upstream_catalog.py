"""Tests for upstream-catalog snapshot + WAIVED contract (#88 Phase 2).

The snapshot captures what upstream *currently advertises* per source —
the truth data the validator's catalog-contract gate compares against.
Stored as JSON so it's diffable across releases (``diff catalog_N
catalog_{N-1}`` = upstream change audit log).

The WAIVED set is hand-maintained in ``waived.yaml`` — materials that
are known-not-available at specific tiers (e.g. polyhaven doesn't ship
2k for certain wood textures). Without waivers, every legitimate
upstream-only material would cause a false positive.
"""

from __future__ import annotations

import json
from unittest.mock import patch


# ── Schema ──────────────────────────────────────────────────────


def test_catalog_schema_minimal(tmp_path):
    from scripts.snapshot_upstream_catalog import build_catalog

    sources = {
        "ambientcg": ["Rock064", "Metal032"],
        "polyhaven": ["oak_planks"],
    }
    catalog = build_catalog(
        release_tag="v2026.04.0",
        sources=sources,
        snapshotted_at="2026-04-19T12:00:00Z",
    )
    assert catalog["release_tag"] == "v2026.04.0"
    assert catalog["snapshotted_at"] == "2026-04-19T12:00:00Z"
    assert "ambientcg" in catalog["sources"]
    assert catalog["sources"]["ambientcg"]["count"] == 2
    assert set(catalog["sources"]["ambientcg"]["ids"]) == {"Rock064", "Metal032"}


def test_catalog_ids_are_sorted(tmp_path):
    """Deterministic output — sorted IDs make the JSON diffable."""
    from scripts.snapshot_upstream_catalog import build_catalog

    catalog = build_catalog(
        release_tag="v2026.04.0",
        sources={"x": ["zebra", "apple", "mango"]},
        snapshotted_at="2026-04-19T12:00:00Z",
    )
    assert catalog["sources"]["x"]["ids"] == ["apple", "mango", "zebra"]


# ── Snapshot via source adapters (mocked) ───────────────────────


def test_snapshot_via_source_adapters(tmp_path):
    """The snapshot script calls each source's discover() to get the
    current material list. We mock the three adapters."""
    from scripts.snapshot_upstream_catalog import snapshot

    with (
        patch("scripts.snapshot_upstream_catalog._ids_for_source") as fake,
    ):
        fake.side_effect = lambda name: {
            "ambientcg": ["Rock064"],
            "polyhaven": ["oak"],
            "gpuopen": ["a", "b", "c"],
        }[name]
        out = tmp_path / "upstream-catalog.json"
        snapshot(
            output_path=out,
            release_tag="v2026.04.0",
            sources=["ambientcg", "polyhaven", "gpuopen"],
        )

    catalog = json.loads(out.read_text())
    assert catalog["sources"]["ambientcg"]["count"] == 1
    assert catalog["sources"]["gpuopen"]["count"] == 3


# ── WAIVED loader ───────────────────────────────────────────────


def test_load_waivers_empty_file(tmp_path):
    from scripts.validate_release import load_waivers

    wf = tmp_path / "waived.yaml"
    wf.write_text("")
    assert load_waivers(wf) == {}


def test_load_waivers_missing_file_returns_empty(tmp_path):
    """A missing waived.yaml is legitimate — not every project needs
    waivers. Should not raise."""
    from scripts.validate_release import load_waivers

    assert load_waivers(tmp_path / "nonexistent.yaml") == {}


def test_load_waivers_parses_source_tier_structure(tmp_path):
    from scripts.validate_release import load_waivers

    wf = tmp_path / "waived.yaml"
    wf.write_text(
        """\
polyhaven:
  "2k":
    - material_without_2k_upstream
    - another_one
gpuopen:
  "1k":
    - special_case_id
"""
    )
    waivers = load_waivers(wf)
    assert waivers[("polyhaven", "2k")] == {
        "material_without_2k_upstream",
        "another_one",
    }
    assert waivers[("gpuopen", "1k")] == {"special_case_id"}
    assert ("ambientcg", "1k") not in waivers


# ── Contract: baked == upstream \ WAIVED ────────────────────────


def test_catalog_contract_flags_missing_materials(tmp_path):
    """Baker produced 2 materials but upstream had 3 — flag the missing one."""
    from scripts.validate_release import find_catalog_violations

    upstream = {
        "ambientcg": {"Rock064", "Metal032", "Wood045"},
    }
    baked_per_tier = {
        ("ambientcg", "1k"): {"Rock064", "Metal032"},  # Wood045 missing
    }
    waivers: dict = {}

    violations = find_catalog_violations(
        upstream=upstream, baked_per_tier=baked_per_tier, waivers=waivers
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["source"] == "ambientcg"
    assert v["tier"] == "1k"
    assert v["missing"] == {"Wood045"}
    assert v["extras"] == set()


def test_catalog_contract_flags_extras(tmp_path):
    """Baker produced a material not in upstream — also a violation."""
    from scripts.validate_release import find_catalog_violations

    upstream = {"ambientcg": {"Rock064"}}
    baked_per_tier = {("ambientcg", "1k"): {"Rock064", "phantom_material"}}

    violations = find_catalog_violations(
        upstream=upstream, baked_per_tier=baked_per_tier, waivers={}
    )
    assert violations[0]["extras"] == {"phantom_material"}


def test_catalog_contract_waives_known_gaps(tmp_path):
    """Waived material is allowed to be missing — no violation reported."""
    from scripts.validate_release import find_catalog_violations

    upstream = {"polyhaven": {"a", "b", "c"}}
    baked_per_tier = {("polyhaven", "2k"): {"a", "b"}}  # 'c' missing
    waivers = {("polyhaven", "2k"): {"c"}}

    violations = find_catalog_violations(
        upstream=upstream, baked_per_tier=baked_per_tier, waivers=waivers
    )
    assert violations == []  # c is waived


def test_catalog_contract_clean_when_fully_matched(tmp_path):
    from scripts.validate_release import find_catalog_violations

    upstream = {"ambientcg": {"Rock064", "Metal032"}}
    baked_per_tier = {
        ("ambientcg", "128"): {"Rock064", "Metal032"},
        ("ambientcg", "1k"): {"Rock064", "Metal032"},
    }
    violations = find_catalog_violations(
        upstream=upstream, baked_per_tier=baked_per_tier, waivers={}
    )
    assert violations == []


def test_catalog_contract_ignores_sources_not_in_upstream_snapshot(tmp_path):
    """If upstream snapshot has no 'gpuopen' entry, we don't check that
    source's bakes — the snapshot is incomplete, not the bake."""
    from scripts.validate_release import find_catalog_violations

    upstream = {"ambientcg": {"Rock064"}}  # no gpuopen
    baked_per_tier = {("gpuopen", "1k"): {"material_a"}}  # bake exists
    violations = find_catalog_violations(
        upstream=upstream, baked_per_tier=baked_per_tier, waivers={}
    )
    assert violations == []


# ── Rowmap IDs reader ───────────────────────────────────────────


def test_baked_ids_from_rowmap_dir(tmp_path):
    """Helper that scans a rowmap dir and returns {(source, tier): set(ids)}.
    Used at validation time to produce baked_per_tier for the contract."""
    from scripts.validate_release import baked_ids_from_rowmap_dir

    # ambientcg 1k has two category rowmaps
    (tmp_path / "ambientcg-1k-stone-rowmap.json").write_text(
        json.dumps(
            {
                "parquet_file": "x.parquet",
                "materials": {"Rock064": {}, "Rock065": {}},
            }
        )
    )
    (tmp_path / "ambientcg-1k-metal-rowmap.json").write_text(
        json.dumps(
            {
                "parquet_file": "y.parquet",
                "materials": {"Metal032": {}},
            }
        )
    )
    # gpuopen 2k has one rowmap
    (tmp_path / "gpuopen-2k-other-rowmap.json").write_text(
        json.dumps({"parquet_file": "z.parquet", "materials": {"ID1": {}}})
    )

    baked = baked_ids_from_rowmap_dir(tmp_path)
    assert baked[("ambientcg", "1k")] == {"Rock064", "Rock065", "Metal032"}
    assert baked[("gpuopen", "2k")] == {"ID1"}
