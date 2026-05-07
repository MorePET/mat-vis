"""Tests for adapter input-key aliases (ADR-0013 §Decision-3).

py-mat and other downstream wrappers store PBR scalars under glTF-spec
names (``metallic``); mat-vis adapters historically accept only the
Three.js naming (``metalness``). Every wrapper performs a manual
rename at the boundary. ADR-0013 promotes the glTF name to a
first-class accepted alias on the input side, with the canonical
output name unchanged per adapter (Three.js: ``metalness``; glTF:
``metallicFactor``; MTLX: ``metallic``).

Conflict rule: setting both ``metallic`` and ``metalness`` with
non-equal non-None values raises ``ValueError``. Equal values pass
through silently. None on either side defers to the other.

#303.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mat_vis_client.adapters import export_mtlx, to_gltf, to_threejs


class TestMetallicAlias:
    """``metallic`` (glTF spec) accepted as alias for ``metalness`` (Three.js)."""

    def test_to_threejs_metallic_resolves_to_metalness(self):
        result = to_threejs({"metallic": 0.8})
        assert result["metalness"] == 0.8

    def test_to_gltf_metallic_resolves_to_metallicFactor(self):
        result = to_gltf({"metallic": 0.8})
        assert result["pbrMetallicRoughness"]["metallicFactor"] == 0.8

    def test_to_threejs_metallic_zero_distinguished_from_missing(self):
        result = to_threejs({"metallic": 0.0})
        assert result["metalness"] == 0.0

    def test_to_threejs_metalness_still_works(self):
        result = to_threejs({"metalness": 0.7})
        assert result["metalness"] == 0.7

    def test_to_threejs_both_keys_equal_values_pass(self):
        result = to_threejs({"metalness": 0.5, "metallic": 0.5})
        assert result["metalness"] == 0.5

    def test_to_gltf_both_keys_equal_values_pass(self):
        result = to_gltf({"metalness": 0.5, "metallic": 0.5})
        assert result["pbrMetallicRoughness"]["metallicFactor"] == 0.5

    def test_to_threejs_both_keys_conflict_raises(self):
        with pytest.raises(ValueError, match="metalness.*metallic"):
            to_threejs({"metalness": 0.7, "metallic": 0.9})

    def test_to_gltf_both_keys_conflict_raises(self):
        with pytest.raises(ValueError, match="metalness.*metallic"):
            to_gltf({"metalness": 0.7, "metallic": 0.9})

    def test_to_threejs_metalness_none_defers_to_metallic(self):
        result = to_threejs({"metalness": None, "metallic": 0.6})
        assert result["metalness"] == 0.6

    def test_to_threejs_metallic_none_defers_to_metalness(self):
        result = to_threejs({"metalness": 0.4, "metallic": None})
        assert result["metalness"] == 0.4

    def test_export_mtlx_metallic_resolves_to_metallic_input(self, tmp_path: Path):
        # MTLX UsdPreviewSurface uses the glTF-spec name on its input —
        # <input name="metallic" type="float" value="..."/>. So input
        # alias and output name happen to coincide for this adapter; the
        # test pins that ``metallic`` reaches the shader regardless of
        # which input key the caller used.
        mtlx_path = export_mtlx({"metallic": 0.8}, output_dir=tmp_path)
        xml = mtlx_path.read_text()
        assert 'name="metallic"' in xml
        assert 'value="0.8"' in xml

    def test_export_mtlx_both_keys_conflict_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="metalness.*metallic"):
            export_mtlx({"metalness": 0.7, "metallic": 0.9}, output_dir=tmp_path)
