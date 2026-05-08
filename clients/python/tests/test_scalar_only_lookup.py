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
