"""Adapter coverage for subsurface scattering (#409).

| input              | Three.js                | glTF                                          |
|--------------------|-------------------------|-----------------------------------------------|
| subsurface         | (no-op — documented)    | KHR_materials_subsurface.subsurfaceFactor     |
| subsurface_color   | (no-op — documented)    | KHR_materials_subsurface.subsurfaceColorFactor|
| subsurface_radius  | (no-op — documented)    | KHR_materials_subsurface.subsurfaceRadiusFactor|

Three.js ``MeshPhysicalMaterial`` has no native SSS field; the adapter is
intentionally a no-op and these tests pin that contract. The glTF
adapter emits a (draft / unratified) ``KHR_materials_subsurface``
extension carrying the MaterialX-faithful triplet so the substrate
round-trips correctly through glTF consumers that recognize the
extension. See #409.
"""

from __future__ import annotations

from mat_vis_client.adapters import to_gltf, to_threejs


# ── Three.js: documented no-op ────────────────────────────────────


class TestThreejsSubsurfaceNoOp:
    """``MeshPhysicalMaterial`` has no SSS field. Verify the SSS scalars
    do NOT leak into the Three.js output under any spelling — neither
    as a top-level property nor as a renamed alias. This pin protects
    against a well-meaning future change quietly emitting a property
    that ``new THREE.MeshPhysicalMaterial(...)`` will silently swallow."""

    def test_subsurface_does_not_leak(self) -> None:
        out = to_threejs(
            {
                "subsurface": 1.0,
                "subsurface_color": [0.9, 0.5, 0.4],
                "subsurface_radius": [1.0, 0.2, 0.1],
            }
        )
        # No subsurface key in any casing; no _color / _radius siblings.
        for key in out:
            assert "subsurface" not in key.lower(), f"unexpected SSS key {key!r}"

    def test_subsurface_factor_alone_does_not_leak(self) -> None:
        out = to_threejs({"subsurface": 0.5})
        for key in out:
            assert "subsurface" not in key.lower()

    def test_subsurface_does_not_disturb_other_fields(self) -> None:
        # SSS-bearing inputs must not silently disable other passthrough
        # fields. Pin a mix.
        out = to_threejs(
            {
                "roughness": 0.4,
                "metalness": 0.0,
                "subsurface": 0.5,
                "subsurface_color": [0.9, 0.5, 0.4],
                "subsurface_radius": [1.0, 0.2, 0.1],
            }
        )
        assert out["roughness"] == 0.4
        assert out["metalness"] == 0.0


# ── glTF: KHR_materials_subsurface emission ──────────────────────


class TestGltfSubsurface:
    def test_subsurface_factor_emits_extension(self) -> None:
        out = to_gltf({"subsurface": 0.25})
        ext = out["extensions"]["KHR_materials_subsurface"]
        assert ext == {"subsurfaceFactor": 0.25}

    def test_full_triplet_emits_all_three_factors(self) -> None:
        out = to_gltf(
            {
                "subsurface": 1.0,
                "subsurface_color": [0.9, 0.85, 0.7],
                "subsurface_radius": [11.6, 9.4, 7.4],
            }
        )
        ext = out["extensions"]["KHR_materials_subsurface"]
        assert ext == {
            "subsurfaceFactor": 1.0,
            "subsurfaceColorFactor": [0.9, 0.85, 0.7],
            # Per-channel mean-free-path carried verbatim — MaterialX
            # and the draft glTF extension share length-per-channel
            # semantics, no unit conversion at the boundary.
            "subsurfaceRadiusFactor": [11.6, 9.4, 7.4],
        }

    def test_subsurface_zero_omits_extension(self) -> None:
        # Mirrors clearcoat / transmission / dispersion suppression:
        # no-op extension entries bloat glTF output for the 441/454
        # corpus materials authoring the default.
        out = to_gltf({"subsurface": 0.0, "subsurface_color": [0.5, 0.5, 0.5]})
        assert "extensions" not in out or "KHR_materials_subsurface" not in out.get(
            "extensions", {}
        )

    def test_subsurface_none_omits_extension(self) -> None:
        out = to_gltf({"subsurface": None})
        assert "extensions" not in out or "KHR_materials_subsurface" not in out.get(
            "extensions", {}
        )

    def test_subsurface_color_alone_does_not_emit_extension(self) -> None:
        # Without subsurface > 0, the SSS layer is disabled — the color
        # / radius authoring scaffolding has no rendering effect. Don't
        # ship an extension that turns into a no-op for consumers.
        out = to_gltf({"subsurface_color": [0.5, 0.5, 0.5]})
        assert "extensions" not in out or "KHR_materials_subsurface" not in out.get(
            "extensions", {}
        )

    def test_partial_triplet_emits_only_authored_factors(self) -> None:
        # Factor authored without color/radius: emit just the factor.
        out = to_gltf({"subsurface": 0.4})
        ext = out["extensions"]["KHR_materials_subsurface"]
        assert "subsurfaceFactor" in ext
        assert "subsurfaceColorFactor" not in ext
        assert "subsurfaceRadiusFactor" not in ext

    def test_subsurface_factor_with_color_only(self) -> None:
        out = to_gltf({"subsurface": 0.5, "subsurface_color": [0.8, 0.5, 0.4]})
        ext = out["extensions"]["KHR_materials_subsurface"]
        assert ext["subsurfaceFactor"] == 0.5
        assert ext["subsurfaceColorFactor"] == [0.8, 0.5, 0.4]
        assert "subsurfaceRadiusFactor" not in ext
