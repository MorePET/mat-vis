"""Adapter tests for iridescence scalar coverage (#408).

The mat-vis substrate carries two iridescence scalars renamed from
the MaterialX ``thin_film_*`` author surface:

    | substrate scalar         | unit | source                  |
    |--------------------------|------|-------------------------|
    | iridescence_thickness    | nm   | thin_film_thickness     |
    | iridescence_ior          |  -   | thin_film_IOR           |

Output bindings:

    | adapter    | thickness                                 | ior                                             |
    |------------|-------------------------------------------|-------------------------------------------------|
    | to_threejs | iridescenceThicknessRange = [0, thickness]| iridescenceIOR (defaults 1.3)                   |
    |            | iridescence = 1.0 (on/off via thickness>0)|                                                 |
    | to_gltf    | KHR_materials_iridescence.                | KHR_materials_iridescence.iridescenceIor        |
    |            |   iridescenceThicknessMaximum = thickness |   (omit at spec default 1.3)                    |
    |            |   iridescenceFactor = 1.0                 |                                                 |

Unit contract: nm end to end — MaterialX, glTF, and Three.js all
agree. No conversion at any boundary. mat-vis#408.

Audit (v2026.04.99 corpus): 0/3160 entries author thin_film_*
non-default across gpuopen + polyhaven + ambientcg. The schema +
adapters ship forward-looking per #408 P1.
"""

from __future__ import annotations

import math

from mat_vis_client.adapters import to_gltf, to_threejs


class TestIridescenceToThreejs:
    def test_thickness_authored_sets_factor_range_and_ior(self):
        # Soap-bubble / pearl regime: ~400nm thin film. Author provides
        # both thickness and IOR.
        result = to_threejs({"iridescence_thickness": 400.0, "iridescence_ior": 1.33})
        assert result["iridescence"] == 1.0
        assert result["iridescenceThicknessRange"] == [0.0, 400.0]
        assert math.isclose(result["iridescenceIOR"], 1.33)

    def test_thickness_only_uses_threejs_default_ior(self):
        # No IOR authored — Three.js MeshPhysicalMaterial default is 1.3,
        # matching the glTF KHR_materials_iridescence spec default.
        result = to_threejs({"iridescence_thickness": 550.0})
        assert result["iridescence"] == 1.0
        assert result["iridescenceThicknessRange"] == [0.0, 550.0]
        assert math.isclose(result["iridescenceIOR"], 1.3)

    def test_thickness_zero_does_not_emit(self):
        # The on/off switch is thickness>0. A zero thickness is the
        # spec default — emitting iridescence=1.0 with a zero range
        # would be a no-op that bloats the material dict.
        result = to_threejs({"iridescence_thickness": 0.0, "iridescence_ior": 1.4})
        assert "iridescence" not in result
        assert "iridescenceThicknessRange" not in result
        assert "iridescenceIOR" not in result

    def test_unset_does_not_emit(self):
        result = to_threejs({})
        assert "iridescence" not in result
        assert "iridescenceThicknessRange" not in result
        assert "iridescenceIOR" not in result

    def test_none_does_not_emit(self):
        result = to_threejs({"iridescence_thickness": None, "iridescence_ior": None})
        assert "iridescence" not in result

    def test_ior_alone_does_not_emit(self):
        # IOR without a thickness is meaningless — Three.js needs the
        # thickness range to compute the interference. No emission.
        result = to_threejs({"iridescence_ior": 1.4})
        assert "iridescence" not in result
        assert "iridescenceIOR" not in result


class TestIridescenceToGltf:
    def test_thickness_authored_emits_extension(self):
        result = to_gltf({"iridescence_thickness": 400.0, "iridescence_ior": 1.33})
        ext = result["extensions"]["KHR_materials_iridescence"]
        assert ext["iridescenceFactor"] == 1.0
        assert math.isclose(ext["iridescenceThicknessMaximum"], 400.0)
        assert math.isclose(ext["iridescenceIor"], 1.33)

    def test_ior_at_spec_default_omitted(self):
        # KHR_materials_iridescence default iridescenceIor is 1.3 —
        # emitting it is a no-op that bloats glTF. Mirrors the
        # ior=1.5 / clearcoat=0.0 / transmission=0.0 suppression.
        result = to_gltf({"iridescence_thickness": 400.0, "iridescence_ior": 1.3})
        ext = result["extensions"]["KHR_materials_iridescence"]
        assert "iridescenceIor" not in ext
        assert ext["iridescenceFactor"] == 1.0
        assert ext["iridescenceThicknessMaximum"] == 400.0

    def test_thickness_only_uses_spec_default_ior(self):
        # No IOR authored — glTF default is 1.3; we omit the field per
        # the no-op-suppression pattern. iridescenceFactor + thickness
        # still ship.
        result = to_gltf({"iridescence_thickness": 550.0})
        ext = result["extensions"]["KHR_materials_iridescence"]
        assert ext["iridescenceFactor"] == 1.0
        assert ext["iridescenceThicknessMaximum"] == 550.0
        assert "iridescenceIor" not in ext

    def test_thickness_zero_omitted(self):
        result = to_gltf({"iridescence_thickness": 0.0, "iridescence_ior": 1.4})
        assert "KHR_materials_iridescence" not in result.get("extensions", {})

    def test_unset_omitted(self):
        result = to_gltf({})
        assert "KHR_materials_iridescence" not in result.get("extensions", {})

    def test_none_omitted(self):
        result = to_gltf({"iridescence_thickness": None})
        assert "KHR_materials_iridescence" not in result.get("extensions", {})

    def test_ior_alone_does_not_emit_extension(self):
        # IOR without a thickness is meaningless — the extension
        # requires the thickness signal to be the on/off trigger.
        result = to_gltf({"iridescence_ior": 1.4})
        assert "KHR_materials_iridescence" not in result.get("extensions", {})


class TestIridescenceUnitContract:
    """Pin the unit contract (#408 pitfall): nm end to end, no
    conversion at any boundary. A 400 nm thickness must round-trip
    verbatim from substrate → Three.js iridescenceThicknessRange max
    AND → glTF iridescenceThicknessMaximum."""

    def test_thickness_value_unchanged_threejs(self):
        result = to_threejs({"iridescence_thickness": 387.5})
        # Three.js iridescenceThicknessRange is [min_nm, max_nm];
        # 387.5 nm flows through verbatim.
        assert result["iridescenceThicknessRange"][1] == 387.5

    def test_thickness_value_unchanged_gltf(self):
        result = to_gltf({"iridescence_thickness": 387.5})
        ext = result["extensions"]["KHR_materials_iridescence"]
        # glTF iridescenceThicknessMaximum is in nm; 387.5 nm verbatim.
        assert ext["iridescenceThicknessMaximum"] == 387.5
