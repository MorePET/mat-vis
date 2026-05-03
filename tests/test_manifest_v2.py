"""Tests for the v2 manifest helpers (ADR-0007 Phase 3)."""

from __future__ import annotations

from mat_vis_baker.manifest import (
    MANIFEST_SCHEMA_VERSION,
    _deep_merge,
    build_manifest_from_tree,
    generate_manifest_v2,
)


# ── build_manifest_from_tree: race-free derive from tree listing ──


def test_build_from_tree_empty():
    m = build_manifest_from_tree(release_tag="v1.0.0", tree_paths=[])
    assert m == {"schema_version": MANIFEST_SCHEMA_VERSION, "release_tag": "v1.0.0", "sources": {}}


def test_build_from_tree_scalar_source():
    m = build_manifest_from_tree(
        release_tag="v1", tree_paths=["physicallybased.json", "release-manifest.json"]
    )
    assert m["sources"]["physicallybased"] == {
        "catalog": "physicallybased.json",
        "tiers": {},
    }
    assert "release-manifest" not in m["sources"]


def test_build_from_tree_textured_source_multiple_tiers():
    paths = [
        "polyhaven.json",
        "polyhaven-1k.tar",
        "polyhaven-1k-rowmap.json",
        "polyhaven-512.tar",
        "polyhaven-512-rowmap.json",
    ]
    m = build_manifest_from_tree(release_tag="v1", tree_paths=paths)
    poly = m["sources"]["polyhaven"]
    assert poly["catalog"] == "polyhaven.json"
    assert set(poly["tiers"].keys()) == {"1k", "512"}
    assert poly["tiers"]["1k"] == {"tar": "polyhaven-1k.tar", "rowmap": "polyhaven-1k-rowmap.json"}


def test_build_from_tree_ktx2_tier():
    paths = [
        "polyhaven.json",
        "polyhaven-1k.tar",
        "polyhaven-1k-rowmap.json",
        "ktx2/polyhaven-ktx2-1k.tar",
        "ktx2/polyhaven-ktx2-1k-rowmap.json",
    ]
    m = build_manifest_from_tree(release_tag="v1", tree_paths=paths)
    tiers = m["sources"]["polyhaven"]["tiers"]
    assert set(tiers.keys()) == {"1k", "ktx2-1k"}
    assert tiers["ktx2-1k"] == {
        "tar": "ktx2/polyhaven-ktx2-1k.tar",
        "rowmap": "ktx2/polyhaven-ktx2-1k-rowmap.json",
    }


def test_build_from_tree_multiple_sources():
    paths = [
        "ambientcg.json",
        "ambientcg-1k.tar",
        "ambientcg-1k-rowmap.json",
        "gpuopen.json",
        "gpuopen-2k.tar",
        "gpuopen-2k-rowmap.json",
        "physicallybased.json",
    ]
    m = build_manifest_from_tree(release_tag="v1", tree_paths=paths)
    assert set(m["sources"].keys()) == {"ambientcg", "gpuopen", "physicallybased"}
    assert list(m["sources"]["physicallybased"]["tiers"].keys()) == []
    assert list(m["sources"]["ambientcg"]["tiers"].keys()) == ["1k"]
    assert list(m["sources"]["gpuopen"]["tiers"].keys()) == ["2k"]


def test_build_from_tree_with_materials_counts():
    m = build_manifest_from_tree(
        release_tag="v1",
        tree_paths=["polyhaven.json"],
        materials_counts={"polyhaven": 753},
    )
    assert m["sources"]["polyhaven"]["materials_count"] == 753


def test_build_from_tree_ignores_noise():
    paths = [
        "README.md",
        ".gitattributes",
        "LICENSES/CC0-1.0.txt",
        "polyhaven.json",
        "polyhaven-1k.tar",
        "polyhaven-1k-rowmap.json",
    ]
    m = build_manifest_from_tree(release_tag="v1", tree_paths=paths)
    assert set(m["sources"].keys()) == {"polyhaven"}


def test_generate_empty():
    m = generate_manifest_v2("v2026.04.1-rc1", {})
    assert m == {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_tag": "v2026.04.1-rc1",
        "sources": {},
    }


def test_generate_with_source():
    m = generate_manifest_v2(
        "v2026.04.1-rc1",
        {"physicallybased": {"catalog": "physicallybased.json", "materials_count": 86}},
    )
    assert m["sources"]["physicallybased"]["materials_count"] == 86


def test_deep_merge_adds_new_source():
    base = {"sources": {"pb": {"catalog": "pb.json"}}}
    patch = {"sources": {"poly": {"catalog": "poly.json"}}}
    out = _deep_merge(base, patch)
    assert set(out["sources"].keys()) == {"pb", "poly"}


def test_deep_merge_adds_new_tier_to_existing_source():
    base = {
        "sources": {
            "poly": {
                "catalog": "poly.json",
                "tiers": {"1k": {"tar": "poly-1k.tar", "rowmap": "poly-1k-rowmap.json"}},
            }
        }
    }
    patch = {
        "sources": {
            "poly": {"tiers": {"2k": {"tar": "poly-2k.tar", "rowmap": "poly-2k-rowmap.json"}}}
        }
    }
    out = _deep_merge(base, patch)
    assert set(out["sources"]["poly"]["tiers"].keys()) == {"1k", "2k"}
    assert out["sources"]["poly"]["catalog"] == "poly.json"


def test_deep_merge_patch_wins_on_scalar():
    base = {"sources": {"poly": {"materials_count": 10}}}
    patch = {"sources": {"poly": {"materials_count": 20}}}
    out = _deep_merge(base, patch)
    assert out["sources"]["poly"]["materials_count"] == 20


def test_deep_merge_does_not_mutate_input():
    base = {"sources": {"poly": {"materials_count": 10}}}
    _deep_merge(base, {"sources": {"poly": {"materials_count": 99}}})
    assert base["sources"]["poly"]["materials_count"] == 10
