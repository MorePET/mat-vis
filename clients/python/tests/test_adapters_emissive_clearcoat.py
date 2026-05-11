"""Tests for emissive + clearcoat scalar coverage (ADR-0013 §Decision-3).

py-mat's ``Vis`` dataclass exposes ``emissive`` (RGB float-3) and
``clearcoat`` (float 0-1) as part of its public PBR-spec mirror.
Adapters previously dropped both keys silently — only the five-key
allowlist (metalness/roughness/color_hex/ior/transmission) flowed
through. ADR-0013 expands the input schema to mirror Three.js
MeshPhysicalMaterial + glTF 2.0.

Output bindings:
    | scalar    | to_threejs           | to_gltf                              | export_mtlx                                  |
    |-----------|----------------------|--------------------------------------|----------------------------------------------|
    | emissive  | result["emissive"]   | material["emissiveFactor"] (core)   | <input name="emissiveColor" type="color3">  |
    | clearcoat | result["clearcoat"]  | KHR_materials_clearcoat extension    | (n/a — UsdPreviewSurface lacks clearcoat)   |

clearcoat at the spec default 0.0 is omitted (no-op extension entry,
mirroring the existing ior=1.5 / transmission=0.0 suppression pattern).

#302.
"""

from __future__ import annotations

from pathlib import Path


from mat_vis_client.adapters import export_mtlx, to_gltf, to_threejs


class TestEmissiveToThreejs:
    def test_emissive_emitted_as_array(self):
        result = to_threejs({"emissive": (1.0, 0.5, 0.0)})
        assert result["emissive"] == [1.0, 0.5, 0.0]

    def test_emissive_list_input_accepted(self):
        result = to_threejs({"emissive": [0.2, 0.4, 0.6]})
        assert result["emissive"] == [0.2, 0.4, 0.6]

    def test_emissive_absent_when_not_in_scalars(self):
        result = to_threejs({})
        assert "emissive" not in result

    def test_emissive_absent_when_none(self):
        result = to_threejs({"emissive": None})
        assert "emissive" not in result


class TestEmissiveToGltf:
    def test_emissive_emitted_as_emissive_factor(self):
        result = to_gltf({"emissive": (1.0, 0.5, 0.0)})
        # emissiveFactor is a CORE glTF 2.0 material field — not under
        # an extension. Lives at material["emissiveFactor"].
        assert result["emissiveFactor"] == [1.0, 0.5, 0.0]

    def test_emissive_absent_when_not_in_scalars(self):
        result = to_gltf({})
        assert "emissiveFactor" not in result

    def test_emissive_absent_when_none(self):
        result = to_gltf({"emissive": None})
        assert "emissiveFactor" not in result


class TestEmissiveExportMtlx:
    def test_emissive_emits_color3_input(self, tmp_path: Path):
        out = export_mtlx({"emissive": (1.0, 0.5, 0.0)}, output_dir=tmp_path)
        xml = out.read_text()
        # UsdPreviewSurface emissiveColor input — comma-separated RGB.
        assert 'name="emissiveColor"' in xml
        assert 'type="color3"' in xml
        # Float formatting must be unambiguous; %g suppresses trailing zeros.
        assert 'value="1,0.5,0"' in xml

    def test_emissive_absent_when_not_in_scalars(self, tmp_path: Path):
        out = export_mtlx({}, output_dir=tmp_path)
        xml = out.read_text()
        assert "emissiveColor" not in xml


class TestClearcoatToThreejs:
    def test_clearcoat_emitted(self):
        result = to_threejs({"clearcoat": 0.5})
        assert result["clearcoat"] == 0.5

    def test_clearcoat_zero_emitted_explicitly(self):
        # Three.js MeshPhysicalMaterial.clearcoat = 0 is a meaningful
        # explicit "no clearcoat" — distinct from "absent". Three.js
        # accepts the value either way; we emit on presence to keep the
        # adapter dumb.
        result = to_threejs({"clearcoat": 0.0})
        assert result["clearcoat"] == 0.0

    def test_clearcoat_absent_when_not_in_scalars(self):
        result = to_threejs({})
        assert "clearcoat" not in result

    def test_clearcoat_absent_when_none(self):
        result = to_threejs({"clearcoat": None})
        assert "clearcoat" not in result


class TestClearcoatToGltf:
    def test_clearcoat_emits_khr_extension(self):
        result = to_gltf({"clearcoat": 0.5})
        ext = result["extensions"]["KHR_materials_clearcoat"]
        assert ext["clearcoatFactor"] == 0.5

    def test_clearcoat_at_default_omitted(self):
        # Mirrors the ior=1.5 / transmission=0.0 default-suppression
        # pattern: a KHR extension entry that exactly matches the spec
        # default is a no-op and bloats output.
        result = to_gltf({"clearcoat": 0.0})
        assert "KHR_materials_clearcoat" not in result.get("extensions", {})

    def test_clearcoat_absent_when_not_in_scalars(self):
        result = to_gltf({})
        assert "KHR_materials_clearcoat" not in result.get("extensions", {})

    def test_clearcoat_absent_when_none(self):
        result = to_gltf({"clearcoat": None})
        assert "KHR_materials_clearcoat" not in result.get("extensions", {})


class TestEmissionFactorToThreejs:
    """Emission scalar coverage (#406 / #405 Phase 3a). ``emission``
    (factor) + ``emission_color`` (RGB tint) flow from the substrate to
    Three.js via the HDR split: SDR cases write ``emissive`` alone;
    HDR cases (factor > 1) add ``emissiveIntensity``. The legacy
    ``emissive`` key (RGB triple) stays a backward-compat passthrough."""

    def test_emission_sdr_writes_emissive_only(self):
        # Factor at 0.5 + green tint → emissive = green * 0.5 (clamped to
        # SDR range). No emissiveIntensity (Three.js default is 1.0).
        result = to_threejs({"emission": 0.5, "emission_color": [0.0, 1.0, 0.5]})
        assert result["emissive"] == [0.0, 0.5, 0.25]
        assert "emissiveIntensity" not in result

    def test_emission_at_unit_writes_emissive_only(self):
        # Factor = 1.0 → emissive carries the full color; intensity stays
        # at the Three.js default, so the adapter doesn't emit it.
        result = to_threejs({"emission": 1.0, "emission_color": [0.8, 0.4, 0.2]})
        assert result["emissive"] == [0.8, 0.4, 0.2]
        assert "emissiveIntensity" not in result

    def test_emission_hdr_splits_color_and_intensity(self):
        # Factor = 3.5 → color clamped to SDR (×1), intensity carries
        # the HDR multiplier. This is the Three.js HDR mechanism
        # (intensity * color). Renderer math reconstructs 3.5 × color.
        result = to_threejs({"emission": 3.5, "emission_color": [1.0, 0.5, 0.0]})
        assert result["emissive"] == [1.0, 0.5, 0.0]
        assert result["emissiveIntensity"] == 3.5

    def test_emission_without_color_defaults_to_white(self):
        # MTLX ``<standard_surface>`` default for emission_color is
        # (1, 1, 1) — when only the factor is authored, emit a white
        # emissive at the factor's SDR clamp.
        result = to_threejs({"emission": 2.0})
        assert result["emissive"] == [1.0, 1.0, 1.0]
        assert result["emissiveIntensity"] == 2.0

    def test_emission_zero_suppresses_output(self):
        # 3160/3160 corpus materials author emission=0 — the SDR-zero
        # case is a no-op, suppress to keep the Three.js dict clean.
        result = to_threejs({"emission": 0.0, "emission_color": [1.0, 0.0, 0.0]})
        assert "emissive" not in result
        assert "emissiveIntensity" not in result

    def test_emission_none_with_color_none_suppresses(self):
        # Neither field authored → no emissive output.
        result = to_threejs({"emission": None, "emission_color": None})
        assert "emissive" not in result
        assert "emissiveIntensity" not in result

    def test_emission_factor_wins_over_legacy_emissive(self):
        # Adapter contract: ``emission`` (HDR factor) carries information
        # the legacy ``emissive`` key (RGB triple, no factor) cannot
        # express. When both arrive, ``emission`` wins so HDR substrate
        # values aren't silently downgraded.
        result = to_threejs(
            {
                "emissive": (0.1, 0.1, 0.1),  # legacy
                "emission": 5.0,
                "emission_color": [1.0, 1.0, 1.0],
            }
        )
        assert result["emissive"] == [1.0, 1.0, 1.0]
        assert result["emissiveIntensity"] == 5.0


class TestEmissionFactorToGltf:
    """glTF emission output for the factor + color split (#406)."""

    def test_emission_sdr_writes_factor_only(self):
        result = to_gltf({"emission": 0.5, "emission_color": [0.0, 1.0, 0.5]})
        assert result["emissiveFactor"] == [0.0, 0.5, 0.25]
        assert "KHR_materials_emissive_strength" not in result.get("extensions", {})

    def test_emission_at_unit_writes_factor_only(self):
        # SDR boundary (factor = 1.0) — KHR extension omitted (default).
        result = to_gltf({"emission": 1.0, "emission_color": [0.8, 0.4, 0.2]})
        assert result["emissiveFactor"] == [0.8, 0.4, 0.2]
        assert "KHR_materials_emissive_strength" not in result.get("extensions", {})

    def test_emission_hdr_emits_strength_extension(self):
        # HDR — KHR_materials_emissive_strength carries the factor;
        # emissiveFactor stays in SDR range. Spec:
        # https://github.com/KhronosGroup/glTF/blob/main/extensions/2.0/Khronos/KHR_materials_emissive_strength
        result = to_gltf({"emission": 3.5, "emission_color": [1.0, 0.5, 0.0]})
        assert result["emissiveFactor"] == [1.0, 0.5, 0.0]
        ext = result["extensions"]["KHR_materials_emissive_strength"]
        assert ext["emissiveStrength"] == 3.5

    def test_emission_without_color_defaults_to_white(self):
        result = to_gltf({"emission": 2.0})
        assert result["emissiveFactor"] == [1.0, 1.0, 1.0]
        assert result["extensions"]["KHR_materials_emissive_strength"]["emissiveStrength"] == 2.0

    def test_emission_zero_suppresses_output(self):
        result = to_gltf({"emission": 0.0, "emission_color": [1.0, 0.0, 0.0]})
        assert "emissiveFactor" not in result
        assert "KHR_materials_emissive_strength" not in result.get("extensions", {})

    def test_emission_factor_wins_over_legacy_emissive(self):
        result = to_gltf(
            {
                "emissive": (0.1, 0.1, 0.1),
                "emission": 5.0,
                "emission_color": [1.0, 1.0, 1.0],
            }
        )
        assert result["emissiveFactor"] == [1.0, 1.0, 1.0]
        assert result["extensions"]["KHR_materials_emissive_strength"]["emissiveStrength"] == 5.0


class TestClearcoatMtlxSkipped:
    """UsdPreviewSurface 1.38 has no clearcoat input — adapter drops it silently
    with no error. (Future MaterialX shaders may; revisit then.)"""

    def test_no_clearcoat_artifact_in_mtlx(self, tmp_path: Path):
        out = export_mtlx({"clearcoat": 0.5}, output_dir=tmp_path)
        xml = out.read_text()
        assert "clearcoat" not in xml.lower()
