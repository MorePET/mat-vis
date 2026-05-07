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


class TestClearcoatMtlxSkipped:
    """UsdPreviewSurface 1.38 has no clearcoat input — adapter drops it silently
    with no error. (Future MaterialX shaders may; revisit then.)"""

    def test_no_clearcoat_artifact_in_mtlx(self, tmp_path: Path):
        out = export_mtlx({"clearcoat": 0.5}, output_dir=tmp_path)
        xml = out.read_text()
        assert "clearcoat" not in xml.lower()
