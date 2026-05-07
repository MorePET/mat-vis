"""Adapter passthrough tests for #340 PBR coverage extension.

5 new scalars routed to Three.js MeshPhysicalMaterial native props and
glTF KHR_materials_* extensions:

| input              | Three.js                | glTF                                        |
|--------------------|-------------------------|---------------------------------------------|
| clearcoat_roughness| clearcoatRoughness      | KHR_materials_clearcoat.clearcoatRoughnessF |
| specular_intensity | specularIntensity       | KHR_materials_specular.specularFactor       |
| specular_color_*   | specularColor (sRGB hex)| KHR_materials_specular.specularColorFactor  |
| thickness          | thickness               | KHR_materials_volume.thicknessFactor        |
| dispersion         | dispersion              | KHR_materials_dispersion.dispersion         |
"""

from __future__ import annotations


import pytest

from mat_vis_client.adapters import _resolve_specular_color, to_gltf, to_threejs


# ── Three.js passthrough ──────────────────────────────────────────


class TestThreejsPassthrough:
    def test_clearcoat_roughness_passes_through(self) -> None:
        out = to_threejs({"clearcoat_roughness": 0.35})
        assert out["clearcoatRoughness"] == 0.35

    def test_specular_intensity_passes_through(self) -> None:
        out = to_threejs({"specular_intensity": 0.6})
        assert out["specularIntensity"] == 0.6

    def test_thickness_passes_through(self) -> None:
        out = to_threejs({"thickness": 5.0})
        assert out["thickness"] == 5.0

    def test_dispersion_passes_through(self) -> None:
        out = to_threejs({"dispersion": 0.25})
        assert out["dispersion"] == 0.25

    def test_specular_color_linear_emits_srgb_hex(self) -> None:
        # Three.js consumes specularColor as sRGB; linear input gets
        # re-encoded at the boundary.
        out = to_threejs({"specular_color_linear": [0.5, 0.5, 0.5]})
        # linear 0.5 → sRGB ~0.7354 → 0xbc on 8-bit
        assert out["specularColor"].startswith("#")
        assert out["specularColor"].lower() == "#bcbcbc"

    def test_specular_color_white_passes(self) -> None:
        out = to_threejs({"specular_color_linear": [1.0, 1.0, 1.0]})
        assert out["specularColor"].lower() == "#ffffff"

    def test_unset_fields_are_omitted(self) -> None:
        out = to_threejs({})
        for key in (
            "clearcoatRoughness",
            "specularIntensity",
            "specularColor",
            "thickness",
            "dispersion",
        ):
            assert key not in out


# ── glTF KHR extension routing ────────────────────────────────────


class TestGltfClearcoatRoughness:
    def test_clearcoat_roughness_paired_with_clearcoat(self) -> None:
        out = to_gltf({"clearcoat": 0.5, "clearcoat_roughness": 0.2})
        ext = out["extensions"]["KHR_materials_clearcoat"]
        assert ext == {"clearcoatFactor": 0.5, "clearcoatRoughnessFactor": 0.2}

    def test_clearcoat_roughness_alone_does_not_enable_clearcoat(self) -> None:
        # Without clearcoat > 0, the clearcoat layer is disabled — KHR
        # extension stays absent (avoids no-op extension entry).
        out = to_gltf({"clearcoat_roughness": 0.2})
        assert "extensions" not in out or "KHR_materials_clearcoat" not in out.get("extensions", {})

    def test_clearcoat_roughness_at_default_omitted(self) -> None:
        # Default clearcoatRoughnessFactor is 0.0 — emitting it is no-op.
        out = to_gltf({"clearcoat": 0.5, "clearcoat_roughness": 0.0})
        ext = out["extensions"]["KHR_materials_clearcoat"]
        assert ext == {"clearcoatFactor": 0.5}
        assert "clearcoatRoughnessFactor" not in ext


class TestGltfSpecular:
    def test_specular_intensity_emits_when_not_default(self) -> None:
        out = to_gltf({"specular_intensity": 0.6})
        ext = out["extensions"]["KHR_materials_specular"]
        assert ext == {"specularFactor": 0.6}

    def test_specular_intensity_at_default_omits_extension(self) -> None:
        out = to_gltf({"specular_intensity": 1.0})
        assert "extensions" not in out or "KHR_materials_specular" not in out.get("extensions", {})

    def test_specular_color_emits_linear_in_extension(self) -> None:
        # KHR_materials_specular.specularColorFactor is linear per spec —
        # our resolver returns linear, passes straight through.
        out = to_gltf({"specular_color_linear": [0.95, 0.64, 0.54]})
        ext = out["extensions"]["KHR_materials_specular"]
        assert ext["specularColorFactor"] == pytest.approx([0.95, 0.64, 0.54])

    def test_specular_color_white_at_default_omits_field(self) -> None:
        # White [1,1,1] is the spec default — emit nothing.
        out = to_gltf({"specular_color_linear": [1.0, 1.0, 1.0]})
        assert "extensions" not in out or "KHR_materials_specular" not in out.get("extensions", {})

    def test_specular_color_rgba_alias_de_gammas(self) -> None:
        # sRGB input mid-grey [0.5, 0.5, 0.5] should land in glTF as
        # linear [~0.214, ~0.214, ~0.214].
        out = to_gltf({"specular_color_rgba": [0.5, 0.5, 0.5]})
        ext = out["extensions"]["KHR_materials_specular"]
        assert ext["specularColorFactor"][0] == pytest.approx(0.21404, abs=1e-3)

    def test_specular_intensity_and_color_together(self) -> None:
        out = to_gltf(
            {
                "specular_intensity": 0.6,
                "specular_color_linear": [0.95, 0.64, 0.54],
            }
        )
        ext = out["extensions"]["KHR_materials_specular"]
        assert ext["specularFactor"] == 0.6
        assert ext["specularColorFactor"] == pytest.approx([0.95, 0.64, 0.54])


class TestGltfVolumeThickness:
    def test_thickness_emits_only_with_transmission(self) -> None:
        out = to_gltf({"transmission": 0.8, "thickness": 5.0})
        ext = out["extensions"]["KHR_materials_volume"]
        assert ext == {"thicknessFactor": 5.0}

    def test_thickness_without_transmission_skipped(self) -> None:
        # Defensive — the baker should already emit None for
        # transmission=0, but the adapter checks too.
        out = to_gltf({"thickness": 5.0})
        assert "extensions" not in out or "KHR_materials_volume" not in out.get("extensions", {})

    def test_thickness_zero_skipped(self) -> None:
        out = to_gltf({"transmission": 0.8, "thickness": 0.0})
        assert "extensions" not in out or "KHR_materials_volume" not in out.get("extensions", {})


class TestGltfDispersion:
    def test_dispersion_emits_when_nonzero(self) -> None:
        out = to_gltf({"dispersion": 0.25})
        ext = out["extensions"]["KHR_materials_dispersion"]
        assert ext == {"dispersion": 0.25}

    def test_dispersion_zero_at_default(self) -> None:
        out = to_gltf({"dispersion": 0.0})
        assert "extensions" not in out or "KHR_materials_dispersion" not in out.get(
            "extensions", {}
        )

    def test_dispersion_unset(self) -> None:
        out = to_gltf({})
        assert "extensions" not in out or "KHR_materials_dispersion" not in out.get(
            "extensions", {}
        )


# ── _resolve_specular_color helper ──────────────────────────────


class TestResolveSpecularColor:
    def test_linear_canonical_passes_through(self) -> None:
        result = _resolve_specular_color({"specular_color_linear": [0.95, 0.64, 0.54]})
        assert result == pytest.approx((0.95, 0.64, 0.54))

    def test_rgba_alias_de_gammas(self) -> None:
        # sRGB 0.5 → linear ~0.214
        result = _resolve_specular_color({"specular_color_rgba": [0.5, 0.5, 0.5]})
        assert result is not None
        assert result[0] == pytest.approx(0.21404, abs=1e-3)

    def test_unset_returns_none(self) -> None:
        assert _resolve_specular_color({}) is None

    def test_conflicting_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="specular-color"):
            _resolve_specular_color(
                {
                    "specular_color_linear": [0.5, 0.5, 0.5],
                    "specular_color_rgba": [0.9, 0.9, 0.9],
                }
            )

    def test_equal_via_aliases_pass(self) -> None:
        # If both forms specify the same linear value (after sRGB→linear),
        # no error.
        # sRGB 1.0 = linear 1.0 (fixed point); both aliases should agree.
        result = _resolve_specular_color(
            {
                "specular_color_linear": [1.0, 1.0, 1.0],
                "specular_color_rgba": [1.0, 1.0, 1.0],
            }
        )
        assert result == pytest.approx((1.0, 1.0, 1.0))


# ── opacityMap (texture) ────────────────────────────────────────


def _tiny_png() -> bytes:
    """Build a valid 1x1 PNG byte string at import time.

    Hand-rolled byte literals are easy to corrupt (silent CRC mismatch
    that Pillow rejects with "broken PNG file"). Generating the bytes
    via Pillow when available, falling back to a verified minimal PNG
    otherwise, keeps the fixture sound under both code paths.
    """
    try:
        from PIL import Image as _Image

        from io import BytesIO as _BytesIO

        buf = _BytesIO()
        _Image.new("RGB", (1, 1), (128, 128, 128)).save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        # Verified minimal 1×1 grayscale PNG (8-bit). The transparent=true
        # / alphaMap path doesn't decode the bytes so this only matters
        # for tests that exercise Pillow packing — which skip when
        # Pillow isn't installed anyway.
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x00\x00\x00\x00:~\x9bU\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00"
            b"\x02\x00\x01\xe5\x27\xde\xfc\x00\x00\x00\x00IEND\xaeB`\x82"
        )


_TINY_PNG = _tiny_png()


class TestOpacityMapThreejs:
    def test_opacity_emits_alphaMap_and_transparent(self) -> None:
        out = to_threejs({}, {"opacity": _TINY_PNG})
        assert "alphaMap" in out
        assert out["alphaMap"].startswith("data:image/png;base64,")
        assert out["transparent"] is True

    def test_no_opacity_no_transparent_flag(self) -> None:
        out = to_threejs({})
        assert "transparent" not in out

    def test_opacity_alongside_color(self) -> None:
        out = to_threejs({}, {"color": _TINY_PNG, "opacity": _TINY_PNG})
        assert "map" in out
        assert "alphaMap" in out
        assert out["transparent"] is True


class TestOpacityMapGltf:
    def test_opacity_emits_alphaMode_mask(self) -> None:
        out = to_gltf({}, {"opacity": _TINY_PNG})
        assert out["alphaMode"] == "MASK"
        assert out["alphaCutoff"] == 0.5

    def test_no_opacity_no_alphaMode(self) -> None:
        out = to_gltf({})
        assert "alphaMode" not in out

    def test_opacity_with_color_packs_alpha(self) -> None:
        # When both are present and Pillow is available, alpha is packed
        # into baseColorTexture.
        try:
            from PIL import Image as _Image  # noqa: F401
        except ImportError:
            pytest.skip("Pillow not installed")
        out = to_gltf({}, {"color": _TINY_PNG, "opacity": _TINY_PNG})
        assert "baseColorTexture" in out["pbrMetallicRoughness"]
        # No "_note_opacity_unpacked" warning when packed successfully.
        assert "_note_opacity_unpacked" not in out


# ── Round-trip / cross-format consistency ────────────────────────


class TestCrossFormatConsistency:
    def test_specular_color_threejs_srgb_gltf_linear(self) -> None:
        """Same linear input → Three.js sRGB hex + glTF linear factor.
        Round-trip-stable through the boundary conversions."""
        scalars = {"specular_color_linear": [0.5, 0.5, 0.5]}
        threejs = to_threejs(scalars)
        gltf = to_gltf(scalars)

        # glTF: linear 0.5 stays linear
        assert gltf["extensions"]["KHR_materials_specular"]["specularColorFactor"] == pytest.approx(
            [0.5, 0.5, 0.5]
        )
        # Three.js: linear 0.5 → sRGB ~0.7354 ≈ 0xbc / 255 = 0.7333…
        assert threejs["specularColor"].lower() == "#bcbcbc"
        # Verify the conversion is the standard piecewise sRGB transfer.
        # round(_linear_to_srgb(0.5) * 255) ≈ 188 (0xbc)
        expected = round((1.055 * 0.5 ** (1 / 2.4) - 0.055) * 255)
        assert int(threejs["specularColor"][1:3], 16) == expected
