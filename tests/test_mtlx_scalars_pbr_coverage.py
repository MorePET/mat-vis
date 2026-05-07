"""Bake-side extraction tests for #340 — full MeshPhysicalMaterial PBR coverage.

5 new scalar fields + 1 conditional + 1 color3:
- coat_roughness         → clearcoat_roughness (float)
- specular               → specular_intensity (float)
- specular_color         → specular_color (color3, linear)
- transmission_dispersion → dispersion (float)
- transmission_depth     → thickness (float, only when transmission > 0)

All extracted from <standard_surface> direct `value=` attributes.
Survey of 454 gpuopen materials confirms each input is authored
~80-95% of the time; specular_color is 94% non-default.
"""

from __future__ import annotations

import pytest

from mat_vis_baker._mtlx_scalars import parse_standard_surface_scalars


def _wrap(*surface_inputs: str) -> str:
    """Minimal materialx with a <standard_surface> shader carrying the
    given <input> children verbatim."""
    body = "\n    ".join(surface_inputs)
    return f"""<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="SR_T" type="surfaceshader">
    {body}
  </standard_surface>
  <surfacematerial name="T" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="SR_T"/>
  </surfacematerial>
</materialx>
"""


class TestSpecularIntensity:
    def test_specular_authored_non_default(self) -> None:
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="specular" type="float" value="0.6"/>')
        )
        assert pbr.specular_intensity == pytest.approx(0.6)

    def test_specular_default_extracted_too(self) -> None:
        # Survey shows 425/454 use default 1.0 — extracting it confirms
        # provenance even at default. Adapter suppresses spec defaults.
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="specular" type="float" value="1.0"/>')
        )
        assert pbr.specular_intensity == pytest.approx(1.0)

    def test_specular_unset_stays_none(self) -> None:
        pbr = parse_standard_surface_scalars(_wrap())
        assert pbr.specular_intensity is None


class TestSpecularColor:
    def test_specular_color_authored_tinted(self) -> None:
        # 428/454 corpus materials author with non-white tints.
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="specular_color" type="color3" value="0.95, 0.64, 0.54"/>')
        )
        assert pbr.specular_color == pytest.approx([0.95, 0.64, 0.54])

    def test_specular_color_default_white(self) -> None:
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="specular_color" type="color3" value="1, 1, 1"/>')
        )
        assert pbr.specular_color == pytest.approx([1.0, 1.0, 1.0])

    def test_specular_color_unset_stays_none(self) -> None:
        pbr = parse_standard_surface_scalars(_wrap())
        assert pbr.specular_color is None


class TestClearcoatRoughness:
    def test_coat_roughness_authored_non_default(self) -> None:
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="coat_roughness" type="float" value="0.35"/>')
        )
        assert pbr.clearcoat_roughness == pytest.approx(0.35)

    def test_coat_roughness_default_extracted(self) -> None:
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="coat_roughness" type="float" value="0.1"/>')
        )
        assert pbr.clearcoat_roughness == pytest.approx(0.1)


class TestDispersion:
    def test_dispersion_authored(self) -> None:
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="transmission_dispersion" type="float" value="0.25"/>')
        )
        assert pbr.dispersion == pytest.approx(0.25)

    def test_dispersion_default_zero(self) -> None:
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="transmission_dispersion" type="float" value="0.0"/>')
        )
        assert pbr.dispersion == pytest.approx(0.0)


class TestThickness:
    def test_thickness_extracted_only_when_transmission_nonzero(self) -> None:
        # Real gpuopen shape: transparent solids author transmission >0
        # AND transmission_depth > 0. The depth without transmission is
        # meaningless (the material is opaque) — emit None.
        pbr_transparent = parse_standard_surface_scalars(
            _wrap(
                '<input name="transmission" type="float" value="0.8"/>',
                '<input name="transmission_depth" type="float" value="10.0"/>',
            )
        )
        assert pbr_transparent.thickness == pytest.approx(10.0)
        assert pbr_transparent.transmission == pytest.approx(0.8)

    def test_thickness_none_when_transmission_zero(self) -> None:
        # 446/454 materials have transmission=0; their transmission_depth
        # value (often >0 for SSS scaffolding) is not meaningful as a
        # KHR_materials_volume thicknessFactor.
        pbr_opaque = parse_standard_surface_scalars(
            _wrap(
                '<input name="transmission" type="float" value="0.0"/>',
                '<input name="transmission_depth" type="float" value="10.0"/>',
            )
        )
        assert pbr_opaque.thickness is None

    def test_thickness_none_when_transmission_unset(self) -> None:
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="transmission_depth" type="float" value="10.0"/>')
        )
        assert pbr.thickness is None


class TestPBRBlockFieldsExist:
    def test_new_fields_default_to_none(self) -> None:
        from mat_vis_baker.common import PBRBlock

        pbr = PBRBlock()
        for fname in (
            "clearcoat_roughness",
            "specular_intensity",
            "specular_color",
            "thickness",
            "dispersion",
        ):
            assert getattr(pbr, fname) is None, f"{fname} should default to None"

    def test_fields_are_settable(self) -> None:
        from mat_vis_baker.common import PBRBlock

        pbr = PBRBlock()
        pbr.clearcoat_roughness = 0.2
        pbr.specular_intensity = 0.8
        pbr.specular_color = [0.95, 0.5, 0.3]
        pbr.thickness = 5.0
        pbr.dispersion = 0.1
        assert pbr.clearcoat_roughness == 0.2
        assert pbr.specular_intensity == 0.8
        assert pbr.specular_color == [0.95, 0.5, 0.3]
        assert pbr.thickness == 5.0
        assert pbr.dispersion == 0.1
