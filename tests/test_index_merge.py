"""Tests for `index_builder.merge_index` (ADR-0007 Phase 3)."""

from __future__ import annotations

from mat_vis_baker.index_builder import merge_index


def _entry(id: str, tiers: list[str], maps: list[str], **extra) -> dict:
    base = {
        "id": id,
        "source": "polyhaven",
        "name": id,
        "category": "stone",
        "tags": [],
        "source_url": f"https://example.com/{id}",
        "source_license": "CC0-1.0",
        "available_tiers": tiers,
        "maps": maps,
        "last_updated": "",
    }
    base.update(extra)
    return base


def test_merge_empty_remote_returns_new_sorted():
    new = [_entry("b", ["1k"], ["color"]), _entry("a", ["1k"], ["color"])]
    out = merge_index(None, new)
    assert [e["id"] for e in out] == ["a", "b"]


def test_merge_new_wins_on_conflict_scalar_fields():
    existing = [_entry("a", ["1k"], ["color"], roughness=0.1)]
    new = [_entry("a", ["1k"], ["color"], roughness=0.9)]
    out = merge_index(existing, new)
    assert out[0]["roughness"] == 0.9


def test_merge_unions_available_tiers():
    existing = [_entry("a", ["1k"], ["color"])]
    new = [_entry("a", ["2k"], ["color"])]
    out = merge_index(existing, new)
    assert out[0]["available_tiers"] == ["1k", "2k"]


def test_merge_unions_maps():
    existing = [_entry("a", ["1k"], ["color"])]
    new = [_entry("a", ["1k"], ["color", "normal"])]
    out = merge_index(existing, new)
    assert out[0]["maps"] == ["color", "normal"]


def test_merge_keeps_unique_existing_entries():
    existing = [_entry("a", ["1k"], ["color"]), _entry("b", ["1k"], ["color"])]
    new = [_entry("a", ["2k"], ["color"])]
    out = merge_index(existing, new)
    assert {e["id"] for e in out} == {"a", "b"}
    assert next(e for e in out if e["id"] == "a")["available_tiers"] == ["1k", "2k"]


def test_merge_output_is_sorted_by_id():
    existing = [_entry("z", ["1k"], ["color"])]
    new = [_entry("a", ["1k"], ["color"]), _entry("m", ["1k"], ["color"])]
    out = merge_index(existing, new)
    assert [e["id"] for e in out] == ["a", "m", "z"]
