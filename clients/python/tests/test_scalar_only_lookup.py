"""Scalar-only path bug fixes (mat-vis#368, mat-vis#370).

Two bugs surfaced in the scalar-only render path (pymat
``test_visual_regression.py::SCALAR_ONLY``: the case rendered as
default-grey because both lookups failed silently for the typical
display-name input).

Bug A (mat-vis#368): ``MatVisClient._scalars_for`` did a
case-sensitive ``entry["id"] != material_id`` comparison. Substrate
stores normalized lowercase ids (``"aluminum"``); callers pass
capitalised display names (``"Aluminum"``). Result: silent ``{}``,
every PBR scalar dropped.

Bug B (mat-vis#370): ``_resolve_material_id`` raised
:class:`MaterialNotStagedError` when ``tier="scalar"`` was passed for
scalar-only sources. ``"scalar"`` is the convention sentinel for
scalar-only callers (cf. pymat ``TIERS_SCALAR = ["scalar"]``), not a
missing bake — the resolver should accept it.

These tests pin both behaviours with mocked indexes so no network IO
happens.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mat_vis_client import MaterialNotStagedError, MatVisClient


# ── Test fixtures ──────────────────────────────────────────────


def _scalar_entry(
    mid: str,
    *,
    name: str | None = None,
    r: float = 0.5,
    m: float = 0.0,
    ior: float | None = None,
    color_rgb: list[float] | None = None,
    tiers: list[str] | None = None,
) -> dict:
    """Minimal v3 catalog entry with PBR scalars under ``mat_vis.pbr``.

    Mirrors the substrate shape: lowercase canonical ``id``, display
    ``name`` under ``mat_vis``, optional ``available_tiers`` (omitted
    entirely for scalar-only sources).
    """
    entry: dict = {
        "id": mid,
        "mat_vis": {
            "name": name or mid,
            "pbr": {
                "roughness": r,
                "metalness": m,
                "ior": ior,
                "color_rgb": color_rgb,
            },
        },
    }
    if tiers is not None:
        entry["available_tiers"] = tiers
    return entry


# physicallybased: scalar-only, no available_tiers, lowercase ids,
# Title-Case display names. Mirrors the actual prod shape.
PB_INDEX = [
    _scalar_entry(
        "aluminum",
        name="Aluminum",
        r=0.18,
        m=1.0,
        color_rgb=[0.91, 0.92, 0.92],
    ),
    _scalar_entry(
        "plastic-acrylic",
        name="Plastic (Acrylic)",
        r=0.4,
        m=0.0,
        ior=1.49,
        color_rgb=[1.0, 1.0, 1.0],
    ),
]

# gpuopen: mixed — most entries have texture tiers, a scalar-only subset
# (18 entries) carries no available_tiers. Use UUID-shaped ids to mirror
# prod.
CHROME_UUID = "11111111-2222-3333-4444-555555555555"
GPUOPEN_INDEX = [
    _scalar_entry(
        CHROME_UUID,
        name="Chrome",
        r=0.05,
        m=1.0,
        color_rgb=[0.55, 0.56, 0.55],
    ),
    _scalar_entry(
        "66666666-7777-8888-9999-000000000000",
        name="Wood Oak",
        r=0.7,
        m=0.0,
        color_rgb=[0.4, 0.3, 0.2],
        tiers=["1k", "2k"],
    ),
]


# ── Bug A — case-insensitive _scalars_for (mat-vis#368) ────────


def test_scalars_for_case_insensitive_id():
    """``_scalars_for("physicallybased", "Aluminum")`` returns the full PBR.

    Pre-fix: case-sensitive ``entry["id"] != material_id`` returned ``{}``.
    """
    c = MatVisClient()
    with patch.object(c, "index", return_value=PB_INDEX):
        got = c._scalars_for("physicallybased", "Aluminum")
    assert got["roughness"] == 0.18
    assert got["metalness"] == 1.0
    assert got["color_hex"] == "#E8EBEB"


def test_scalars_for_lowercase_id_still_works():
    """Lowercase canonical id still resolves (regression guard)."""
    c = MatVisClient()
    with patch.object(c, "index", return_value=PB_INDEX):
        got = c._scalars_for("physicallybased", "aluminum")
    assert got["roughness"] == 0.18
    assert got["metalness"] == 1.0


def test_scalars_for_display_name_lookup():
    """Display name with punctuation/spaces resolves via mat_vis.name.

    ``"Plastic (Acrylic)"`` doesn't match the canonical
    ``"plastic-acrylic"`` id at all — only the name path can find it.
    """
    c = MatVisClient()
    with patch.object(c, "index", return_value=PB_INDEX):
        got = c._scalars_for("physicallybased", "Plastic (Acrylic)")
    assert got["roughness"] == 0.4
    assert got["ior"] == pytest.approx(1.49)


def test_scalars_for_unknown_returns_empty():
    """Silent-on-miss contract is preserved (no exception, just ``{}``)."""
    c = MatVisClient()
    with patch.object(c, "index", return_value=PB_INDEX):
        assert c._scalars_for("physicallybased", "Nonexistent") == {}


# ── Bug B — tier="scalar" sentinel (mat-vis#370) ───────────────


def test_resolve_material_id_tier_scalar_for_scalar_only_source():
    """``tier="scalar"`` on a scalar-only source returns the canonical id.

    Pre-fix: physicallybased entries have no ``available_tiers``, so
    ``"scalar" not in []`` always raised
    :class:`MaterialNotStagedError`. ``"scalar"`` is the sentinel
    (cf. pymat ``TIERS_SCALAR = ["scalar"]``), not a real tier.
    """
    c = MatVisClient()
    with patch.object(c, "index", return_value=PB_INDEX):
        resolved = c._resolve_material_id("physicallybased", "Aluminum", "scalar")
    assert resolved == "aluminum"


def test_resolve_material_id_tier_scalar_for_gpuopen_subset():
    """gpuopen scalar-only entries (18 in prod) accept ``tier="scalar"``."""
    c = MatVisClient()
    with patch.object(c, "index", return_value=GPUOPEN_INDEX):
        resolved = c._resolve_material_id("gpuopen", "Chrome", "scalar")
    assert resolved == CHROME_UUID


def test_resolve_material_id_tier_scalar_direct_id():
    """Direct lowercase id with tier="scalar" also resolves."""
    c = MatVisClient()
    with patch.object(c, "index", return_value=PB_INDEX):
        resolved = c._resolve_material_id("physicallybased", "aluminum", "scalar")
    assert resolved == "aluminum"


def test_resolve_material_id_tier_1k_still_enforces_staging():
    """Regression: existing tier="1k" callers still raise on missing bake.

    The scalar-sentinel fix must not break the ordinary
    ``MaterialNotStagedError`` path for real texture tiers.
    """
    c = MatVisClient()
    with patch.object(c, "index", return_value=PB_INDEX):
        with pytest.raises(MaterialNotStagedError):
            c._resolve_material_id("physicallybased", "Aluminum", "1k")


def test_resolve_material_id_tier_1k_staged_returns_id():
    """Regression: existing tier="1k" callers with a staged entry still work."""
    c = MatVisClient()
    with patch.object(c, "index", return_value=GPUOPEN_INDEX):
        resolved = c._resolve_material_id("gpuopen", "Wood Oak", "1k")
    assert resolved == "66666666-7777-8888-9999-000000000000"


# ── #369 — _is_scalar_only_entry across the four catalog shapes ───
#
# The substrate-shape contract for scalar-only entries shifted twice:
#
#   pre-#338 (legacy):              key absent       → scalar-only
#   #338 physicallybased fix:        ["scalar"]       → scalar-only
#   pre-#369 gpuopen subset:         key absent / []  → scalar-only
#   post-#369 gpuopen subset:        ["scalar"]       → scalar-only
#
# Clients must keep working across all four shapes — the legacy substrate
# tags (v2026.04.0/.1/.2) still ship missing-key gpuopen entries; future
# bakes will ship ["scalar"]. _is_scalar_only_entry has to accept both.


@pytest.fixture
def _client_with_index():
    """Helper: build a MatVisClient + asset, patch index, return both."""
    from mat_vis_client.client import VisAsset

    def _make(index_data: list[dict], material_id: str) -> tuple[MatVisClient, VisAsset]:
        c = MatVisClient()
        # Don't actually fetch — just stub the index.
        c.index = lambda src: index_data  # type: ignore[assignment]
        asset = VisAsset(client=c, source="test", material_id=material_id, tier="scalar")
        return c, asset

    return _make


@pytest.mark.parametrize(
    "tiers_value,expected_scalar_only",
    [
        # New post-#369 substrate convention.
        (["scalar"], True),
        # Legacy gpuopen scalar-only shape (key present, empty list).
        ([], True),
        # Legacy null shape (key present, value None).
        (None, True),
        # Textured entry — definitely not scalar-only.
        (["1k"], False),
        (["1k", "2k"], False),
        # Hypothetical mixed: ["1k", "scalar"] would mean "renderable both
        # ways" — for the scalar-only check, having ANY texture tier is
        # disqualifying.
        (["1k", "scalar"], False),
    ],
)
def test_is_scalar_only_entry_accepts_all_substrate_shapes(
    _client_with_index, tiers_value, expected_scalar_only
):
    """``_is_scalar_only_entry`` accepts ``["scalar"]`` (post-#369),
    ``[]`` and ``None`` (pre-#369 legacy), and rejects entries with any
    real texture tier."""
    entry: dict = {
        "id": "test-id",
        "mat_vis": {"name": "Test", "pbr": {"roughness": 0.1, "metalness": 1.0}},
    }
    if tiers_value is not None or "tiers_value" == "explicit-none":
        # pytest.parametrize passes None for "missing-key" too; we treat
        # the explicit None case the same as missing-key. To distinguish
        # from "absent", we add the key only when value is not None.
        if tiers_value is None:
            # explicit JSON null
            entry["available_tiers"] = None
        else:
            entry["available_tiers"] = tiers_value
    _, asset = _client_with_index([entry], "test-id")
    assert asset._is_scalar_only_entry() is expected_scalar_only


def test_is_scalar_only_entry_with_missing_key_is_scalar_only(_client_with_index):
    """Pre-#369 legacy substrates omit ``available_tiers`` entirely for
    scalar-only entries. Client must treat key-absence as scalar-only."""
    entry = {
        "id": "test-id",
        "mat_vis": {"name": "Test", "pbr": {"roughness": 0.1}},
        # no available_tiers key at all
    }
    _, asset = _client_with_index([entry], "test-id")
    assert asset._is_scalar_only_entry() is True


# ── #369 end-to-end: selection method returns scalars for new shape ──


def test_asset_textures_empty_for_post369_scalar_shape(_client_with_index):
    """Full flow: asset(...).textures returns {} for the new ``["scalar"]``
    shape without trying to fetch from HF.

    Pre-#369-shape regression vector: if ``_is_scalar_only_entry``
    incorrectly treated ``["scalar"]`` as textured, it would route to
    ``fetch_all_textures`` and either crash (no staged tier) or download
    nothing — either way silently broken. This pins the resilient path.
    """
    entry = {
        "id": "chrome-uuid",
        "mat_vis": {
            "name": "Chrome",
            "pbr": {"roughness": 0.05, "metalness": 1.0, "color_rgb": [0.55, 0.56, 0.55]},
        },
        "available_tiers": ["scalar"],
    }
    _, asset = _client_with_index([entry], "Chrome")
    # Should resolve via display-name → ``Chrome`` matches mat_vis.name.
    assert asset._is_scalar_only_entry() is True
    # textures triggers the lazy load — must NOT call fetch_all_textures.
    asset._client.fetch_all_textures = lambda *a, **kw: pytest.fail(  # type: ignore[assignment]
        "fetch_all_textures called for scalar-only entry"
    )
    assert asset.textures == {}
