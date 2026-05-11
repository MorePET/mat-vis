"""Adapter field passthrough — full PBR coverage from substrate to adapter (mat-vis#380).

Pre-fix: ``MatVisClient._scalars_for`` only read 4 of the 14+ pbr fields
the substrate emits (``roughness`` / ``metalness`` / ``ior`` /
``color_rgb``). The adapter passes through what it gets, so glass-class,
coated, and authored-specular materials silently rendered as opaque
default-grey because their distinguishing scalars never reached
Three.js / glTF.

These tests pin the read-side contract: every scalar field the
substrate emits under ``mat_vis.pbr`` that the adapters can consume
must reach the adapter in a form ``to_threejs`` / ``to_gltf`` already
recognize. Dumb-adapter contract (ADR-0013 / mat-vis#290) — values
flow verbatim.

Mocked indexes — no network IO.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mat_vis_client import MatVisClient
from mat_vis_client.adapters import to_gltf, to_threejs


# ── Fixtures ───────────────────────────────────────────────────


def _entry(mid: str, *, name: str | None = None, **pbr_overrides) -> dict:
    """v3 catalog entry with full-PBR overrides under ``mat_vis.pbr``.

    Mirrors the substrate shape: lowercase canonical ``id``, display
    ``name`` under ``mat_vis``. Pass any pbr fields as kwargs.
    """
    pbr: dict = {
        "color_rgb": None,
        "roughness": None,
        "metalness": None,
        "ior": None,
        "specular_f0": None,
        "transmission": None,
        "complex_ior": None,
    }
    pbr.update(pbr_overrides)
    return {
        "id": mid,
        "mat_vis": {
            "name": name or mid,
            "pbr": pbr,
        },
    }


# Frosted glass — exercises the transmission / dispersion / thickness /
# specular_intensity / specular_color stack the issue called out as
# load-bearing for the fingerprint regression.
GLASS_INDEX = [
    _entry(
        "frosted-glass",
        name="Frosted Glass",
        roughness=0.4,
        metalness=0.0,
        ior=1.5,
        transmission=0.8,
        thickness=10.0,
        dispersion=0.25,
        specular_intensity=0.5,
        specular_color=[1.0, 1.0, 1.0],
        color_rgb=[0.95, 0.95, 0.95],
    ),
]

# Clearcoated paint — exercises the clearcoat / clearcoat_roughness path.
COATED_INDEX = [
    _entry(
        "varnished-wood",
        name="Varnished Wood",
        roughness=0.6,
        metalness=0.0,
        clearcoat=1.0,
        clearcoat_roughness=0.1,
        color_rgb=[0.4, 0.25, 0.1],
    ),
]

# Emissive — exercises the RGB-list emissive passthrough.
EMISSIVE_INDEX = [
    _entry(
        "led-strip",
        name="LED Strip",
        roughness=0.5,
        metalness=0.0,
        emissive=[0.0, 1.0, 0.5],
        color_rgb=[0.1, 0.1, 0.1],
    ),
]

# Emission factor + color (#406) — exercises the MTLX-derived split
# emission scalar coverage. SDR case: factor in [0, 1].
EMISSION_SDR_INDEX = [
    _entry(
        "glowing-decal",
        name="Glowing Decal",
        roughness=0.5,
        metalness=0.0,
        emission=1.0,
        emission_color=[0.0, 1.0, 0.5],
        color_rgb=[0.1, 0.1, 0.1],
    ),
]

# Emission HDR — factor > 1, exercises the emissiveIntensity /
# KHR_materials_emissive_strength split on the adapter side.
EMISSION_HDR_INDEX = [
    _entry(
        "led-sign",
        name="LED Sign",
        roughness=0.5,
        metalness=0.0,
        emission=4.0,
        emission_color=[1.0, 0.4, 0.1],
        color_rgb=[0.1, 0.1, 0.1],
    ),
]

# Iridescent — exercises the iridescence_thickness / iridescence_ior
# path (mat-vis#408). Soap-bubble regime: ~400 nm thin film with
# IOR slightly above water.
IRIDESCENT_INDEX = [
    _entry(
        "soap-bubble",
        name="Soap Bubble",
        roughness=0.05,
        metalness=0.0,
        ior=1.33,
        iridescence_thickness=400.0,
        iridescence_ior=1.33,
        color_rgb=[1.0, 1.0, 1.0],
    ),
]


# ── Read-side: _scalars_for forwards each new field ────────────


class TestScalarsForFullPbrPassthrough:
    """Pin the read-side contract — _scalars_for must surface every
    PBR field the adapter knows how to render."""

    def test_transmission_passes_through(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        assert scalars["transmission"] == pytest.approx(0.8)

    def test_thickness_passes_through(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        assert scalars["thickness"] == pytest.approx(10.0)

    def test_dispersion_passes_through(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        assert scalars["dispersion"] == pytest.approx(0.25)

    def test_specular_intensity_passes_through(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        assert scalars["specular_intensity"] == pytest.approx(0.5)

    def test_specular_color_forwarded_as_linear(self):
        """Substrate authors specular_color in linear RGB (PBRBlock
        docstring); _scalars_for forwards it under the
        ``specular_color_linear`` alias the adapters' resolver consumes,
        avoiding a re-de-gamma at the boundary."""
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        assert scalars["specular_color_linear"] == [1.0, 1.0, 1.0]
        # Raw key not passed through under the substrate name (would be
        # ambiguous w.r.t. colorspace under the adapter resolver).
        assert "specular_color" not in scalars

    def test_clearcoat_passes_through(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=COATED_INDEX):
            scalars = c._scalars_for("physicallybased", "Varnished Wood")
        assert scalars["clearcoat"] == pytest.approx(1.0)

    def test_clearcoat_roughness_passes_through(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=COATED_INDEX):
            scalars = c._scalars_for("physicallybased", "Varnished Wood")
        assert scalars["clearcoat_roughness"] == pytest.approx(0.1)

    def test_emissive_passes_through_as_list(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=EMISSIVE_INDEX):
            scalars = c._scalars_for("physicallybased", "LED Strip")
        assert scalars["emissive"] == [0.0, 1.0, 0.5]

    def test_emission_factor_and_color_pass_through(self):
        """#406 — ``emission`` factor + ``emission_color`` round-trip."""
        c = MatVisClient()
        with patch.object(c, "index", return_value=EMISSION_HDR_INDEX):
            scalars = c._scalars_for("physicallybased", "LED Sign")
        assert scalars["emission"] == pytest.approx(4.0)
        assert scalars["emission_color"] == [1.0, 0.4, 0.1]

    def test_iridescence_thickness_passes_through(self):
        # mat-vis#408 — forward-looking field; substrate carries
        # ``iridescence_thickness`` in nm and ``iridescence_ior``.
        c = MatVisClient()
        with patch.object(c, "index", return_value=IRIDESCENT_INDEX):
            scalars = c._scalars_for("physicallybased", "Soap Bubble")
        assert scalars["iridescence_thickness"] == pytest.approx(400.0)
        assert scalars["iridescence_ior"] == pytest.approx(1.33)

    def test_existing_4_field_contract_preserved(self):
        """Regression guard — the original
        roughness/metalness/ior/color_hex fields still round-trip
        unchanged under the new code path."""
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        assert scalars["roughness"] == pytest.approx(0.4)
        assert scalars["metalness"] == pytest.approx(0.0)
        assert scalars["ior"] == pytest.approx(1.5)
        # color_rgb [0.95, 0.95, 0.95] -> #F2F2F2
        assert scalars["color_hex"] == "#F2F2F2"

    def test_none_fields_omitted(self):
        """Dumb-adapter contract — missing fields stay missing, no
        defaults injected. Pre-fix this behaviour was implicit; pin it."""
        opaque = [_entry("aluminum", name="Aluminum", roughness=0.18, metalness=1.0)]
        c = MatVisClient()
        with patch.object(c, "index", return_value=opaque):
            scalars = c._scalars_for("physicallybased", "Aluminum")
        for k in (
            "transmission",
            "thickness",
            "dispersion",
            "clearcoat",
            "clearcoat_roughness",
            "specular_intensity",
            "specular_color_linear",
            "emissive",
            # Emission scalar coverage (#406) — additive, defaults to absent.
            "emission",
            "emission_color",
            # Iridescence (#408) — additive forward-looking fields.
            "iridescence_thickness",
            "iridescence_ior",
        ):
            assert k not in scalars, f"{k!r} should not be injected when substrate is None"


# ── End-to-end: scalars → to_threejs / to_gltf ─────────────────


class TestEndToEndThreejs:
    """End-to-end: substrate index → _scalars_for → to_threejs emits
    the MeshPhysicalMaterial keys downstream Three.js renderers need."""

    def test_glass_emits_transmission_thickness_dispersion(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        result = to_threejs(scalars)
        assert result["transmission"] == pytest.approx(0.8)
        assert result["thickness"] == pytest.approx(10.0)
        assert result["dispersion"] == pytest.approx(0.25)
        assert result["specularIntensity"] == pytest.approx(0.5)
        # specular_color_linear [1,1,1] re-encodes to sRGB white.
        assert result["specularColor"] == "#ffffff"

    def test_coated_emits_clearcoat(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=COATED_INDEX):
            scalars = c._scalars_for("physicallybased", "Varnished Wood")
        result = to_threejs(scalars)
        assert result["clearcoat"] == pytest.approx(1.0)
        assert result["clearcoatRoughness"] == pytest.approx(0.1)

    def test_emissive_emits_three_tuple(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=EMISSIVE_INDEX):
            scalars = c._scalars_for("physicallybased", "LED Strip")
        result = to_threejs(scalars)
        assert result["emissive"] == [0.0, 1.0, 0.5]

    def test_emission_sdr_emits_emissive_only(self):
        # #406 SDR end-to-end.
        c = MatVisClient()
        with patch.object(c, "index", return_value=EMISSION_SDR_INDEX):
            scalars = c._scalars_for("physicallybased", "Glowing Decal")
        result = to_threejs(scalars)
        assert result["emissive"] == [0.0, 1.0, 0.5]
        assert "emissiveIntensity" not in result

    def test_emission_hdr_emits_emissive_plus_intensity(self):
        # #406 HDR end-to-end.
        c = MatVisClient()
        with patch.object(c, "index", return_value=EMISSION_HDR_INDEX):
            scalars = c._scalars_for("physicallybased", "LED Sign")
        result = to_threejs(scalars)
        assert result["emissive"] == [1.0, 0.4, pytest.approx(0.1)]
        assert result["emissiveIntensity"] == pytest.approx(4.0)

    def test_iridescent_emits_iridescence_keys(self):
        # mat-vis#408 — Three.js end-to-end.
        c = MatVisClient()
        with patch.object(c, "index", return_value=IRIDESCENT_INDEX):
            scalars = c._scalars_for("physicallybased", "Soap Bubble")
        result = to_threejs(scalars)
        assert result["iridescence"] == 1.0
        assert result["iridescenceThicknessRange"] == [0.0, 400.0]
        assert result["iridescenceIOR"] == pytest.approx(1.33)


class TestEndToEndGltf:
    """End-to-end: substrate index → _scalars_for → to_gltf emits the
    KHR extension blocks downstream glTF 2.0 consumers need."""

    def test_glass_emits_transmission_extension(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        result = to_gltf(scalars)
        ext = result["extensions"]
        assert ext["KHR_materials_transmission"] == {"transmissionFactor": 0.8}

    def test_glass_emits_volume_thickness_extension(self):
        """KHR_materials_volume.thicknessFactor only ships when transmission > 0."""
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        result = to_gltf(scalars)
        ext = result["extensions"]
        assert ext["KHR_materials_volume"] == {"thicknessFactor": 10.0}

    def test_glass_emits_dispersion_extension(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        result = to_gltf(scalars)
        ext = result["extensions"]
        assert ext["KHR_materials_dispersion"] == {"dispersion": 0.25}

    def test_glass_emits_specular_extension(self):
        """specular_intensity=0.5 is non-default; specular_color [1,1,1]
        is the spec default and must be omitted from the extension."""
        c = MatVisClient()
        with patch.object(c, "index", return_value=GLASS_INDEX):
            scalars = c._scalars_for("physicallybased", "Frosted Glass")
        result = to_gltf(scalars)
        ext = result["extensions"]
        assert ext["KHR_materials_specular"] == {"specularFactor": 0.5}

    def test_coated_emits_clearcoat_extension(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=COATED_INDEX):
            scalars = c._scalars_for("physicallybased", "Varnished Wood")
        result = to_gltf(scalars)
        ext = result["extensions"]
        assert ext["KHR_materials_clearcoat"] == {
            "clearcoatFactor": 1.0,
            "clearcoatRoughnessFactor": 0.1,
        }

    def test_emissive_emits_factor(self):
        c = MatVisClient()
        with patch.object(c, "index", return_value=EMISSIVE_INDEX):
            scalars = c._scalars_for("physicallybased", "LED Strip")
        result = to_gltf(scalars)
        assert result["emissiveFactor"] == [0.0, 1.0, 0.5]

    def test_emission_sdr_emits_factor_only(self):
        # #406 SDR end-to-end: factor=1.0 → emissiveFactor carries the
        # color; KHR_materials_emissive_strength omitted (default).
        c = MatVisClient()
        with patch.object(c, "index", return_value=EMISSION_SDR_INDEX):
            scalars = c._scalars_for("physicallybased", "Glowing Decal")
        result = to_gltf(scalars)
        assert result["emissiveFactor"] == [0.0, 1.0, 0.5]
        assert "KHR_materials_emissive_strength" not in result.get("extensions", {})

    def test_emission_hdr_emits_strength_extension(self):
        # #406 HDR end-to-end: factor=4.0 → emissiveFactor clamped to
        # SDR; KHR_materials_emissive_strength carries the multiplier.
        c = MatVisClient()
        with patch.object(c, "index", return_value=EMISSION_HDR_INDEX):
            scalars = c._scalars_for("physicallybased", "LED Sign")
        result = to_gltf(scalars)
        assert result["emissiveFactor"] == [1.0, 0.4, pytest.approx(0.1)]
        ext = result["extensions"]["KHR_materials_emissive_strength"]
        assert ext["emissiveStrength"] == pytest.approx(4.0)

    def test_iridescent_emits_iridescence_extension(self):
        # mat-vis#408 — glTF end-to-end.
        c = MatVisClient()
        with patch.object(c, "index", return_value=IRIDESCENT_INDEX):
            scalars = c._scalars_for("physicallybased", "Soap Bubble")
        result = to_gltf(scalars)
        ext = result["extensions"]["KHR_materials_iridescence"]
        assert ext["iridescenceFactor"] == 1.0
        assert ext["iridescenceThicknessMaximum"] == 400.0
        assert ext["iridescenceIor"] == pytest.approx(1.33)
