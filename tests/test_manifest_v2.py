"""Tests for the v2 manifest helpers (ADR-0007 Phase 3)."""

from __future__ import annotations

from mat_vis_baker.manifest import MANIFEST_SCHEMA_VERSION, _deep_merge, generate_manifest_v2


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
