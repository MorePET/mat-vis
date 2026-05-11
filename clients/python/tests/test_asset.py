"""Tests for VisAsset (mat-vis#93).

VisAsset is the ergonomic wrapper that bundles identity + lazy scalars +
lazy textures + adapter methods. Calls the free-function primitives
(``to_threejs`` / ``to_gltf`` / ``export_mtlx``) under the hood. These
unit tests mock the client so no network IO happens.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mat_vis_client import MatVisClient, MtlxSource, VisAsset
from mat_vis_client.adapters import to_gltf, to_threejs


# ── Identity round-trip ─────────────────────────────────────────


def test_identity_round_trip():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    assert a.source == "ambientcg"
    assert a.material_id == "Wood080"
    assert a.tier == "1k"


# ── Immutability ────────────────────────────────────────────────


def test_tier_is_immutable():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    with pytest.raises(AttributeError):
        a.tier = "2k"  # type: ignore[misc]


def test_source_and_material_id_are_immutable():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    with pytest.raises(AttributeError):
        a.source = "gpuopen"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        a.material_id = "Other"  # type: ignore[misc]


# ── with_tier() ─────────────────────────────────────────────────


def test_with_tier_returns_new_instance_same_identity():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    b = a.with_tier("2k")
    assert b is not a
    assert b.source == "ambientcg"
    assert b.material_id == "Wood080"
    assert b.tier == "2k"
    # Original unchanged.
    assert a.tier == "1k"


# ── Equality + hash ─────────────────────────────────────────────


def test_equality_is_identity_only():
    """Two assets with same (source, mid, tier) are equal even from different clients."""
    c1 = MatVisClient()
    c2 = MatVisClient()
    a = VisAsset(c1, "ambientcg", "Wood080", "1k")
    b = VisAsset(c2, "ambientcg", "Wood080", "1k")
    assert a == b


def test_inequality_when_any_field_differs():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    assert a != VisAsset(c, "gpuopen", "Wood080", "1k")
    assert a != VisAsset(c, "ambientcg", "Other", "1k")
    assert a != VisAsset(c, "ambientcg", "Wood080", "2k")
    assert a != "not-an-asset"


def test_hash_matches_equality_and_set_dedupes():
    c1 = MatVisClient()
    c2 = MatVisClient()
    a = VisAsset(c1, "ambientcg", "Wood080", "1k")
    b = VisAsset(c2, "ambientcg", "Wood080", "1k")
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


# ── Lazy fetching ───────────────────────────────────────────────


def test_scalars_property_calls_client_once():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    expected = {"roughness": 0.5, "metalness": 0.0, "color_hex": "#A0522D"}
    with patch.object(c, "_scalars_for", return_value=expected) as mock:
        first = a.scalars
        second = a.scalars
    assert first == expected
    assert second is first  # cached object
    assert mock.call_count == 1
    mock.assert_called_with("ambientcg", "Wood080")


def test_textures_property_calls_fetch_all_textures_once():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    expected = {"color": b"PNG-color", "normal": b"PNG-normal"}
    with patch.object(c, "fetch_all_textures", return_value=expected) as mock:
        first = a.textures
        second = a.textures
    assert first == expected
    assert second is first
    assert mock.call_count == 1
    mock.assert_called_with("ambientcg", "Wood080", "1k")


# ── Adapter methods delegate to free functions ────────────────


def test_to_threejs_matches_free_function():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    scalars = {"roughness": 0.5, "metalness": 0.0, "color_hex": "#A0522D"}
    textures = {"color": b"\x89PNG\r\n\x1a\nFAKE"}
    with (
        patch.object(c, "_scalars_for", return_value=scalars),
        patch.object(c, "fetch_all_textures", return_value=textures),
    ):
        got = a.to_threejs(color_format="int")
    assert got == to_threejs(scalars, textures, color_format="int")


def test_to_gltf_matches_free_function():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    scalars = {"roughness": 0.5, "metalness": 0.0, "color_hex": "#A0522D"}
    # Only color texture — avoids the metallicRoughness-packing path
    # (which depends on Pillow per #91).
    textures = {"color": b"\x89PNG\r\n\x1a\nFAKE"}
    with (
        patch.object(c, "_scalars_for", return_value=scalars),
        patch.object(c, "fetch_all_textures", return_value=textures),
    ):
        got = a.to_gltf()
    assert got == to_gltf(scalars, textures)


def test_to_mtlx_returns_mtlx_source_with_matching_identity():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    m = a.to_mtlx()
    assert isinstance(m, MtlxSource)
    assert m.source == "ambientcg"
    assert m.material_id == "Wood080"
    assert m.tier == "1k"
    assert m.is_original is False


# ── Factory on the client ────────────────────────────────────


def test_client_asset_factory_returns_visasset():
    c = MatVisClient()
    a = c.asset("ambientcg", "Wood080", tier="1k")
    assert isinstance(a, VisAsset)
    assert a.source == "ambientcg"
    assert a.material_id == "Wood080"
    assert a.tier == "1k"


def test_client_asset_factory_default_tier():
    # mat-vis#374: default tier flipped from "1k" to "auto" in 0.7.0.
    # The auto sentinel collapses to a concrete tier lazily on first
    # .textures access (see test_asset_resolved_tier_*).
    c = MatVisClient()
    a = c.asset("ambientcg", "Wood080")
    assert a.tier == "auto"


def test_client_asset_factory_holds_client_reference():
    """The factory binds *this* client — so e.g. textures/scalars route through it."""
    c = MatVisClient()
    a = c.asset("ambientcg", "Wood080", tier="1k")
    expected = {"roughness": 0.7}
    with patch.object(c, "_scalars_for", return_value=expected) as mock:
        assert a.scalars == expected
    assert mock.call_count == 1


# ── Repr ────────────────────────────────────────────────────────


def test_repr_round_trip_identity():
    c = MatVisClient()
    a = VisAsset(c, "ambientcg", "Wood080", "1k")
    r = repr(a)
    assert "VisAsset" in r
    assert "ambientcg" in r
    assert "Wood080" in r
    assert "1k" in r


# ── Public API export ───────────────────────────────────────────


def test_visasset_importable_from_package_root():
    from mat_vis_client import VisAsset as Imported

    assert Imported is VisAsset


# ── Scalar-only sources short-circuit texture fetch (mat-vis#288) ──


def _scalar_only_index_entry(material_id: str) -> dict:
    """Build a scalar-only v3 index entry (available_tiers=[]) like physicallybased."""
    return {
        "id": material_id,
        "source": "physicallybased",
        "mat_vis": {
            "name": material_id,
            "category": "metal",
            "pbr": {
                "color_rgb": [0.91, 0.92, 0.92],
                "roughness": 0.5,
                "metalness": 1.0,
                "ior": 1.39,
            },
        },
        "available_tiers": [],
        "maps": [],
    }


def test_textures_returns_empty_for_scalar_only_source():
    """VisAsset.textures must short-circuit when the index entry has
    no staged tiers (scalar-only source like physicallybased), instead
    of calling fetch_all_textures (which raises MaterialNotStagedError)."""
    c = MatVisClient()
    a = VisAsset(c, "physicallybased", "Aluminum", "1k")
    entries = [_scalar_only_index_entry("Aluminum")]
    with (
        patch.object(c, "index", return_value=entries),
        patch.object(c, "fetch_all_textures") as fetch_mock,
    ):
        assert a.textures == {}
    fetch_mock.assert_not_called()


def test_to_threejs_scalar_only_source_returns_scalars_no_textures():
    """Repro for mat-vis#288: physicallybased + to_threejs should return
    scalars-only output, not raise MaterialNotStagedError."""
    c = MatVisClient()
    a = VisAsset(c, "physicallybased", "Aluminum", "1k")
    entries = [_scalar_only_index_entry("Aluminum")]
    with patch.object(c, "index", return_value=entries):
        result = a.to_threejs(color_format="int")
    # Scalars come through.
    assert result["type"] == "MeshPhysicalMaterial"
    assert result.get("metalness") == 1.0
    assert result.get("roughness") == 0.5
    assert result.get("ior") == 1.39
    assert "color" in result
    # No texture maps were fetched.
    for k in ("map", "normalMap", "roughnessMap", "metalnessMap", "aoMap"):
        assert k not in result


def test_textures_still_fetched_for_staged_source():
    """Regression: textured sources (e.g. gpuopen Chrome staged for 1k)
    must still route through fetch_all_textures normally."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Chrome", "1k")
    staged_entry = {
        "id": "Chrome",
        "source": "gpuopen",
        "mat_vis": {"name": "Chrome", "pbr": {}},
        "available_tiers": ["1k"],
        "maps": ["color", "normal"],
    }
    expected = {"color": b"PNG-color", "normal": b"PNG-normal"}
    with (
        patch.object(c, "index", return_value=[staged_entry]),
        patch.object(c, "fetch_all_textures", return_value=expected) as fetch_mock,
    ):
        assert a.textures == expected
    fetch_mock.assert_called_once_with("gpuopen", "Chrome", "1k")
