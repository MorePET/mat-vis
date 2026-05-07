"""Tests for MaterialX <color3> diffuseColor scalar path (#317).

export_mtlx historically dropped color_hex on the scalar path —
``_build_mtlx_tree`` emitted ``roughness`` / ``metallic`` / ``ior``
scalar shader inputs but not ``diffuseColor``. Materials with only a
``color_hex`` scalar (no color texture) exported an mtlx with no
diffuse color at all; renderers fell back to white.

ADR-0013 §Decision-2 makes export_mtlx symmetric with to_threejs /
to_gltf on the scalar path. Texture-bound color still routes through
the existing nodegraph (with the established ``srgb_texture``
colorspace tag); only the scalar fallback is new.

The 0.6.5 cut emits sRGB-byte/255 (matches the existing naive
``_color_hex_to_rgba`` helper). The 0.7.0 cut will switch to linear
via ``_srgb_to_linear`` since UsdPreviewSurface ``diffuseColor`` is
linear by convention.

#317.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from mat_vis_client.adapters import export_mtlx, generate_mtlx_xml


def _png_bytes() -> bytes:
    """Tiny opaque PNG. Pillow optional — fall back to a 1x1 raw header."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x00\x00\x00\x00:~\x9bU"
            b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
    img = Image.new("RGB", (4, 4), (191, 191, 196))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestMtlxScalarColor:
    def test_color_hex_emits_color3_diffuse_input(self, tmp_path: Path):
        out = export_mtlx({"color_hex": "#bfbfc4"}, output_dir=tmp_path)
        xml = out.read_text()
        assert 'name="diffuseColor"' in xml
        assert 'type="color3"' in xml
        # 0xbf/255 = 0.7490196..., 0xc4/255 = 0.7686274...
        # Default ``{:g}`` (6 sig figs) → "0.74902" / "0.768627".
        assert 'value="0.74902,0.74902,0.768627"' in xml

    def test_pure_red_renders_cleanly(self, tmp_path: Path):
        out = export_mtlx({"color_hex": "#FF0000"}, output_dir=tmp_path)
        xml = out.read_text()
        assert 'value="1,0,0"' in xml

    def test_pure_white(self, tmp_path: Path):
        out = export_mtlx({"color_hex": "#FFFFFF"}, output_dir=tmp_path)
        xml = out.read_text()
        assert 'value="1,1,1"' in xml

    def test_color_texture_wins_over_scalar(self, tmp_path: Path):
        # When a "color" texture is bound, the existing nodegraph path
        # provides the diffuse color via <image> + srgb_texture
        # colorspace — the scalar input must NOT be emitted.
        out = export_mtlx(
            {"color_hex": "#bfbfc4"},
            textures={"color": _png_bytes()},
            output_dir=tmp_path,
        )
        xml = out.read_text()
        # Scalar diffuseColor input is `<... value="r,g,b"/>`; the
        # texture-path diffuseColor input is `<... nodegraph="..."
        # output="..."/>` (no value attr). Confirm the scalar form is
        # absent — only the nodegraph form should bind diffuseColor.
        assert "0.74902" not in xml
        assert 'colorspace="srgb_texture"' in xml

    def test_no_color_input_when_neither_scalar_nor_texture(self, tmp_path: Path):
        out = export_mtlx({}, output_dir=tmp_path)
        xml = out.read_text()
        assert "diffuseColor" not in xml

    def test_in_memory_xml_emits_diffuse_input(self):
        # generate_mtlx_xml — no disk IO. Must agree with export_mtlx.
        xml = generate_mtlx_xml({"color_hex": "#FF0000"}, material_name="red")
        assert 'name="diffuseColor"' in xml
        assert 'value="1,0,0"' in xml
