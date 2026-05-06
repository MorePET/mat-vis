"""Tests for ``mat_vis_baker._mtlx_scalars`` (mat-vis#290).

Synthetic .mtlx fixtures only — the parser is exercised against the real
gpuopen corpus by the per-file metrics suite, not here. Hand-authored
fixtures keep this test fast and intent-clear.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mat_vis_baker._mtlx_scalars import parse_standard_surface_scalars
from mat_vis_baker.common import PBRBlock


# ── fixtures ────────────────────────────────────────────────────


FIXTURE_AUTHORED_METAL = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="brushed_metal_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" value="0.89, 0.89, 0.89"/>
    <input name="metalness" type="float" value="1.0"/>
    <input name="specular_roughness" type="float" value="0.2"/>
    <input name="specular_IOR" type="float" value="1.6"/>
    <input name="transmission" type="float" value="0.0"/>
  </standard_surface>
  <surfacematerial name="brushed_metal" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="brushed_metal_shader"/>
  </surfacematerial>
</materialx>
"""

FIXTURE_TEXTURE_METAL = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng_metal_textures">
    <output name="out_color" type="color3"/>
    <output name="out_rough" type="float"/>
  </nodegraph>
  <standard_surface name="metal_shader" type="surfaceshader">
    <input name="base_color" type="color3" nodegraph="ng_metal_textures" output="out_color"/>
    <input name="metalness" type="float" value="1.0"/>
    <input name="specular_roughness" type="float" nodegraph="ng_metal_textures" output="out_rough"/>
  </standard_surface>
  <surfacematerial name="metal_mat" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="metal_shader"/>
  </surfacematerial>
</materialx>
"""

FIXTURE_DIELECTRIC = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="brick_shader" type="surfaceshader">
    <input name="base_color" type="color3" value="0.7, 0.2, 0.1"/>
    <input name="metalness" type="float" value="0.0"/>
    <input name="specular_roughness" type="float" value="0.5"/>
    <input name="specular_IOR" type="float" value="1.5"/>
  </standard_surface>
  <surfacematerial name="brick_mat" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="brick_shader"/>
  </surfacematerial>
</materialx>
"""

FIXTURE_TRANSMISSIVE = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="glass_shader" type="surfaceshader">
    <input name="base_color" type="color3" value="0.95, 0.95, 1.0"/>
    <input name="metalness" type="float" value="0.0"/>
    <input name="specular_IOR" type="float" value="1.45"/>
    <input name="transmission" type="float" value="1.0"/>
  </standard_surface>
  <surfacematerial name="glass_mat" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="glass_shader"/>
  </surfacematerial>
</materialx>
"""

FIXTURE_MALFORMED = '<materialx version="1.38"><standard_surface name="oops"'

FIXTURE_UNKNOWN_SHADER = """<?xml version="1.0"?>
<materialx version="1.38">
  <UsdPreviewSurface name="usd_shader" type="surfaceshader">
    <input name="diffuseColor" type="color3" value="0.5, 0.5, 0.5"/>
  </UsdPreviewSurface>
</materialx>
"""

FIXTURE_NO_SHADER = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="orphan_ng">
    <output name="out_color" type="color3"/>
  </nodegraph>
</materialx>
"""

FIXTURE_BASE_MULTIPLIES_COLOR = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="dim_shader" type="surfaceshader">
    <input name="base" type="float" value="0.5"/>
    <input name="base_color" type="color3" value="0.8, 0.8, 0.8"/>
  </standard_surface>
</materialx>
"""

FIXTURE_GRAPH_CONSTANT = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="NG_TEST">
    <output name="metalness_output" type="float" nodename="Metalness" />
    <output name="roughness_output" type="float" nodename="Roughness" />
    <constant name="Metalness" type="float">
      <input name="value" type="float" value="1.0" />
    </constant>
    <constant name="Roughness" type="float">
      <input name="value" type="float" value="0.25" />
    </constant>
    <image name="img_color" type="color3">
      <input name="file" type="filename" value="texture.png"/>
    </image>
    <output name="color_output" type="color3" nodename="img_color"/>
  </nodegraph>
  <standard_surface name="surf" type="surfaceshader">
    <input name="metalness" type="float" output="metalness_output" nodegraph="NG_TEST"/>
    <input name="specular_roughness" type="float" output="roughness_output" nodegraph="NG_TEST"/>
    <input name="base_color" type="color3" output="color_output" nodegraph="NG_TEST"/>
  </standard_surface>
</materialx>
"""

# Graph terminal is a procedural node (multiply/mix), not a constant.
# We deliberately do NOT lossy-flatten these — field stays None.
FIXTURE_GRAPH_NON_CONSTANT_TERMINAL = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="NG_PROC">
    <constant name="A" type="float">
      <input name="value" type="float" value="0.5" />
    </constant>
    <constant name="B" type="float">
      <input name="value" type="float" value="0.5" />
    </constant>
    <multiply name="MulNode" type="float">
      <input name="in1" type="float" nodename="A" />
      <input name="in2" type="float" nodename="B" />
    </multiply>
    <mix name="MixNode" type="float">
      <input name="fg" type="float" nodename="A" />
      <input name="bg" type="float" nodename="B" />
      <input name="mix" type="float" value="0.3" />
    </mix>
    <output name="metal_out" type="float" nodename="MulNode" />
    <output name="rough_out" type="float" nodename="MixNode" />
  </nodegraph>
  <standard_surface name="proc_shader" type="surfaceshader">
    <input name="metalness" type="float" output="metal_out" nodegraph="NG_PROC"/>
    <input name="specular_roughness" type="float" output="rough_out" nodegraph="NG_PROC"/>
  </standard_surface>
</materialx>
"""

FIXTURE_LOSSY_COAT = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="coated_shader" type="surfaceshader">
    <input name="base_color" type="color3" value="0.5, 0.5, 0.5"/>
    <input name="coat" type="float" value="0.8"/>
    <input name="coat_roughness" type="float" value="0.1"/>
    <input name="sheen" type="float" value="0.0"/>
  </standard_surface>
</materialx>
"""


# ── tests ───────────────────────────────────────────────────────


def test_authored_metal_populates_all_fields():
    pbr = parse_standard_surface_scalars(FIXTURE_AUTHORED_METAL, material_id="m-metal")
    assert pbr.metalness == 1.0
    assert pbr.ior == 1.6
    assert pbr.roughness == 0.2
    assert pbr.transmission == 0.0
    # base=1.0 → color unchanged.
    assert pbr.color_rgb == [0.89, 0.89, 0.89]


def test_texture_bound_inputs_leave_fields_none():
    pbr = parse_standard_surface_scalars(FIXTURE_TEXTURE_METAL, material_id="m-tex")
    # Only metalness was authored as a scalar value.
    assert pbr.metalness == 1.0
    assert pbr.roughness is None
    assert pbr.color_rgb is None
    assert pbr.ior is None


def test_dielectric_authored_scalars():
    pbr = parse_standard_surface_scalars(FIXTURE_DIELECTRIC, material_id="m-brick")
    assert pbr.metalness == 0.0
    assert pbr.ior == 1.5
    assert pbr.roughness == 0.5
    assert pbr.color_rgb == [0.7, 0.2, 0.1]
    assert pbr.transmission is None


def test_transmissive_authored_scalars():
    pbr = parse_standard_surface_scalars(FIXTURE_TRANSMISSIVE, material_id="m-glass")
    assert pbr.metalness == 0.0
    assert pbr.transmission == 1.0
    assert pbr.ior == 1.45
    assert pbr.color_rgb == [0.95, 0.95, 1.0]


def test_malformed_xml_returns_empty_block_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="mat-vis-baker.gpuopen-scalars"):
        pbr = parse_standard_surface_scalars(FIXTURE_MALFORMED, material_id="m-bad")
    assert pbr == PBRBlock()
    assert any("malformed" in rec.message for rec in caplog.records)


def test_unknown_shader_type_returns_empty_block(caplog):
    with caplog.at_level(logging.WARNING, logger="mat-vis-baker.gpuopen-scalars"):
        pbr = parse_standard_surface_scalars(FIXTURE_UNKNOWN_SHADER, material_id="m-usd")
    assert pbr == PBRBlock()
    assert any("UsdPreviewSurface" in rec.message for rec in caplog.records)


def test_no_shader_at_all_returns_empty_block(caplog):
    with caplog.at_level(logging.WARNING, logger="mat-vis-baker.gpuopen-scalars"):
        pbr = parse_standard_surface_scalars(FIXTURE_NO_SHADER, material_id="m-nochader")
    assert pbr == PBRBlock()
    assert any("no standard_surface" in rec.message for rec in caplog.records)


def test_base_scalar_multiplies_color():
    pbr = parse_standard_surface_scalars(FIXTURE_BASE_MULTIPLIES_COLOR, material_id="m-dim")
    assert pbr.color_rgb is not None
    assert all(abs(c - 0.4) < 1e-9 for c in pbr.color_rgb)


def test_lossy_coat_inputs_logged_and_dropped(caplog):
    # Lossy log was demoted from INFO to DEBUG (mat-vis#290 round-2):
    # ~454 materials × ~3 lossy inputs ≈ 1.5k INFO lines per bake is
    # noise, not signal. Capture at DEBUG so the test still observes.
    with caplog.at_level(logging.DEBUG, logger="mat-vis-baker.gpuopen-scalars"):
        pbr = parse_standard_surface_scalars(FIXTURE_LOSSY_COAT, material_id="m-coat")
    # Coat inputs have no PBRBlock home — verify they are NOT smuggled
    # into any field.
    assert pbr.color_rgb == [0.5, 0.5, 0.5]
    msgs = [rec.message for rec in caplog.records]
    assert any("coat=0.8" in m for m in msgs)
    assert any("coat_roughness=0.1" in m for m in msgs)
    # sheen=0.0 should NOT be logged (zero is not lossy).
    assert not any("sheen=" in m for m in msgs)


# ── Test 6: lossy log extended to subsurface + sheen + zero color3 ─


FIXTURE_LOSSY_SUBSURFACE_SHEEN = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="sss_shader" type="surfaceshader">
    <input name="base_color" type="color3" value="0.5, 0.5, 0.5"/>
    <input name="subsurface" type="float" value="0.5"/>
    <input name="sheen" type="float" value="0.3"/>
  </standard_surface>
</materialx>
"""

# Real gpuopen format — leading space, six decimals — must NOT log
# (this is the B1/B3 false-positive the round-2 fix targets).
FIXTURE_LOSSY_ZERO_COLOR3 = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="z_shader" type="surfaceshader">
    <input name="base_color" type="color3" value="0.5, 0.5, 0.5"/>
    <input name="subsurface_color" type="color3" value=" 0.000000, 0.000000, 0.000000"/>
    <input name="sheen_color" type="color3" value="0.0, 0.0, 0.0"/>
    <input name="coat_color" type="color3" value="0, 0, 0"/>
  </standard_surface>
</materialx>
"""


def test_lossy_subsurface_and_sheen_logged_at_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger="mat-vis-baker.gpuopen-scalars"):
        parse_standard_surface_scalars(FIXTURE_LOSSY_SUBSURFACE_SHEEN, material_id="m-sss")
    msgs = [rec.message for rec in caplog.records]
    assert any("subsurface=0.5" in m for m in msgs)
    assert any("sheen=0.3" in m for m in msgs)


def test_zero_color3_lossy_inputs_do_not_log(caplog):
    """B1/B3 fix pin: real-corpus `" 0.000000, 0.000000, 0.000000"` must
    be detected as zero (not flagged as lossy via string-split heuristic).
    """
    with caplog.at_level(logging.DEBUG, logger="mat-vis-baker.gpuopen-scalars"):
        parse_standard_surface_scalars(FIXTURE_LOSSY_ZERO_COLOR3, material_id="m-zero")
    msgs = [rec.message for rec in caplog.records]
    assert not any("subsurface_color" in m for m in msgs)
    assert not any("sheen_color" in m for m in msgs)
    assert not any("coat_color" in m for m in msgs)


# ── Test 5: adversarial parser inputs ──────────────────────────


FIXTURE_EMPTY_FLOAT = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="s" type="surfaceshader">
    <input name="metalness" type="float" value=""/>
  </standard_surface>
</materialx>
"""

FIXTURE_NAN_FLOAT = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="s" type="surfaceshader">
    <input name="metalness" type="float" value="not a number"/>
  </standard_surface>
</materialx>
"""

FIXTURE_COLOR3_TWO_PARTS = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="s" type="surfaceshader">
    <input name="base_color" type="color3" value="0.5, 0.5"/>
  </standard_surface>
</materialx>
"""

FIXTURE_COLOR3_FOUR_PARTS = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="s" type="surfaceshader">
    <input name="base_color" type="color3" value="0.5, 0.5, 0.5, 0.5"/>
  </standard_surface>
</materialx>
"""

FIXTURE_NO_INPUTS = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="empty_shader" type="surfaceshader">
  </standard_surface>
</materialx>
"""

FIXTURE_TWO_SHADERS = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="first_shader" type="surfaceshader">
    <input name="metalness" type="float" value="1.0"/>
  </standard_surface>
  <standard_surface name="second_shader" type="surfaceshader">
    <input name="metalness" type="float" value="0.0"/>
  </standard_surface>
</materialx>
"""


def test_empty_float_value_yields_none_no_crash():
    pbr = parse_standard_surface_scalars(FIXTURE_EMPTY_FLOAT, material_id="m-empty")
    assert pbr.metalness is None


def test_unparsable_float_value_yields_none_no_crash():
    pbr = parse_standard_surface_scalars(FIXTURE_NAN_FLOAT, material_id="m-nan")
    assert pbr.metalness is None


def test_color3_two_components_yields_none():
    pbr = parse_standard_surface_scalars(FIXTURE_COLOR3_TWO_PARTS, material_id="m-c2")
    assert pbr.color_rgb is None


def test_color3_four_components_yields_none():
    pbr = parse_standard_surface_scalars(FIXTURE_COLOR3_FOUR_PARTS, material_id="m-c4")
    assert pbr.color_rgb is None


def test_shader_with_zero_inputs_returns_all_none():
    pbr = parse_standard_surface_scalars(FIXTURE_NO_INPUTS, material_id="m-noinputs")
    assert pbr == PBRBlock()


def test_two_shaders_picks_first():
    """Pin: when two <standard_surface> shaders exist, parser uses the first."""
    pbr = parse_standard_surface_scalars(FIXTURE_TWO_SHADERS, material_id="m-two")
    assert pbr.metalness == 1.0


# ── 1-hop nodegraph→constant resolution (mat-vis#290 follow-up) ──


def test_graph_constant_resolves_to_authored_scalar():
    """Float inputs bound via `nodegraph=`+`output=` whose terminal is a
    `<constant>` MUST be promoted to the authored scalar value.

    The color3 input here terminates in `<image>`, not `<constant>`, so
    `color_rgb` stays None — the graph walker is intentionally narrow.
    """
    pbr = parse_standard_surface_scalars(FIXTURE_GRAPH_CONSTANT, material_id="m-graph-const")
    assert pbr.metalness == 1.0
    assert pbr.roughness == 0.25
    # Color terminates in <image>, not a constant — must remain texture-bound.
    assert pbr.color_rgb is None


def test_graph_walker_ignores_non_constant_terminals():
    """Procedural graph terminals (`<multiply>`, `<mix>`, ...) MUST NOT be
    lossy-flattened to a single scalar — fields stay None so adapters
    fall back to texture-bound treatment.
    """
    pbr = parse_standard_surface_scalars(
        FIXTURE_GRAPH_NON_CONSTANT_TERMINAL, material_id="m-graph-proc"
    )
    assert pbr.metalness is None
    assert pbr.roughness is None


# ── Test 4: real-corpus golden (defense-in-depth, skipped in CI) ──

# These goldens act as extra coverage when the exploratory probe cache
# exists locally. CI runs without the cache and these tests skip
# gracefully. The cache is NOT a canonical fixture — it's regenerated
# by the gpuopen probe scripts.

_CORPUS = Path("/tmp/gpuopen_probe_cache")


def _load_or_skip(material_id: str) -> str:
    p = _CORPUS / f"{material_id}.mtlx"
    if not p.exists():
        pytest.skip(f"probe cache miss: {p} (exploratory cache, not part of fixtures)")
    return p.read_text(encoding="utf-8")


def test_real_corpus_brushed_metal_scalars():
    """Brushed-style metal with all-scalar inputs (id 34f2c1f9...).

    Spec example was 'Aluminum Brushed' (c12edfda...), but in the actual
    gpuopen corpus that material's metalness/roughness/color are
    nodegraph-bound — only ior/transmission are scalars there. We pick a
    different brushed metal from the same corpus that has all scalars
    populated, which exercises the same code path the spec intended.
    """
    xml = _load_or_skip("34f2c1f9-6169-4975-b5d8-4e21f49ddf55")
    pbr = parse_standard_surface_scalars(xml, material_id="brushed-metal")
    assert pbr.metalness == pytest.approx(1.0, rel=1e-5)
    assert pbr.color_rgb is not None
    assert len(pbr.color_rgb) == 3
    for c in pbr.color_rgb:
        assert isinstance(c, float)
        assert 0.0 <= c <= 1.0  # finite + bounded
    assert pbr.roughness is not None
    assert 0.0 <= pbr.roughness <= 1.0


def test_real_corpus_glass_transmissive_scalars():
    """Glass material — fully scalar transmission/ior (id d02bc0a9...)."""
    xml = _load_or_skip("d02bc0a9-2009-4d8c-b2e0-1f3c75577127")
    pbr = parse_standard_surface_scalars(xml, material_id="glass")
    assert pbr.transmission is not None
    assert pbr.transmission > 0
    assert pbr.ior is not None
    assert pbr.ior > 1


def test_real_corpus_aluminum_brushed_graph_constant():
    """Aluminum Brushed (c12edfda...) — the motivating case for #290's
    graph-constant walker.

    In this material:
      - `metalness` is graph-bound to NG_Aluminum_Brushed/metalness_output,
        which terminates in `<constant name="Metalness" value="1.0">`.
        The walker MUST resolve it to 1.0.
      - `base_color` is graph-bound but terminates in `<image>` — stays None.
      - `specular_roughness` is graph-bound but terminates in `<clamp>`
        (procedural) — stays None.
      - `specular_IOR` and `transmission` are direct scalars — populated
        as before.
      - `base` is a direct scalar (0.8...), but base_color is None so it
        has no effect on `color_rgb`.
    """
    xml = _load_or_skip("c12edfda-a5bd-4469-8147-4a6540a0a213")
    pbr = parse_standard_surface_scalars(xml, material_id="aluminum-brushed")
    # Newly resolvable via 1-hop graph walker.
    assert pbr.metalness == pytest.approx(1.0, rel=1e-5)
    # Direct scalars on the shader input.
    assert pbr.ior == pytest.approx(1.5, rel=1e-5)
    assert pbr.transmission == pytest.approx(0.0, abs=1e-9)
    # Procedural / image terminals — must stay texture-bound (None).
    assert pbr.roughness is None
    assert pbr.color_rgb is None


def test_real_corpus_layered_metal_all_populated():
    """Layered metal with metalness+roughness+ior all populated (4a18867d...).

    Spec called for 'Bronze Oxydized' but that material is nodegraph-bound
    in the corpus; substituting a layered scalar-bound metal exercises the
    same intent (all three of metalness, roughness, ior populated).
    """
    xml = _load_or_skip("4a18867d-88da-4e8f-aeb0-327015492558")
    pbr = parse_standard_surface_scalars(xml, material_id="layered-metal")
    assert pbr.metalness == pytest.approx(1.0, rel=1e-5)
    assert pbr.roughness is not None
    assert 0.0 <= pbr.roughness <= 1.0
    assert pbr.ior is not None
    assert pbr.ior > 1.0
