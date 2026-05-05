"""Tests for mat_vis_client.adapters — focused on glTF packing.

Covers the metallicRoughnessTexture packing path added for #91:
    - Pack metalness + roughness into G/B channels (R = 255 fallback).
    - Pack with AO into R channel when present.
    - Fallback emits ``_note_no_pillow`` when Pillow is unavailable.
"""

from __future__ import annotations

import base64
from io import BytesIO

import pytest

from mat_vis_client import adapters
from mat_vis_client.adapters import to_gltf

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402  (after importorskip)


def _gray_png(value: int, size: tuple[int, int] = (4, 4)) -> bytes:
    """Build a uniform-grayscale PNG for use as a synthetic texture."""
    img = Image.new("L", size, value)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _decode_data_uri(data_uri: str) -> Image.Image:
    """Decode a ``data:image/png;base64,...`` URI to a Pillow Image."""
    assert data_uri.startswith("data:image/png;base64,")
    payload = data_uri.split(",", 1)[1]
    return Image.open(BytesIO(base64.b64decode(payload)))


class TestMetallicRoughnessPacking:
    """metalness + roughness must be packed per glTF 2.0 (G=rough, B=metal)."""

    def test_packs_when_both_textures_present(self):
        metal_png = _gray_png(0x80)
        rough_png = _gray_png(0xC0)

        result = to_gltf({}, {"metalness": metal_png, "roughness": rough_png})

        pbr = result["pbrMetallicRoughness"]
        assert "_note_metallicRoughnessTexture" not in pbr
        assert "_note_no_pillow" not in pbr
        assert "metallicRoughnessTexture" in pbr

        uri = pbr["metallicRoughnessTexture"]["source"]["uri"]
        packed = _decode_data_uri(uri)

        assert packed.mode == "RGB"
        assert packed.size == (4, 4)

        r, g, b = packed.split()
        # No AO map → R channel is 255 (no occlusion).
        assert set(r.tobytes()) == {0xFF}
        # G channel carries roughness.
        assert set(g.tobytes()) == {0xC0}
        # B channel carries metalness.
        assert set(b.tobytes()) == {0x80}

    def test_packs_ao_into_red_channel(self):
        ao_png = _gray_png(0x40)
        metal_png = _gray_png(0x80)
        rough_png = _gray_png(0xC0)

        result = to_gltf(
            {},
            {"ao": ao_png, "metalness": metal_png, "roughness": rough_png},
        )

        pbr = result["pbrMetallicRoughness"]
        uri = pbr["metallicRoughnessTexture"]["source"]["uri"]
        packed = _decode_data_uri(uri)

        r, g, b = packed.split()
        # AO occupies the R channel.
        assert set(r.tobytes()) == {0x40}
        assert set(g.tobytes()) == {0xC0}
        assert set(b.tobytes()) == {0x80}

        # AO is also still emitted as occlusionTexture (top-level material).
        assert "occlusionTexture" in result

    def test_resizes_roughness_to_metalness_dimensions(self):
        """Mismatched sizes resolve by resizing to the metalness reference."""
        metal_png = _gray_png(0x80, size=(4, 4))
        rough_png = _gray_png(0xC0, size=(8, 8))

        result = to_gltf({}, {"metalness": metal_png, "roughness": rough_png})
        uri = result["pbrMetallicRoughness"]["metallicRoughnessTexture"]["source"]["uri"]
        packed = _decode_data_uri(uri)
        assert packed.size == (4, 4)

    def test_fallback_when_pillow_unavailable(self, monkeypatch):
        """Without Pillow, emit a ``_note_no_pillow`` marker instead."""
        monkeypatch.setattr(adapters, "Image", None)

        result = to_gltf(
            {},
            {"metalness": _gray_png(0x80), "roughness": _gray_png(0xC0)},
        )

        pbr = result["pbrMetallicRoughness"]
        assert "metallicRoughnessTexture" not in pbr
        assert "_note_no_pillow" in pbr
        assert "Pillow" in pbr["_note_no_pillow"]
