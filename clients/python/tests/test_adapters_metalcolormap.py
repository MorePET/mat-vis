"""Tests for the adapter-side glTF-format concerns (mat-vis#290 follow-up).

The metallic+colorMap / metalnessMap neutral-multiplier convention has
moved to the baker (see ``tests/test_gpuopen_baker.py``) so every
consumer — Python, JS, Rust, shell, plus the search-side
``pbr.metalness`` readers — inherits it from the substrate without
per-language reimplementation. The adapter is now dumb: substrate
scalars flow through verbatim.

What STAYS adapter-side and is exercised here:

  - The KHR_materials_ior / KHR_materials_transmission extensions are
    omitted from glTF output when their values match the spec defaults
    (1.5 / 0.0). Emitting a no-op extension entry just bloats output.
    This is a glTF-format concern (extension emission policy), NOT a
    substrate fact.
  - Dumb-adapter contract: with raw scalars + textures the adapter
    does NOT auto-inject neutralized colors anymore — callers feeding
    raw scalars without baker treatment are responsible for the
    multiplier convention themselves.
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


# ── KHR extension default suppression (adapter-side, format concern) ──


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


# ── dumb-adapter contract: no auto-injection ──────────────────


def test_to_threejs_does_not_auto_neutralize_color():
    """Adapter does NOT inject color when only metalness + colorMap are passed.

    The convention now lives in the baker. Callers who hand raw scalars
    to the adapter (bypassing the substrate) get exactly what they passed
    — no surprise color injection.
    """
    result = to_threejs(
        {"metalness": 1.0},
        {"color": _png_bytes()},
    )
    assert "color" not in result


def test_to_gltf_does_not_auto_neutralize_color():
    result = to_gltf(
        {"metalness": 1.0},
        {"color": _png_bytes()},
    )
    assert "baseColorFactor" not in result["pbrMetallicRoughness"]


def test_substrate_neutralized_color_flows_through():
    """When the BAKER has already neutralized color_hex='#FFFFFF', the
    adapter passes it through verbatim — no double-tint, no override.
    """
    result = to_threejs(
        {"metalness": 1.0, "color_hex": "#FFFFFF"},
        {"color": _png_bytes()},
        color_format="int",
    )
    assert result["color"] == 0xFFFFFF

    result_gltf = to_gltf(
        {"metalness": 1.0, "color_hex": "#FFFFFF"},
        {"color": _png_bytes()},
    )
    assert result_gltf["pbrMetallicRoughness"]["baseColorFactor"] == [1.0, 1.0, 1.0, 1.0]


def test_authored_color_preserved():
    """Authored color flows through unchanged (it always did)."""
    result = to_threejs(
        {"metalness": 1.0, "color_hex": "#E3E3E3"},
        {"color": _png_bytes()},
        color_format="int",
    )
    assert result["color"] == 0xE3E3E3
