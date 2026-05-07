"""Tests for export_mtlx material_name sanitization (ADR-0013 §Decision-4).

Material names from real corpora contain spaces ("Stainless Steel 304"),
slashes ("Saint-Gobain/LYSO"), and other path-unsafe characters. The
substrate sanitizes internally so consumers don't repeat the logic per
language client.

Same sanitized name flows into both the file path and the MaterialX
``name=`` attributes (spaces / slashes break MaterialX parsers anyway).

#305.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mat_vis_client.adapters import export_mtlx, generate_mtlx_xml


class TestExportMtlxMaterialNameSanitize:
    def test_spaces_become_underscores(self, tmp_path: Path):
        out = export_mtlx({}, output_dir=tmp_path, material_name="Stainless Steel 304")
        assert out.name == "Stainless_Steel_304.mtlx"
        assert out.exists()

    def test_slashes_become_underscores(self, tmp_path: Path):
        out = export_mtlx({}, output_dir=tmp_path, material_name="Saint-Gobain/LYSO")
        assert out.name == "Saint-Gobain_LYSO.mtlx"

    def test_empty_name_falls_back_to_material(self, tmp_path: Path):
        out = export_mtlx({}, output_dir=tmp_path, material_name="")
        assert out.name == "material.mtlx"

    def test_path_traversal_blocked(self, tmp_path: Path):
        # "../escape" must not write outside output_dir; the leading "../"
        # is sanitized away rather than being honored.
        out = export_mtlx({}, output_dir=tmp_path, material_name="../escape")
        assert out.parent == tmp_path
        assert out.name == "escape.mtlx"

    def test_all_stripped_falls_back(self, tmp_path: Path):
        out = export_mtlx({}, output_dir=tmp_path, material_name="...")
        assert out.name == "material.mtlx"

    def test_default_name_unchanged(self, tmp_path: Path):
        out = export_mtlx({}, output_dir=tmp_path)  # default material_name="Material"
        assert out.name == "Material.mtlx"

    def test_alphanumeric_dash_underscore_preserved(self, tmp_path: Path):
        out = export_mtlx({}, output_dir=tmp_path, material_name="aged-iron_v2")
        assert out.name == "aged-iron_v2.mtlx"

    def test_xml_name_attributes_use_sanitized(self, tmp_path: Path):
        # Spaces in XML name= attributes break MaterialX parsers; the
        # internal node names ("<material_name>_textures",
        # "<material_name>_shader", surfacematerial name=) must reuse
        # the sanitized form.
        out = export_mtlx({}, output_dir=tmp_path, material_name="Stainless Steel 304")
        xml = out.read_text()
        assert 'name="Stainless_Steel_304"' in xml
        assert 'name="Stainless_Steel_304_shader"' in xml
        assert 'name="Stainless_Steel_304_textures"' in xml
        assert "Stainless Steel 304" not in xml  # raw form must not leak


class TestGenerateMtlxXmlSanitize:
    """In-memory variant must sanitize too — same XML correctness concern."""

    def test_xml_name_sanitized(self):
        xml = generate_mtlx_xml({}, material_name="Saint-Gobain/LYSO")
        assert 'name="Saint-Gobain_LYSO"' in xml
        assert "Saint-Gobain/LYSO" not in xml

    def test_empty_falls_back(self):
        xml = generate_mtlx_xml({}, material_name="")
        assert 'name="material"' in xml


class TestSanitizeIsIdempotent:
    """Sanitizing an already-safe name leaves it unchanged."""

    @pytest.mark.parametrize("safe", ["Material", "aged-iron_v2", "X1", "_legacy_"])
    def test_safe_names_pass_through(self, tmp_path: Path, safe: str):
        out = export_mtlx({}, output_dir=tmp_path, material_name=safe)
        # Strip leading underscores per the spec (".strip('_')") but keep
        # internal underscores. "_legacy_" → "legacy".
        expected = safe.strip("_") or "material"
        assert out.name == f"{expected}.mtlx"
