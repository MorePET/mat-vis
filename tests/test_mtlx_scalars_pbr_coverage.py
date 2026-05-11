"""Bake-side extraction tests for #340 / #396 / #409 — full MeshPhysicalMaterial coverage.

Scalar fields extracted from ``<standard_surface>``:
- coat                   → clearcoat (float, #396)
- coat_roughness         → clearcoat_roughness (float, #340)
- specular               → specular_intensity (float, #340)
- specular_color         → specular_color (color3, linear, #340)
- transmission_dispersion → dispersion (float, #340)
- transmission_depth     → thickness (float, only when transmission > 0, #340)
- subsurface             → subsurface (float, #409)
- subsurface_color       → subsurface_color (color3, linear, #409)
- subsurface_radius      → subsurface_radius (color3, per-channel mfp, #409)

All extracted from <standard_surface> direct `value=` attributes or
1-hop nodegraph→<constant> chains. Survey of 454 gpuopen materials
confirms each input is authored ~80-95% of the time; specular_color
is 94% non-default; coat is 3/454 (Car Paint family); subsurface
is 13/454 with ``subsurface > 0`` (wax, resin, semi-translucent).
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


class TestClearcoat:
    """``coat`` factor extraction (#396) — the on/off switch for
    KHR_materials_clearcoat. Survey of 454 gpuopen materials: 451
    author the default 0.0, 3 (Car Paint family) author > 0."""

    def test_coat_authored_non_default(self) -> None:
        # Car Paint family authors coat=1.0 — fully-clearcoated.
        pbr = parse_standard_surface_scalars(_wrap('<input name="coat" type="float" value="1.0"/>'))
        assert pbr.clearcoat == pytest.approx(1.0)

    def test_coat_authored_partial(self) -> None:
        pbr = parse_standard_surface_scalars(_wrap('<input name="coat" type="float" value="0.5"/>'))
        assert pbr.clearcoat == pytest.approx(0.5)

    def test_coat_default_zero_extracted(self) -> None:
        # 451/454 corpus materials default to 0.0 — extracting it
        # confirms provenance. Adapter suppresses the spec-default
        # KHR extension entry.
        pbr = parse_standard_surface_scalars(_wrap('<input name="coat" type="float" value="0.0"/>'))
        assert pbr.clearcoat == pytest.approx(0.0)

    def test_coat_unset_stays_none(self) -> None:
        pbr = parse_standard_surface_scalars(_wrap())
        assert pbr.clearcoat is None

    def test_coat_texture_bound_stays_none(self) -> None:
        # 1 corpus material binds ``coat`` to a texture via nodegraph.
        # Consistent with metalness-texture-bound handling: leave the
        # field None; the baker carries the texture path separately.
        mtlx = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="NG_COAT">
    <image name="coat_img" type="float">
      <input name="file" type="filename" value="coat.png"/>
    </image>
    <output name="coat_out" type="float" nodename="coat_img"/>
  </nodegraph>
  <standard_surface name="SR_T" type="surfaceshader">
    <input name="coat" type="float" output="coat_out" nodegraph="NG_COAT"/>
  </standard_surface>
  <surfacematerial name="T" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="SR_T"/>
  </surfacematerial>
</materialx>
"""
        pbr = parse_standard_surface_scalars(mtlx)
        assert pbr.clearcoat is None

    def test_coat_graph_constant_promoted(self) -> None:
        # 1-hop nodegraph → <constant> resolution applies to ``coat``
        # the same way it does for every other _FLOAT_INPUT.
        mtlx = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="NG_COAT">
    <constant name="coat_const" type="float">
      <input name="value" type="float" value="0.75"/>
    </constant>
    <output name="coat_out" type="float" nodename="coat_const"/>
  </nodegraph>
  <standard_surface name="SR_T" type="surfaceshader">
    <input name="coat" type="float" output="coat_out" nodegraph="NG_COAT"/>
  </standard_surface>
  <surfacematerial name="T" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="SR_T"/>
  </surfacematerial>
</materialx>
"""
        pbr = parse_standard_surface_scalars(mtlx)
        assert pbr.clearcoat == pytest.approx(0.75)


class TestSubsurface:
    """``subsurface`` factor + ``subsurface_color`` + ``subsurface_radius``
    extraction (#409). Survey of 454 gpuopen materials: 13 author
    ``subsurface > 0`` (wax-like, resin, semi-translucent); the rest
    default to 0.0 with sometimes non-default color/radius scaffolding
    that has no rendering effect when the factor is zero. The baker
    still extracts color/radius unconditionally so the adapter can
    decide whether to ship the extension."""

    def test_subsurface_authored_non_default(self) -> None:
        # gpuopen 'Wax (White)' and 'Skin' families author subsurface=1.0.
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="subsurface" type="float" value="1.0"/>')
        )
        assert pbr.subsurface == pytest.approx(1.0)

    def test_subsurface_authored_partial(self) -> None:
        # Real corpus: values like 0.1, 0.15, 0.178, 0.2, 0.25, 0.3, 0.5.
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="subsurface" type="float" value="0.25"/>')
        )
        assert pbr.subsurface == pytest.approx(0.25)

    def test_subsurface_default_zero_extracted(self) -> None:
        # 441/454 corpus materials default to 0.0 — extracting it
        # confirms provenance. Adapter suppresses the no-op extension.
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="subsurface" type="float" value="0.0"/>')
        )
        assert pbr.subsurface == pytest.approx(0.0)

    def test_subsurface_unset_stays_none(self) -> None:
        pbr = parse_standard_surface_scalars(_wrap())
        assert pbr.subsurface is None

    def test_subsurface_color_authored_tinted(self) -> None:
        # Linear RGB per MaterialX 1.38 — extracted verbatim.
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="subsurface_color" type="color3" value="0.8, 0.4, 0.3"/>')
        )
        assert pbr.subsurface_color == pytest.approx([0.8, 0.4, 0.3])

    def test_subsurface_color_unset_stays_none(self) -> None:
        pbr = parse_standard_surface_scalars(_wrap())
        assert pbr.subsurface_color is None

    def test_subsurface_radius_per_channel_mfp(self) -> None:
        # MaterialX subsurface_radius is color3 carrying per-wavelength
        # mean-free-path (typically mm). Skin-like values: ~[1, 0.2, 0.1].
        pbr = parse_standard_surface_scalars(
            _wrap('<input name="subsurface_radius" type="color3" value="1.0, 0.2, 0.1"/>')
        )
        assert pbr.subsurface_radius == pytest.approx([1.0, 0.2, 0.1])

    def test_subsurface_radius_unset_stays_none(self) -> None:
        pbr = parse_standard_surface_scalars(_wrap())
        assert pbr.subsurface_radius is None

    def test_subsurface_full_triplet_authored(self) -> None:
        # Real-corpus shape: all three authored together (wax white).
        pbr = parse_standard_surface_scalars(
            _wrap(
                '<input name="subsurface" type="float" value="1.0"/>',
                '<input name="subsurface_color" type="color3" value="0.9, 0.85, 0.7"/>',
                '<input name="subsurface_radius" type="color3" value="11.6, 9.4, 7.4"/>',
            )
        )
        assert pbr.subsurface == pytest.approx(1.0)
        assert pbr.subsurface_color == pytest.approx([0.9, 0.85, 0.7])
        assert pbr.subsurface_radius == pytest.approx([11.6, 9.4, 7.4])

    def test_subsurface_graph_constant_promoted(self) -> None:
        # 1-hop nodegraph → <constant> resolution applies to ``subsurface``
        # the same way it does for every other _FLOAT_INPUT.
        mtlx = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="NG_SSS">
    <constant name="sss_const" type="float">
      <input name="value" type="float" value="0.42"/>
    </constant>
    <output name="sss_out" type="float" nodename="sss_const"/>
  </nodegraph>
  <standard_surface name="SR_T" type="surfaceshader">
    <input name="subsurface" type="float" output="sss_out" nodegraph="NG_SSS"/>
  </standard_surface>
  <surfacematerial name="T" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="SR_T"/>
  </surfacematerial>
</materialx>
"""
        pbr = parse_standard_surface_scalars(mtlx)
        assert pbr.subsurface == pytest.approx(0.42)

    def test_subsurface_texture_bound_stays_none(self) -> None:
        # texture-bound subsurface leaves the field None — consistent
        # with metalness-texture-bound handling.
        mtlx = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="NG_SSS">
    <image name="sss_img" type="float">
      <input name="file" type="filename" value="sss.png"/>
    </image>
    <output name="sss_out" type="float" nodename="sss_img"/>
  </nodegraph>
  <standard_surface name="SR_T" type="surfaceshader">
    <input name="subsurface" type="float" output="sss_out" nodegraph="NG_SSS"/>
  </standard_surface>
  <surfacematerial name="T" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="SR_T"/>
  </surfacematerial>
</materialx>
"""
        pbr = parse_standard_surface_scalars(mtlx)
        assert pbr.subsurface is None


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
            "clearcoat",
            "clearcoat_roughness",
            "specular_intensity",
            "specular_color",
            "thickness",
            "dispersion",
            # Subsurface scattering (#409) — additive triplet.
            "subsurface",
            "subsurface_color",
            "subsurface_radius",
        ):
            assert getattr(pbr, fname) is None, f"{fname} should default to None"

    def test_fields_are_settable(self) -> None:
        from mat_vis_baker.common import PBRBlock

        pbr = PBRBlock()
        pbr.clearcoat = 1.0
        pbr.clearcoat_roughness = 0.2
        pbr.specular_intensity = 0.8
        pbr.specular_color = [0.95, 0.5, 0.3]
        pbr.thickness = 5.0
        pbr.dispersion = 0.1
        pbr.subsurface = 0.5
        pbr.subsurface_color = [0.9, 0.6, 0.5]
        pbr.subsurface_radius = [1.0, 0.2, 0.1]
        assert pbr.clearcoat == 1.0
        assert pbr.clearcoat_roughness == 0.2
        assert pbr.specular_intensity == 0.8
        assert pbr.specular_color == [0.95, 0.5, 0.3]
        assert pbr.thickness == 5.0
        assert pbr.dispersion == 0.1
        assert pbr.subsurface == 0.5
        assert pbr.subsurface_color == [0.9, 0.6, 0.5]
        assert pbr.subsurface_radius == [1.0, 0.2, 0.1]
