"""Tests for KHR_materials_sheen adapter coverage (#407).

Velvet/satin/fabric retroreflective edge backscatter. The MaterialX
``<standard_surface>`` ``sheen`` / ``sheen_color`` / ``sheen_roughness``
inputs map to:

    | scalar              | to_threejs              | to_gltf                                                     |
    |---------------------|-------------------------|-------------------------------------------------------------|
    | sheen               | result["sheen"]         | KHR_materials_sheen.sheenColorFactor (magnitude × color)    |
    | sheen_color         | result["sheenColor"]    | KHR_materials_sheen.sheenColorFactor (color × magnitude)    |
    | sheen_roughness     | result["sheenRoughness"]| KHR_materials_sheen.sheenRoughnessFactor                    |

Sheen at the spec default 0.0 is omitted (no-op extension entry and
non-fabric MeshPhysicalMaterial pollution, mirroring the existing
clearcoat=0.0 suppression pattern). Audit at v2026.04.99: polyhaven
authors sheen=0.0 on 757/757 entries, ambientcg never authors the
inputs, gpuopen 0/454 — gating on >0 keeps the output clean.
"""

from __future__ import annotations

from mat_vis_client.adapters import to_gltf, to_threejs


# ── Three.js MeshPhysicalMaterial ───────────────────────────────


class TestSheenToThreejs:
    def test_sheen_emitted_with_full_triple(self) -> None:
        result = to_threejs(
            {
                "sheen": 0.8,
                "sheen_color": [0.3, 0.5, 1.0],
                "sheen_roughness": 0.5,
            }
        )
        assert result["sheen"] == 0.8
        assert result["sheenColor"] == [0.3, 0.5, 1.0]
        assert result["sheenRoughness"] == 0.5

    def test_sheen_zero_omitted(self) -> None:
        # Polyhaven authors sheen=0.0 on all 757 entries; the adapter
        # MUST suppress to avoid polluting every non-fabric material's
        # parameter dict with sheenColor/sheenRoughness defaults.
        result = to_threejs(
            {
                "sheen": 0.0,
                "sheen_color": [1.0, 1.0, 1.0],
                "sheen_roughness": 0.3,
            }
        )
        assert "sheen" not in result
        assert "sheenColor" not in result
        assert "sheenRoughness" not in result

    def test_sheen_none_omitted(self) -> None:
        result = to_threejs({"sheen": None})
        assert "sheen" not in result
        assert "sheenColor" not in result
        assert "sheenRoughness" not in result

    def test_sheen_absent_when_not_in_scalars(self) -> None:
        result = to_threejs({})
        assert "sheen" not in result
        assert "sheenColor" not in result
        assert "sheenRoughness" not in result

    def test_sheen_defaults_filled_when_factor_authored(self) -> None:
        # sheen > 0 but sheen_color / sheen_roughness unset → fill
        # MaterialX neutral defaults so Three.js doesn't render with
        # uninitialized halo.
        result = to_threejs({"sheen": 1.0})
        assert result["sheen"] == 1.0
        assert result["sheenColor"] == [1.0, 1.0, 1.0]
        assert result["sheenRoughness"] == 1.0


# ── glTF 2.0 with KHR_materials_sheen ───────────────────────────


class TestSheenToGltf:
    def test_sheen_emits_khr_extension(self) -> None:
        result = to_gltf(
            {
                "sheen": 1.0,
                "sheen_color": [0.2, 0.4, 0.9],
                "sheen_roughness": 0.5,
            }
        )
        ext = result["extensions"]["KHR_materials_sheen"]
        # sheenColorFactor = sheen * sheen_color (per spec — the factor
        # IS the tinted magnitude in linear RGB).
        assert ext["sheenColorFactor"] == [0.2, 0.4, 0.9]
        assert ext["sheenRoughnessFactor"] == 0.5

    def test_sheen_color_factor_premultiplied(self) -> None:
        # sheen=0.5, sheen_color=(1,1,1) → sheenColorFactor=(0.5, 0.5, 0.5).
        result = to_gltf({"sheen": 0.5, "sheen_color": [1.0, 1.0, 1.0]})
        ext = result["extensions"]["KHR_materials_sheen"]
        assert ext["sheenColorFactor"] == [0.5, 0.5, 0.5]

    def test_sheen_zero_omitted(self) -> None:
        # Spec default sheenColorFactor=(0,0,0). 757/757 polyhaven
        # entries author sheen=0; emitting the extension would bloat
        # the substrate massively for no rendering effect.
        result = to_gltf(
            {
                "sheen": 0.0,
                "sheen_color": [1.0, 1.0, 1.0],
                "sheen_roughness": 0.3,
            }
        )
        assert "KHR_materials_sheen" not in result.get("extensions", {})

    def test_sheen_none_omitted(self) -> None:
        result = to_gltf({"sheen": None})
        assert "KHR_materials_sheen" not in result.get("extensions", {})

    def test_sheen_absent_when_not_in_scalars(self) -> None:
        result = to_gltf({})
        assert "KHR_materials_sheen" not in result.get("extensions", {})

    def test_sheen_without_color_falls_back_to_white(self) -> None:
        # sheen authored but sheen_color absent → adapter substitutes
        # MaterialX neutral (1,1,1) so the extension is renderable.
        result = to_gltf({"sheen": 0.7})
        ext = result["extensions"]["KHR_materials_sheen"]
        assert ext["sheenColorFactor"] == [0.7, 0.7, 0.7]

    def test_sheen_roughness_omitted_when_unset(self) -> None:
        # sheenRoughnessFactor has a spec default (0.0). When the input
        # is unset we don't synthesize a value — the field stays absent
        # and renderers apply their own default. Mirrors the clearcoat
        # roughness handling.
        result = to_gltf({"sheen": 0.5, "sheen_color": [1.0, 1.0, 1.0]})
        ext = result["extensions"]["KHR_materials_sheen"]
        assert "sheenRoughnessFactor" not in ext
