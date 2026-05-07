"""Phase 1.5 walker tests: nested-mix + mix=0/mix=1 fold + symmetric conductor.

Real-corpus fixtures lifted from gpuopen-mtlx.json @ v2026.04.3 and
minimal-reduced to the metalness subgraph. See `tests/fixtures/gpuopen_metalness/`.

#346 / Phase 1.5 / extends #316 procedural-PBR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mat_vis_baker._mtlx_scalars import parse_standard_surface_scalars

FIXTURES = Path(__file__).parent / "fixtures" / "gpuopen_metalness"


def _load(name: str) -> str:
    return (FIXTURES / f"{name}.mtlx").read_text(encoding="utf-8")


# ── Real-corpus shape fixtures ─────────────────────────────────────────────


class TestPhase15CorpusShapes:
    """Each fixture is a minimal-reduced metalness subgraph from a real
    gpuopen material. Shape labels match the strategist's catalog.
    """

    def test_A_perforated_metal_mix0_folds_to_bg(self) -> None:
        # mix(fg=extract, bg=constant 1.0, mix=constant 0.0)
        # mix=0 → output = bg = 1.0 (deterministic regardless of fg).
        pbr = parse_standard_surface_scalars(_load("A_perforated_metal"))
        assert pbr.metalness == pytest.approx(1.0)
        assert pbr.metalness_source == "graph_constant"
        assert pbr.is_conductor is None
        assert pbr.metalness_mean is None

    def test_D_brass_satin_bg_side_conductor(self) -> None:
        # mix(fg=extract, bg=constant 1.0, mix=constant 0.166)
        # bg=1.0 (metal), fg=texture, mix small → mostly metal.
        # Symmetric conductor (NEW in Phase 1.5).
        pbr = parse_standard_surface_scalars(_load("D_brass_satin"))
        assert pbr.metalness is None
        assert pbr.is_conductor is True
        assert pbr.metalness_source == "graph_estimate"
        # bg + (fg - bg) * t with fg=0.5 default for unresolvable texture:
        # 1.0 + (0.5 - 1.0) * 0.166 ≈ 0.917
        assert pbr.metalness_mean == pytest.approx(0.917, abs=0.01)

    def test_E_wallpaper_damask_fg_side_conductor_via_texture_mix(self) -> None:
        # mix(fg=constant 1.0, bg=constant 0.0, mix=texture-extract)
        # Phase 1 already handles this — strict-superset regression.
        pbr = parse_standard_surface_scalars(_load("E_wallpaper_damask"))
        assert pbr.metalness is None
        assert pbr.is_conductor is True
        assert pbr.metalness_source == "graph_estimate"
        # mix unresolvable (texture) → t_eff=0.5 default
        # 0.0 + (1.0 - 0.0) * 0.5 = 0.5
        assert pbr.metalness_mean == pytest.approx(0.5)

    def test_F_gun_metal_mix0_to_image_falls_through(self) -> None:
        # mix(fg=extract, bg=<image>, mix=constant 0.0)
        # mix=0 → output = bg = image (texture-bound, unresolvable).
        # Per acceptance: must fall through cleanly without crash.
        pbr = parse_standard_surface_scalars(_load("F_gun_metal"))
        assert pbr.metalness is None
        assert pbr.is_conductor is None
        assert pbr.metalness_source is None
        assert pbr.metalness_mean is None

    def test_G_bronze_oxydized_nested_mix_with_all_1_0_chain(self) -> None:
        # Outer: mix(fg=node_mix_30, bg=extract, mix=constant 1.0)
        # Inner: mix(fg=extract, bg=constant 1.0, mix=constant 1.0)
        # All resolved-constants in the chain are 1.0 → conductor-leaning.
        pbr = parse_standard_surface_scalars(_load("G_bronze_oxydized"))
        assert pbr.metalness is None
        assert pbr.is_conductor is True
        assert pbr.metalness_source == "graph_estimate"
        # Chain is fully-metal-side → mean defensible at 1.0
        assert pbr.metalness_mean == pytest.approx(1.0, abs=0.01)

    def test_H_chrome_parametric_outer_mix0_folds_to_bg(self) -> None:
        # Outer: mix(fg=node_mix_37, bg=constant 1.0, mix=value=0.0 inline)
        # mix=0 → output = bg = 1.0. Inner mix is dead code.
        pbr = parse_standard_surface_scalars(_load("H_chrome_parametric"))
        assert pbr.metalness == pytest.approx(1.0)
        assert pbr.metalness_source == "graph_constant"
        assert pbr.is_conductor is None
        assert pbr.metalness_mean is None


# ── Adversarial false-positive guards ──────────────────────────────────────


def _wrap(metalness_node_xml: str, *extra_nodes: str) -> str:
    """Wrap a metalness terminal + supporting nodes in a minimal materialx
    document referencing it from <standard_surface>.metalness."""
    extras = "\n    ".join(extra_nodes)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<materialx version="1.38">
  <nodegraph name="NG_T">
    <output name="metalness_output" type="float" nodename="terminal" />
    {metalness_node_xml}
    {extras}
  </nodegraph>
  <standard_surface name="SR_T" type="surfaceshader">
    <input name="metalness" type="float" output="metalness_output" nodegraph="NG_T" />
  </standard_surface>
  <surfacematerial name="T" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="SR_T" />
  </surfacematerial>
</materialx>
"""


class TestPhase15AdversarialFixtures:
    """False-positive guards for the more-permissive Phase 1.5 heuristic."""

    def test_normalization_1_0_in_unrelated_multiply_is_not_conductor(self) -> None:
        # A 1.0 constant in a <multiply> upstream of the mix must not
        # leak into the conductor heuristic. Walker only follows <mix>
        # fg/bg slots, never <multiply>/<add>.
        xml = _wrap(
            '<mix name="terminal" type="float">'
            '  <input name="fg" type="float" nodename="m1"/>'
            '  <input name="bg" type="float" value="0.0"/>'
            '  <input name="mix" type="float" value="0.5"/>'
            "</mix>",
            '<multiply name="m1" type="float">'
            '  <input name="in1" type="float" value="0.3"/>'
            '  <input name="in2" type="float" value="1.0"/>'
            "</multiply>",
        )
        pbr = parse_standard_surface_scalars(xml)
        assert pbr.is_conductor is None
        assert pbr.metalness is None

    def test_inverted_mask_no_meaningful_1_0_stays_none(self) -> None:
        # Nested mix where fg=0.0 and bg=0.0; even if a 1.0 appears
        # somewhere unrelated, must stay None.
        xml = _wrap(
            '<mix name="terminal" type="float">'
            '  <input name="fg" type="float" value="0.0"/>'
            '  <input name="bg" type="float" value="0.0"/>'
            '  <input name="mix" type="float" value="0.5"/>'
            "</mix>",
        )
        pbr = parse_standard_surface_scalars(xml)
        assert pbr.is_conductor is None
        assert pbr.metalness == pytest.approx(0.0)
        assert pbr.metalness_source == "graph_constant"

    def test_unknown_node_type_falls_through_cleanly(self) -> None:
        # <switch> mid-chain — must fall through to None, not raise.
        xml = _wrap(
            '<mix name="terminal" type="float">'
            '  <input name="fg" type="float" nodename="sw"/>'
            '  <input name="bg" type="float" value="0.0"/>'
            '  <input name="mix" type="float" value="0.5"/>'
            "</mix>",
            '<switch name="sw" type="float">'
            '  <input name="in1" type="float" value="1.0"/>'
            '  <input name="which" type="integer" value="0"/>'
            "</switch>",
        )
        pbr = parse_standard_surface_scalars(xml)
        # Should NOT crash. Ideally None (unknown node type).
        assert pbr.metalness is None or pbr.metalness == pytest.approx(0.0)

    def test_circular_reference_does_not_loop(self) -> None:
        # mix A points to mix B which points back to mix A.
        # Walker must terminate (visited-set guard) and return None.
        xml = _wrap(
            '<mix name="terminal" type="float">'
            '  <input name="fg" type="float" nodename="b"/>'
            '  <input name="bg" type="float" value="0.0"/>'
            '  <input name="mix" type="float" value="0.5"/>'
            "</mix>",
            '<mix name="b" type="float">'
            '  <input name="fg" type="float" nodename="terminal"/>'
            '  <input name="bg" type="float" value="0.0"/>'
            '  <input name="mix" type="float" value="0.5"/>'
            "</mix>",
        )
        pbr = parse_standard_surface_scalars(xml)
        # Must terminate; result is None (cycle detected).
        assert pbr.metalness is None
        assert pbr.is_conductor is None

    def test_recursion_depth_cap_aborts_safely(self) -> None:
        # Build a 5-deep nested mix (cap is 3). Must abort and return None.
        chain = []
        for i in range(5):
            nxt = f"m{i + 1}" if i < 4 else "leaf"
            chain.append(
                f'<mix name="m{i}" type="float">'
                f'  <input name="fg" type="float" nodename="{nxt}"/>'
                f'  <input name="bg" type="float" value="0.0"/>'
                f'  <input name="mix" type="float" value="0.5"/>'
                f"</mix>"
            )
        chain.append(
            '<constant name="leaf" type="float"><input name="value" type="float" value="1.0"/></constant>'
        )
        xml = _wrap(
            '<mix name="terminal" type="float">'
            '  <input name="fg" type="float" nodename="m0"/>'
            '  <input name="bg" type="float" value="0.0"/>'
            '  <input name="mix" type="float" value="0.5"/>'
            "</mix>",
            *chain,
        )
        pbr = parse_standard_surface_scalars(xml)
        # Must terminate without crash. Result is None at depth-cap.
        assert pbr.metalness is None
