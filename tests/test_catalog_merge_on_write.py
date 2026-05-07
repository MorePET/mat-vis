"""Tests for the per-source catalog merge-on-write (mat-vis#301).

The merge function preserves cross-tier `available_tiers` so that
baking 1k then 2k (or any cross-tier sequence) doesn't clobber the
earlier tier off every material's catalog entry.

Pure-function tests against `_merge_catalog_with_existing`. The
HF-fetch wiring + CAS retry loop is tested live in
`tests/e2e/test_per_file_roundtrip.py` (gated on MAT_VIS_E2E=1) and
not duplicated here.
"""

from __future__ import annotations


from mat_vis_baker.hf_bake_per_file import _merge_catalog_with_existing


def _entry(mid: str, tiers: list[str], **extra) -> dict:
    """Build a minimal catalog entry with the given available_tiers."""
    return {
        "id": mid,
        "source": "test",
        "available_tiers": list(tiers),
        "mat_vis": {"name": mid, **(extra.get("mat_vis") or {})},
    }


# ── first-cut case ──────────────────────────────────────────────


def test_first_cut_no_existing_returns_fresh_unchanged():
    fresh = [_entry("a", ["1k"]), _entry("b", ["1k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=[], fresh_tier="1k")
    assert out == fresh


def test_first_cut_with_falsy_existing_treats_as_empty():
    fresh = [_entry("a", ["2k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=[], fresh_tier="2k")
    assert out == fresh


# ── core merge behavior ─────────────────────────────────────────


def test_material_in_both_unions_available_tiers():
    """Material present at 1k previously, now baked at 2k → both tiers."""
    existing = [_entry("a", ["1k"])]
    fresh = [_entry("a", ["2k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="2k")
    assert len(out) == 1
    assert out[0]["id"] == "a"
    assert out[0]["available_tiers"] == ["1k", "2k"]


def test_material_in_both_takes_fresh_data_for_other_fields():
    """Newest data wins for everything except available_tiers."""
    existing = [_entry("a", ["1k"], mat_vis={"category": "stale"})]
    fresh = [_entry("a", ["2k"], mat_vis={"category": "current"})]
    out = _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="2k")
    assert out[0]["mat_vis"]["category"] == "current"


def test_re_bake_same_tier_no_op_on_available_tiers():
    """Bake 1k twice — available_tiers stays ["1k"]."""
    existing = [_entry("a", ["1k"])]
    fresh = [_entry("a", ["1k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="1k")
    assert out[0]["available_tiers"] == ["1k"]


def test_material_only_in_fresh_added_as_new():
    """A new material upstream — added as-is."""
    existing = [_entry("a", ["1k"])]
    fresh = [_entry("a", ["2k"]), _entry("b", ["2k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="2k")
    ids = [e["id"] for e in out]
    assert ids == ["a", "b"]


def test_material_only_in_existing_with_other_tier_preserved():
    """Bake 1k after upstream prunes material X from a previous 2k bake.
    X had available_tiers=["1k", "2k"] before this 1k re-bake; fresh
    doesn't see X. Drop fresh_tier from X → ["2k"], preserve."""
    existing = [_entry("x", ["1k", "2k"])]
    fresh = [_entry("a", ["1k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="1k")
    ids = [e["id"] for e in out]
    assert "x" in ids
    x_entry = next(e for e in out if e["id"] == "x")
    assert x_entry["available_tiers"] == ["2k"]


def test_material_only_in_existing_with_only_fresh_tier_dropped():
    """Material had only the fresh_tier; fresh bake doesn't see it
    (upstream pruned). Drop the entry."""
    existing = [_entry("x", ["1k"])]
    fresh = [_entry("a", ["1k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="1k")
    ids = [e["id"] for e in out]
    assert "x" not in ids
    assert ids == ["a"]


# ── cross-tier scenarios from the issue's acceptance criteria ───


def test_acceptance_bake_1k_then_2k_preserves_both_tiers():
    """Issue mat-vis#301 acceptance: bake (gpuopen, 1k), bake
    (gpuopen, 2k), assert catalog has available_tiers=["1k", "2k"]
    for the same material."""
    # Step 1: fresh 1k bake (no existing)
    cat_after_1k = _merge_catalog_with_existing(
        fresh=[_entry("aluminum", ["1k"])],
        existing=[],
        fresh_tier="1k",
    )
    assert cat_after_1k[0]["available_tiers"] == ["1k"]

    # Step 2: 2k bake on top
    cat_after_2k = _merge_catalog_with_existing(
        fresh=[_entry("aluminum", ["2k"])],
        existing=cat_after_1k,
        fresh_tier="2k",
    )
    assert cat_after_2k[0]["available_tiers"] == ["1k", "2k"]


def test_three_tier_chain():
    """1k → 2k → 4k cumulative."""
    cat = _merge_catalog_with_existing(fresh=[_entry("m", ["1k"])], existing=[], fresh_tier="1k")
    cat = _merge_catalog_with_existing(fresh=[_entry("m", ["2k"])], existing=cat, fresh_tier="2k")
    cat = _merge_catalog_with_existing(fresh=[_entry("m", ["4k"])], existing=cat, fresh_tier="4k")
    assert cat[0]["available_tiers"] == ["1k", "2k", "4k"]


# ── ordering ────────────────────────────────────────────────────


def test_fresh_entries_come_before_preserved_existing():
    """Output order: fresh entries (in input order), then preserved
    existing entries (in their original order)."""
    existing = [_entry("z", ["2k"]), _entry("y", ["2k"])]
    fresh = [_entry("a", ["1k"]), _entry("b", ["1k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="1k")
    ids = [e["id"] for e in out]
    assert ids == ["a", "b", "z", "y"]


# ── edge cases ──────────────────────────────────────────────────


def test_entries_without_id_skipped():
    existing = [{"available_tiers": ["1k"]}, _entry("a", ["1k"])]
    fresh = [{"available_tiers": ["2k"]}, _entry("a", ["2k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="2k")
    # Only the entries with an id survive the merge
    assert len(out) == 1
    assert out[0]["id"] == "a"
    assert out[0]["available_tiers"] == ["1k", "2k"]


def test_missing_available_tiers_treated_as_empty():
    existing = [{"id": "a", "source": "test", "mat_vis": {}}]  # no available_tiers
    fresh = [_entry("a", ["1k"])]
    out = _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="1k")
    assert out[0]["available_tiers"] == ["1k"]


def test_does_not_mutate_inputs():
    existing = [_entry("a", ["1k"])]
    fresh = [_entry("a", ["2k"])]
    existing_snapshot = [dict(e) for e in existing]
    fresh_snapshot = [dict(e) for e in fresh]

    _merge_catalog_with_existing(fresh=fresh, existing=existing, fresh_tier="2k")

    assert existing == existing_snapshot
    assert fresh == fresh_snapshot
