"""Tests for the metallic+colorMap convention and KHR default suppression.

Both behaviors land in ``adapters.py`` per the mat-vis#290 review:

  - When a material is metallic and a colorMap is bound but no scalar
    color was authored, the adapter neutralizes baseColor to white so
    the texture is the sole color contributor (else the renderer's
    default mid-grey double-tints the texture).
  - The KHR_materials_ior / KHR_materials_transmission extensions are
    omitted from glTF output when their values match the spec defaults
    (1.5 / 0.0). Emitting a no-op extension entry just bloats output.

Convention belongs in the adapter layer (NOT the baker) so that the
.mtlx round-trip preserves authored truth.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from mat_vis_client.adapters import to_gltf, to_threejs


# ── synthetic textures ─────────────────────────────────────────


def _png_bytes() -> bytes:
    """Tiny opaque PNG. Pillow optional — fall back to a 1x1 raw header."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        # 1x1 grayscale PNG, hand-encoded.
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x00\x00\x00\x00:~\x9bU"
            b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
    img = Image.new("RGB", (4, 4), (128, 64, 32))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


WHITE_INT = 0xFFFFFF


# ── metallic+colorMap → neutralize ─────────────────────────────


def test_metallic_colormap_neutralizes_color_in_threejs():
    result = to_threejs(
        {"metalness": 1.0},
        {"color": _png_bytes()},
    )
    assert result["color"] == WHITE_INT


def test_metallic_colormap_neutralizes_color_in_gltf():
    result = to_gltf(
        {"metalness": 1.0},
        {"color": _png_bytes()},
    )
    assert result["pbrMetallicRoughness"]["baseColorFactor"] == [1.0, 1.0, 1.0, 1.0]


def test_authored_color_preserved_when_metallic():
    """Don't override an authored color — only fill when None."""
    result = to_threejs(
        {"metalness": 1.0, "color_hex": "#E3E3E3"},
        {"color": _png_bytes()},
    )
    # 0xE3E3E3 = 14935011 — not 0xFFFFFF.
    assert result["color"] == 0xE3E3E3

    result_gltf = to_gltf(
        {"metalness": 1.0, "color_hex": "#E3E3E3"},
        {"color": _png_bytes()},
    )
    bcf = result_gltf["pbrMetallicRoughness"]["baseColorFactor"]
    assert bcf != [1.0, 1.0, 1.0, 1.0]


def test_dielectric_never_neutralized():
    """metalness=0 (or missing) → no convention applied."""
    # No color at all → adapter doesn't synthesize one (existing behavior).
    result = to_threejs({"metalness": 0.0}, {"color": _png_bytes()})
    assert "color" not in result

    result_none = to_threejs({}, {"color": _png_bytes()})
    assert "color" not in result_none


def test_no_colormap_never_neutralized():
    """No colorMap bound → no convention applied (color stays unset)."""
    result = to_threejs({"metalness": 1.0}, {})
    assert "color" not in result

    result_gltf = to_gltf({"metalness": 1.0}, {})
    assert "baseColorFactor" not in result_gltf["pbrMetallicRoughness"]


# ── KHR extension default suppression ──────────────────────────


def test_khr_ior_omitted_when_default():
    result = to_gltf({"ior": 1.5}, {})
    assert "KHR_materials_ior" not in result.get("extensions", {})


def test_khr_ior_emitted_when_authored():
    result = to_gltf({"ior": 1.6}, {})
    assert result["extensions"]["KHR_materials_ior"] == {"ior": 1.6}


@pytest.mark.parametrize("transmission", [0.0, None])
def test_khr_transmission_omitted_when_zero_or_none(transmission):
    result = to_gltf({"transmission": transmission}, {})
    assert "KHR_materials_transmission" not in result.get("extensions", {})


def test_khr_transmission_emitted_when_authored():
    result = to_gltf({"transmission": 1.0}, {})
    assert result["extensions"]["KHR_materials_transmission"] == {"transmissionFactor": 1.0}


# ── Boundary cases: metallic threshold + authored color preservation ──


def test_metallic_threshold_at_exact_0_9_neutralizes():
    """metalness == 0.9 is the threshold — must neutralize."""
    result = to_threejs({"metalness": 0.9}, {"color": _png_bytes()})
    assert result["color"] == WHITE_INT


def test_metallic_threshold_just_below_0_9_does_not_neutralize():
    """metalness == 0.89 — off-by-one: must NOT neutralize."""
    result = to_threejs({"metalness": 0.89}, {"color": _png_bytes()})
    assert "color" not in result


def test_authored_color_rgb_preserved_when_metallic():
    """color_rgb authored (without color_hex) → convention does NOT fire."""
    result_gltf = to_gltf(
        {"metalness": 1.0, "color_rgb": [0.89, 0.89, 0.89]},
        {"color": _png_bytes()},
    )
    # Adapter only writes baseColorFactor when color_hex is set; with
    # only color_rgb authored and the convention skipped, no factor is
    # written — but critically the convention's white override does NOT
    # fire (color_rgb is preserved as authored truth, not overwritten).
    assert "baseColorFactor" not in result_gltf["pbrMetallicRoughness"]
