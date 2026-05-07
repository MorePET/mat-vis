"""Tests for the procedural-PBR Phase 1 extension to ``_mtlx_scalars`` (#316).

Bronze Oxydized–style materials author metalness as a procedural
``<mix fg=A bg=B mix=t>`` graph rather than a single texture or scalar.
The current parser deliberately returns ``None`` for graph-bound
inputs (#290) — it only resolves the literal ``<constant>`` 1-hop case.
This module extends the parser two ways:

1. **Constant-folder.** When ``<mix>``'s ``fg``, ``bg``, AND ``mix``
   inputs all resolve to scalar constants, fold to ``bg + (fg-bg)*t``
   and set ``metalness_source="graph_constant"``. The contract for
   ``metalness`` (a numeric value where set is trustworthy) is
   preserved — this is a function evaluation, not a heuristic.

2. **Graph-walker estimate.** When ``fg`` resolves to ``1.0`` (the
   "pure metal" branch of a metal/dielectric mix), set
   ``is_conductor=True`` + ``metalness_mean=fg*t + bg*(1-t)`` (using
   the mix input's ``value`` default, falling back to 0.5 when the
   mix is texture-bound) and source="graph_estimate". The
   ``metalness`` scalar stays ``None`` — the contract is honest.

Provenance fields land on every populated path:
    "scalar"          — direct ``value=`` on the shader input
    "graph_constant"  — 1-hop nodegraph→constant OR mix-fold to constants
    "graph_estimate"  — fg=1.0 graph-walker estimate, metalness still None
    "texture"         — populated by the convention helper at bake time

#316 / #314.
"""

from __future__ import annotations

import math

from mat_vis_baker._mtlx_scalars import parse_standard_surface_scalars
from mat_vis_baker.common import PBRBlock, apply_pbr_neutral_multiplier_conventions


# ── fixtures: scalar provenance ────────────────────────────────


FIXTURE_SCALAR_AUTHORED = """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="m_shader" type="surfaceshader">
    <input name="metalness" type="float" value="0.5"/>
  </standard_surface>
</materialx>
"""


FIXTURE_GRAPH_CONSTANT_FOLDABLE_MIX = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng_metal">
    <constant name="c_fg" type="float">
      <input name="value" type="float" value="1.0"/>
    </constant>
    <constant name="c_bg" type="float">
      <input name="value" type="float" value="0.0"/>
    </constant>
    <constant name="c_mix" type="float">
      <input name="value" type="float" value="0.7"/>
    </constant>
    <mix name="m_metal" type="float">
      <input name="fg" nodename="c_fg"/>
      <input name="bg" nodename="c_bg"/>
      <input name="mix" nodename="c_mix"/>
    </mix>
    <output name="out_metal" type="float" nodename="m_metal"/>
  </nodegraph>
  <standard_surface name="m_shader" type="surfaceshader">
    <input name="metalness" type="float" nodegraph="ng_metal" output="out_metal"/>
  </standard_surface>
</materialx>
"""


# Bronze Oxydized–style: fg=1.0 (pure metal), bg=constant dielectric,
# mix=texture-bound mask. The walker should emit is_conductor=True +
# metalness_mean estimate; the metalness scalar stays None.
FIXTURE_BRONZE_OXYDIZED_LIKE = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng_metal">
    <constant name="c_fg" type="float">
      <input name="value" type="float" value="1.0"/>
    </constant>
    <constant name="c_bg" type="float">
      <input name="value" type="float" value="0.0"/>
    </constant>
    <image name="img_mask" type="float">
      <input name="file" type="filename" value="metalness_mask.png"/>
    </image>
    <mix name="m_metal" type="float">
      <input name="fg" nodename="c_fg"/>
      <input name="bg" nodename="c_bg"/>
      <input name="mix" nodename="img_mask" value="0.7"/>
    </mix>
    <output name="out_metal" type="float" nodename="m_metal"/>
  </nodegraph>
  <standard_surface name="m_shader" type="surfaceshader">
    <input name="metalness" type="float" nodegraph="ng_metal" output="out_metal"/>
  </standard_surface>
</materialx>
"""


# Same shape but no `value=` default on the mix input. Estimator falls
# back to the mid-mask 0.5 default so metalness_mean is still set.
FIXTURE_BRONZE_NO_MIX_DEFAULT = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng_metal">
    <constant name="c_fg" type="float">
      <input name="value" type="float" value="1.0"/>
    </constant>
    <constant name="c_bg" type="float">
      <input name="value" type="float" value="0.0"/>
    </constant>
    <image name="img_mask" type="float">
      <input name="file" type="filename" value="metalness_mask.png"/>
    </image>
    <mix name="m_metal" type="float">
      <input name="fg" nodename="c_fg"/>
      <input name="bg" nodename="c_bg"/>
      <input name="mix" nodename="img_mask"/>
    </mix>
    <output name="out_metal" type="float" nodename="m_metal"/>
  </nodegraph>
  <standard_surface name="m_shader" type="surfaceshader">
    <input name="metalness" type="float" nodegraph="ng_metal" output="out_metal"/>
  </standard_surface>
</materialx>
"""


# fg=0.0 (pure dielectric mixed with something) — must NOT set
# is_conductor=True. The walker only fires on fg=1.0.
FIXTURE_DIELECTRIC_MIX = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng_metal">
    <constant name="c_fg" type="float">
      <input name="value" type="float" value="0.0"/>
    </constant>
    <constant name="c_bg" type="float">
      <input name="value" type="float" value="0.0"/>
    </constant>
    <image name="img_mask" type="float">
      <input name="file" type="filename" value="metalness_mask.png"/>
    </image>
    <mix name="m_metal" type="float">
      <input name="fg" nodename="c_fg"/>
      <input name="bg" nodename="c_bg"/>
      <input name="mix" nodename="img_mask"/>
    </mix>
    <output name="out_metal" type="float" nodename="m_metal"/>
  </nodegraph>
  <standard_surface name="m_shader" type="surfaceshader">
    <input name="metalness" type="float" nodegraph="ng_metal" output="out_metal"/>
  </standard_surface>
</materialx>
"""


# Partial-conductor blend (fg=1.0, bg=0.3): a non-zero dielectric
# branch means the material isn't a clean metal/dielectric mix. The
# walker must NOT flip is_conductor=True (post-review tightening).
FIXTURE_PARTIAL_CONDUCTOR_BLEND = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng_metal">
    <constant name="c_fg" type="float">
      <input name="value" type="float" value="1.0"/>
    </constant>
    <constant name="c_bg" type="float">
      <input name="value" type="float" value="0.3"/>
    </constant>
    <image name="img_mask" type="float">
      <input name="file" type="filename" value="metalness_mask.png"/>
    </image>
    <mix name="m_metal" type="float">
      <input name="fg" nodename="c_fg"/>
      <input name="bg" nodename="c_bg"/>
      <input name="mix" nodename="img_mask"/>
    </mix>
    <output name="out_metal" type="float" nodename="m_metal"/>
  </nodegraph>
  <standard_surface name="m_shader" type="surfaceshader">
    <input name="metalness" type="float" nodegraph="ng_metal" output="out_metal"/>
  </standard_surface>
</materialx>
"""


# A <mix type="color3"> on the metalness input — schema-drift defense.
# The walker must early-return on non-float mix terminals so a future
# corpus shape doesn't accidentally trip is_conductor=True.
FIXTURE_COLOR3_MIX_ON_METALNESS = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng_metal">
    <constant name="c_fg" type="color3">
      <input name="value" type="color3" value="1.0, 1.0, 1.0"/>
    </constant>
    <constant name="c_bg" type="color3">
      <input name="value" type="color3" value="0.0, 0.0, 0.0"/>
    </constant>
    <constant name="c_mix" type="float">
      <input name="value" type="float" value="0.7"/>
    </constant>
    <mix name="m_metal" type="color3">
      <input name="fg" nodename="c_fg"/>
      <input name="bg" nodename="c_bg"/>
      <input name="mix" nodename="c_mix"/>
    </mix>
    <output name="out_metal" type="color3" nodename="m_metal"/>
  </nodegraph>
  <standard_surface name="m_shader" type="surfaceshader">
    <input name="metalness" type="float" nodegraph="ng_metal" output="out_metal"/>
  </standard_surface>
</materialx>
"""


# Genuine texture-bound metalness (no graph) — current behavior:
# parser leaves metalness=None and source=None. Convention helper
# (separate test below) is what flips it to "texture".
FIXTURE_PLAIN_TEXTURE_METAL = """<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng_metal">
    <image name="img" type="float">
      <input name="file" type="filename" value="metalness.png"/>
    </image>
    <output name="out_metal" type="float" nodename="img"/>
  </nodegraph>
  <standard_surface name="m_shader" type="surfaceshader">
    <input name="metalness" type="float" nodegraph="ng_metal" output="out_metal"/>
  </standard_surface>
</materialx>
"""


# ── tests: parser provenance + graph extension ─────────────────


class TestMetalnessSourceScalar:
    def test_direct_value_marks_source_scalar(self):
        pbr = parse_standard_surface_scalars(FIXTURE_SCALAR_AUTHORED, material_id="m-scalar")
        assert pbr.metalness == 0.5
        assert pbr.metalness_source == "scalar"
        assert pbr.metalness_mean is None
        assert pbr.is_conductor is None  # parser doesn't infer from scalar


class TestMetalnessSourceGraphConstant:
    def test_constant_foldable_mix_resolves_to_scalar(self):
        # fg=1.0, bg=0.0, mix=0.7 → fold to 0.0 + (1.0-0.0)*0.7 = 0.7
        pbr = parse_standard_surface_scalars(
            FIXTURE_GRAPH_CONSTANT_FOLDABLE_MIX, material_id="m-fold"
        )
        assert pbr.metalness is not None
        assert math.isclose(pbr.metalness, 0.7, abs_tol=1e-9)
        assert pbr.metalness_source == "graph_constant"


class TestMetalnessGraphEstimate:
    def test_bronze_oxydized_pattern_emits_is_conductor_and_mean(self):
        pbr = parse_standard_surface_scalars(FIXTURE_BRONZE_OXYDIZED_LIKE, material_id="m-bronze")
        assert pbr.is_conductor is True
        # fg=1.0, bg=0.0, mix=0.7 (the input default) → mean = 0*(1-0.7) + 1*0.7 = 0.7
        assert pbr.metalness_mean is not None
        assert math.isclose(pbr.metalness_mean, 0.7, abs_tol=1e-9)
        # The scalar contract stays honest — metalness is still None
        # because the value is per-pixel.
        assert pbr.metalness is None
        assert pbr.metalness_source == "graph_estimate"

    def test_no_mix_default_falls_back_to_mid_mask(self):
        pbr = parse_standard_surface_scalars(
            FIXTURE_BRONZE_NO_MIX_DEFAULT, material_id="m-bronze-nodefault"
        )
        assert pbr.is_conductor is True
        # fg=1.0, bg=0.0, mix unresolvable → fall back to 0.5
        assert pbr.metalness_mean is not None
        assert math.isclose(pbr.metalness_mean, 0.5, abs_tol=1e-9)
        assert pbr.metalness is None
        assert pbr.metalness_source == "graph_estimate"

    def test_dielectric_mix_does_not_flip_is_conductor(self):
        pbr = parse_standard_surface_scalars(FIXTURE_DIELECTRIC_MIX, material_id="m-die")
        # fg=0.0 → not a metal-mix; walker does NOT set is_conductor=True
        assert pbr.is_conductor is None
        assert pbr.metalness is None
        assert pbr.metalness_source is None

    def test_partial_conductor_blend_does_not_flip_is_conductor(self):
        # fg=1.0, bg=0.3 → not a clean metal/dielectric mix. Per the
        # post-review tightening, is_conductor stays None when the
        # dielectric branch is non-zero.
        pbr = parse_standard_surface_scalars(
            FIXTURE_PARTIAL_CONDUCTOR_BLEND, material_id="m-partial"
        )
        assert pbr.is_conductor is None
        assert pbr.metalness is None
        assert pbr.metalness_source is None
        assert pbr.metalness_mean is None

    def test_color3_mix_on_metalness_falls_through(self):
        # A <mix type="color3"> on the metalness input is schema drift
        # (metalness is a float). The walker must early-return — no
        # is_conductor flip, no estimate.
        pbr = parse_standard_surface_scalars(
            FIXTURE_COLOR3_MIX_ON_METALNESS, material_id="m-color3"
        )
        assert pbr.is_conductor is None
        assert pbr.metalness is None
        assert pbr.metalness_source is None
        assert pbr.metalness_mean is None


class TestPlainTextureBoundParserSide:
    def test_parser_leaves_provenance_unset(self):
        # The parser doesn't know about textures (the bake-time helper
        # does) — so for plain texture-bound metalness, every new field
        # stays None at parser time. The convention helper fills it in.
        pbr = parse_standard_surface_scalars(FIXTURE_PLAIN_TEXTURE_METAL, material_id="m-tex")
        assert pbr.metalness is None
        assert pbr.metalness_source is None
        assert pbr.is_conductor is None
        assert pbr.metalness_mean is None


# ── tests: convention helper sets source="texture" ─────────────


class TestConventionHelperSourceTexture:
    def test_texture_present_sets_metalness_and_source(self):
        pbr = PBRBlock()
        apply_pbr_neutral_multiplier_conventions(pbr, {"metalness": "metalness.png"})
        assert pbr.metalness == 1.0
        assert pbr.metalness_source == "texture"

    def test_no_texture_no_source(self):
        pbr = PBRBlock()
        apply_pbr_neutral_multiplier_conventions(pbr, {})
        assert pbr.metalness is None
        assert pbr.metalness_source is None

    def test_authored_metalness_preserved_with_existing_source(self):
        # Parser sets metalness=0.5 / source="scalar"; texture is also
        # present. The convention helper must NOT overwrite either.
        pbr = PBRBlock(metalness=0.5, metalness_source="scalar")
        apply_pbr_neutral_multiplier_conventions(pbr, {"metalness": "metalness.png"})
        assert pbr.metalness == 0.5
        assert pbr.metalness_source == "scalar"

    def test_zero_authored_metalness_preserved(self):
        # 0.0 is meaningful (Acoustic Foam mask=0); the convention
        # helper must not overwrite it — and source stays "scalar".
        pbr = PBRBlock(metalness=0.0, metalness_source="scalar")
        apply_pbr_neutral_multiplier_conventions(pbr, {"metalness": "metalness.png"})
        assert pbr.metalness == 0.0
        assert pbr.metalness_source == "scalar"


# ── tests: PBRBlock fields are real (no AttributeError) ────────


class TestPBRBlockFieldsExist:
    def test_new_fields_default_to_none(self):
        pbr = PBRBlock()
        assert pbr.is_conductor is None
        assert pbr.metalness_mean is None
        assert pbr.metalness_source is None

    def test_fields_are_settable(self):
        pbr = PBRBlock(
            metalness=None,
            is_conductor=True,
            metalness_mean=0.7,
            metalness_source="graph_estimate",
        )
        assert pbr.is_conductor is True
        assert pbr.metalness_mean == 0.7
        assert pbr.metalness_source == "graph_estimate"
