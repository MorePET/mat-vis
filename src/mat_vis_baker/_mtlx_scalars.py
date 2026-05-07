"""Parse MaterialX `<standard_surface>` scalars into a :class:`PBRBlock`.

Scope: gpuopen .mtlx files only (mat-vis#290 spike). The gpuopen corpus
mostly authors scalars as direct ``value=`` on the shader input, but a
non-trivial slice (e.g. *Aluminum Brushed*) routes them through a
``<nodegraph>`` whose terminal is a ``<constant>`` node. We resolve that
single hop — see :func:`_resolve_nodegraph_constant`.

  - One ``standard_surface`` shader per file (no UsdPreviewSurface, no
    open_pbr_surface in the wild today).
  - MaterialX 1.38, no document-level ``colorspace=`` attribute.
  - Scalar inputs are EITHER direct ``value="..."`` attributes OR a
    ``nodegraph=`` + ``output=`` pair whose graph output terminates in
    a ``<constant>`` node (1-hop resolution only).
  - Texture inputs use ``<nodegraph>`` references that terminate in
    ``<image>`` (or ``<multiply>``/``<mix>``/etc. procedural chains)
    — those stay as ``None`` here; the baker carries them as
    ``texture_paths`` separately.

We deliberately do NOT resolve through ``<multiply>``, ``<add>``,
``<mix>``, ``<convert>`` etc.: collapsing a procedural graph to a
single scalar would lie. Only the literal ``<constant>`` case promotes
to "authored scalar".

On any parse error (malformed XML, missing/unknown shader, unparsable
float) we log at WARNING and return an EMPTY :class:`PBRBlock` so the
fetcher can keep going — never crash, never fabricate defaults that
look like authored values.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from mat_vis_baker.common import PBRBlock

log = logging.getLogger("mat-vis-baker.gpuopen-scalars")


# Authored field map. Every input below appears in 100% of sampled
# gpuopen materials (see module docstring). Ordering doesn't matter —
# we look up each name in the parsed input dict.
_FLOAT_INPUTS: dict[str, str] = {
    # mtlx input name -> PBRBlock attribute
    "metalness": "metalness",
    "specular_roughness": "roughness",
    "specular_IOR": "ior",  # uppercase IOR — case-aware lookup
    "transmission": "transmission",
    # Full MeshPhysicalMaterial coverage (#340).
    "specular": "specular_intensity",  # KHR_materials_specular.specularFactor
    "transmission_dispersion": "dispersion",  # KHR_materials_dispersion.dispersion
    "coat_roughness": "clearcoat_roughness",  # KHR_materials_clearcoat.clearcoatRoughnessFactor
}

# Color3 inputs. Same direct value=/1-hop graph→constant promotion as
# float scalars; emit as a 3-list of linear floats (gpuopen authors with
# linear values per MaterialX 1.38 spec).
_COLOR3_INPUTS: dict[str, str] = {
    # mtlx input name -> PBRBlock attribute
    "specular_color": "specular_color",  # KHR_materials_specular.specularColorFactor
}

# Inputs that have no PBRBlock home today. Non-zero values are dropped
# with a structured warning so a future schema add knows where to look.
_LOSSY_INPUTS: tuple[str, ...] = (
    "coat",
    "coat_IOR",
    "coat_color",
    "sheen",
    "sheen_roughness",
    "sheen_color",
    "subsurface",
    "subsurface_color",
    "subsurface_radius",
    "thin_film_thickness",
    "emission",
    "emission_color",
)


def _strip_ns(tag: str) -> str:
    """Drop the ``{namespace}`` prefix off an XML tag (defensive — the
    probe shows no namespace in the gpuopen corpus, but cheap to
    handle)."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_color3(raw: str) -> list[float] | None:
    """Parse a color3 ``"r, g, b"`` string into ``[r, g, b]`` floats."""
    try:
        parts = [float(p.strip()) for p in raw.split(",")]
    except (TypeError, ValueError, AttributeError):
        return None
    if len(parts) != 3:
        return None
    return parts


def _is_authored_value(inp: ET.Element) -> bool:
    """An input is authored as a scalar iff it carries ``value=`` and is
    NOT bound to a node (``nodename=`` or ``nodegraph=``)."""
    if "value" not in inp.attrib:
        return False
    if "nodename" in inp.attrib or "nodegraph" in inp.attrib:
        return False
    return True


def _resolve_nodegraph_constant(
    root: ET.Element,
    nodegraph_name: str,
    output_name: str,
) -> str | None:
    """If ``<nodegraph name=X><output name=Y nodename=Z/></nodegraph>``
    resolves to a ``<constant><input name="value" value="V"/></constant>``,
    return ``V`` as a string. Otherwise return ``None``.

    Only resolves the simple constant case — does NOT resolve through
    ``<multiply>``, ``<add>``, ``<mix>``, ``<convert>``, etc. (those
    would be lossy abstractions of a procedural graph; emitting a
    single scalar would lie). Other shapes fall back to the existing
    nodegraph-as-texture-bound treatment.
    """
    # Step 1: locate the named nodegraph as a descendant of root.
    target_ng: ET.Element | None = None
    for elem in root.iter():
        if _strip_ns(elem.tag) == "nodegraph" and elem.attrib.get("name") == nodegraph_name:
            target_ng = elem
            break
    if target_ng is None:
        return None

    # Step 2: locate the named <output> child and read its nodename=.
    terminal_node_name: str | None = None
    for child in target_ng:
        if _strip_ns(child.tag) != "output":
            continue
        if child.attrib.get("name") != output_name:
            continue
        terminal_node_name = child.attrib.get("nodename")
        break
    if not terminal_node_name:
        return None

    # Step 3: look up the terminal node by name within the same graph.
    terminal: ET.Element | None = None
    for child in target_ng:
        if child.attrib.get("name") == terminal_node_name:
            terminal = child
            break
    if terminal is None:
        return None

    # Step 4: only the literal <constant> case promotes to scalar.
    if _strip_ns(terminal.tag) != "constant":
        return None

    for sub in terminal:
        if _strip_ns(sub.tag) != "input":
            continue
        if sub.attrib.get("name") != "value":
            continue
        return sub.attrib.get("value")
    return None


def _input_value_or_graph_constant(
    root: ET.Element,
    inp: ET.Element,
) -> str | None:
    """Return the authored scalar string for ``inp``, resolving a 1-hop
    nodegraph→constant if needed.

    Returns ``None`` for genuinely texture-bound inputs (graph terminal
    is anything other than ``<constant>``). Direct ``value=`` (with no
    binding) is returned unchanged.
    """
    if _is_authored_value(inp):
        return inp.attrib["value"]
    ng = inp.attrib.get("nodegraph")
    out = inp.attrib.get("output")
    if ng and out and "value" not in inp.attrib:
        return _resolve_nodegraph_constant(root, ng, out)
    return None


def _find_named_node(graph: ET.Element, name: str) -> ET.Element | None:
    for child in graph:
        if child.attrib.get("name") == name:
            return child
    return None


def _node_constant_value(node: ET.Element) -> float | None:
    """If ``node`` is a ``<constant>`` whose ``<input name="value">``
    parses as a float, return it. Otherwise None.
    """
    if _strip_ns(node.tag) != "constant":
        return None
    for sub in node:
        if _strip_ns(sub.tag) != "input":
            continue
        if sub.attrib.get("name") != "value":
            continue
        return _parse_float(sub.attrib.get("value", ""))
    return None


def _resolve_input_constant(graph: ET.Element, inp: ET.Element) -> float | None:
    """Resolve a ``<mix>``-input element to a float constant.

    Handles two shapes:
      - ``nodename=`` pointing at a ``<constant>`` sibling.
      - inline ``value=`` attribute (e.g. ``<input name="mix"
        nodename="img_mask" value="0.7"/>`` — the texture-bound case
        where the author left a default for non-rendering consumers).
    """
    nm = inp.attrib.get("nodename")
    if nm is not None:
        target = _find_named_node(graph, nm)
        if target is not None:
            v = _node_constant_value(target)
            if v is not None:
                return v
    raw = inp.attrib.get("value")
    if raw is not None:
        return _parse_float(raw)
    return None


_MIX_RECURSION_DEPTH = 3


def _resolve_mix_input(
    graph: ET.Element,
    inp: ET.Element,
    depth: int,
    visited: frozenset[str],
) -> tuple[str, float | None, bool]:
    """Resolve a single ``<mix>`` input element to a typed result.

    Returns ``(kind, value, saw_one)`` where:

    - ``kind="const"``: input resolves to a scalar constant; ``value``
      is that float.
    - ``kind="texture"``: input is bound to an ``<extract>`` / ``<image>``
      (per-pixel value). ``value`` is None.
    - ``kind="estimate"``: input is a nested ``<mix>`` whose recursive
      evaluation yielded an estimate. ``value`` is the estimate mean.
    - ``kind="unresolved"``: structure unrecognized (unknown node type,
      depth-cap, cycle, missing target). ``value`` is None.

    ``saw_one``: did the input's resolution chain encounter at least
    one constant ≈1.0 in a ``<mix>`` ``fg``/``bg`` slot? Propagated
    upward so a top-level walker can stamp ``is_conductor=True`` on
    nested-mix graphs whose computational output is texture-bound but
    whose structural intent is metal-side (Bronze Oxydized pattern).
    Only counts ``<mix>`` slots — never ``<multiply>``/``<add>``/etc.
    """
    # Inline value= without a binding — direct scalar.
    nm = inp.attrib.get("nodename")
    if nm is None:
        raw = inp.attrib.get("value")
        if raw is not None:
            v = _parse_float(raw)
            if v is not None:
                return "const", v, abs(v - 1.0) < 1e-6
        return "unresolved", None, False

    # Bound via nodename — locate the target node.
    target = _find_named_node(graph, nm)
    if target is None:
        # External reference (graph <input> pin or other). If the
        # mix-input also carries an inline value=, honor it as the
        # author's default for non-rendering consumers.
        raw = inp.attrib.get("value")
        if raw is not None:
            v = _parse_float(raw)
            if v is not None:
                return "const", v, abs(v - 1.0) < 1e-6
        return "texture", None, False

    tag = _strip_ns(target.tag)
    if tag == "constant":
        v = _node_constant_value(target)
        if v is not None:
            return "const", v, abs(v - 1.0) < 1e-6
        return "unresolved", None, False
    if tag == "mix":
        if depth <= 0 or nm in visited:
            return "unresolved", None, False
        kind, value, _is_cond, saw_one = _evaluate_mix_node(
            graph, target, depth - 1, visited | {nm}
        )
        return kind, value, saw_one
    if tag in ("extract", "image"):
        # Honor an inline value= default even when the input is bound
        # to a texture node — authors leave defaults for non-rendering
        # consumers (Bronze-like pattern with explicit value="0.7" on
        # a texture-bound mix slot). Tagged as "textural_default" not
        # "const" so the full-fold path doesn't fire (rendering is
        # per-pixel — folding to a single scalar would lie). The
        # conductor heuristic still uses the value as t_eff. #316.
        raw = inp.attrib.get("value")
        if raw is not None:
            v = _parse_float(raw)
            if v is not None:
                return "textural_default", v, abs(v - 1.0) < 1e-6
        return "texture", None, False
    # Unknown / unhandled shape (multiply, add, switch, …). Honor an
    # inline value= default if present, otherwise unresolved.
    raw = inp.attrib.get("value")
    if raw is not None:
        v = _parse_float(raw)
        if v is not None:
            return "const", v, abs(v - 1.0) < 1e-6
    return "unresolved", None, False


def _evaluate_mix_node(
    graph: ET.Element,
    mix: ET.Element,
    depth: int,
    visited: frozenset[str],
) -> tuple[str, float | None, bool, bool]:
    """Recursive evaluator for a ``<mix>`` node.

    Returns ``(kind, value, is_conductor, saw_one)``:

    - ``("const", v, False, saw_one)``: fully constant-foldable; ``v``
      is the folded scalar.
    - ``("estimate", mean, is_cond, saw_one)``: heuristic estimate;
      ``is_cond=True`` indicates the conductor heuristic fired.
    - ``("texture", None, False, saw_one)``: chain ends in a texture
      passthrough (no rigorous scalar). ``saw_one`` propagates
      structural metal-side intent.
    - ``("unresolved", None, False, False)``: parse failure / depth-cap /
      cycle.

    Phase 1.5 enhancements:
      1. mix=0 / mix=1 special-case fold: when ``mix`` resolves to 0.0
         or 1.0, return the corresponding branch's result directly. The
         discarded branch's resolvability doesn't matter.
      2. Symmetric conductor heuristic: stamps when EITHER ``fg≈1.0``
         with a dielectric/texture ``bg`` (Phase 1 case) OR ``bg≈1.0``
         with a dielectric/texture ``fg`` (NEW — Brass Satin pattern).
      3. Bounded recursion: nested ``<mix>`` resolved up to 3 levels
         with cycle protection via ``visited`` set.
      4. Structural conductor signal: when the chain contains a
         constant ≈1.0 in a ``<mix>`` slot, ``saw_one`` propagates so
         that texture-bound nested graphs (Bronze) still classify as
         conductor at the top level.
    """
    # Type guard — only scalar mixes.
    if mix.attrib.get("type") not in (None, "float"):
        return "unresolved", None, False, False

    fg_inp: ET.Element | None = None
    bg_inp: ET.Element | None = None
    mix_inp: ET.Element | None = None
    for sub in mix:
        if _strip_ns(sub.tag) != "input":
            continue
        nm = sub.attrib.get("name")
        if nm == "fg":
            fg_inp = sub
        elif nm == "bg":
            bg_inp = sub
        elif nm == "mix":
            mix_inp = sub
    if fg_inp is None or bg_inp is None or mix_inp is None:
        return "unresolved", None, False, False

    fg_kind, fg_val, fg_saw = _resolve_mix_input(graph, fg_inp, depth, visited)
    bg_kind, bg_val, bg_saw = _resolve_mix_input(graph, bg_inp, depth, visited)
    mix_kind, mix_val, _mix_saw = _resolve_mix_input(graph, mix_inp, depth, visited)

    # Aggregate "saw_one" across all three slots — chain-level signal.
    saw_one = fg_saw or bg_saw or _mix_saw

    # (1) mix=0 / mix=1 special-case fold. Discarded branch's kind is
    # irrelevant for these terminal cases — output is wholly the
    # selected side.
    if mix_kind == "const" and mix_val is not None:
        if abs(mix_val) < 1e-6:
            # mix=0 → output = bg
            return bg_kind, bg_val, False, saw_one
        if abs(mix_val - 1.0) < 1e-6:
            # mix=1 → output = fg
            return fg_kind, fg_val, False, saw_one

    # (2) Full constant-fold: fg, bg, mix all resolve to constants.
    if (
        fg_kind == "const"
        and bg_kind == "const"
        and mix_kind == "const"
        and fg_val is not None
        and bg_val is not None
        and mix_val is not None
    ):
        metal = bg_val + (fg_val - bg_val) * mix_val
        return "const", metal, False, saw_one

    # (3) Conductor heuristic (symmetric).
    # ``textural_default`` accepted alongside ``const`` for the metal
    # side — author's per-pixel default flagging the input intent.
    _const_kinds = ("const", "textural_default")
    fg_is_metal = fg_kind in _const_kinds and fg_val is not None and abs(fg_val - 1.0) < 1e-6
    bg_is_metal = bg_kind in _const_kinds and bg_val is not None and abs(bg_val - 1.0) < 1e-6

    def _is_dielectric_or_texture(kind: str, val: float | None) -> bool:
        # Constant ≈0, texture-bound, or a sub-mix that resolved to an
        # estimate < 0.5 all count as the "non-metal" side.
        if kind in _const_kinds:
            return val is not None and abs(val) < 1e-6
        if kind == "texture":
            return True
        if kind == "estimate":
            return val is not None and val < 0.5
        return False

    bg_is_nonmetal = _is_dielectric_or_texture(bg_kind, bg_val)
    fg_is_nonmetal = _is_dielectric_or_texture(fg_kind, fg_val)

    fg_side_conductor = fg_is_metal and bg_is_nonmetal
    bg_side_conductor = bg_is_metal and fg_is_nonmetal

    if fg_side_conductor or bg_side_conductor:
        # Compute the heuristic mean. Defaults: 0.5 for unresolvable
        # fg/bg/mix (mid-mask). Saturates when conductor side dominates.
        fg_eff = fg_val if fg_val is not None else 0.5
        bg_eff = bg_val if bg_val is not None else 0.5
        t_eff = mix_val if (mix_kind in _const_kinds and mix_val is not None) else 0.5
        mean = bg_eff + (fg_eff - bg_eff) * t_eff
        # Clamp to a sane range — heuristic shouldn't produce weird
        # negatives or >1 values from edge-case combinations.
        mean = max(0.0, min(1.0, mean))
        return "estimate", mean, True, saw_one

    # (4) Texture-bound passthrough with structural conductor signal.
    # The chain has a constant ≈1.0 somewhere in <mix> slots — interpret
    # as "intended-as-metal" even if the rigorous output is texture.
    # Library-browser facet, not a render value (#346 issue body).
    if (fg_kind == "texture" or bg_kind == "texture") and saw_one:
        return "estimate", 1.0, True, saw_one

    # (5) Pure texture passthrough, no metal signal.
    if fg_kind == "texture" or bg_kind == "texture":
        return "texture", None, False, saw_one

    return "unresolved", None, False, saw_one


def _walk_mix_metalness(
    root: ET.Element,
    nodegraph_name: str,
    output_name: str,
) -> tuple[float | None, bool | None, float | None, str | None]:
    """Inspect a ``<mix>`` graph terminal for a metalness binding.

    Returns ``(metalness, is_conductor, metalness_mean, source)``.

    Phase 1 (#316): direct ``<mix>`` with all-constant fg/bg/mix
    siblings folded to ``graph_constant``; ``fg≈1.0`` + dielectric bg
    with texture-bound mix stamped ``graph_estimate`` + ``is_conductor``.

    Phase 1.5 (#346) extends to: nested ``<mix>`` (depth ≤ 3) with
    cycle protection; ``mix=0`` / ``mix=1`` special-case fold (discarded
    branch's resolvability irrelevant); symmetric conductor heuristic
    (``bg≈1.0`` with dielectric/texture ``fg`` — Brass Satin pattern);
    texture-passthrough chains carrying a constant ≈1.0 stamped as
    structural conductor (Bronze Oxydized pattern). See
    :func:`_evaluate_mix_node` for the recursive design.
    """
    # Locate the named nodegraph + the terminal node referenced by the output.
    target_ng: ET.Element | None = None
    for elem in root.iter():
        if _strip_ns(elem.tag) == "nodegraph" and elem.attrib.get("name") == nodegraph_name:
            target_ng = elem
            break
    if target_ng is None:
        return None, None, None, None

    terminal_node_name: str | None = None
    for child in target_ng:
        if _strip_ns(child.tag) != "output":
            continue
        if child.attrib.get("name") != output_name:
            continue
        terminal_node_name = child.attrib.get("nodename")
        break
    if not terminal_node_name:
        return None, None, None, None

    terminal = _find_named_node(target_ng, terminal_node_name)
    if terminal is None or _strip_ns(terminal.tag) != "mix":
        return None, None, None, None

    kind, value, is_cond, saw_one = _evaluate_mix_node(
        target_ng, terminal, _MIX_RECURSION_DEPTH, frozenset({terminal_node_name})
    )
    if kind == "const" and value is not None:
        return value, None, None, "graph_constant"
    if kind == "estimate" and is_cond:
        return None, True, value, "graph_estimate"
    # Structural conductor signal: chain ends in texture passthrough
    # but contains a constant ≈1.0 in a <mix> fg/bg/mix slot. Bronze
    # Oxydized's nested mix=1 chain is the canonical case — rigorous
    # evaluation reduces to a texture extract, but the all-1.0
    # structural intent classifies it as conductor for the
    # library-browser facet (#346 issue body).
    if kind == "texture" and saw_one:
        return None, True, 1.0, "graph_estimate"
    return None, None, None, None


def parse_standard_surface_scalars(
    mtlx_xml: str,
    *,
    material_id: str = "",
) -> PBRBlock:
    """Return a :class:`PBRBlock` populated from a gpuopen .mtlx string.

    Texture-bound inputs leave the corresponding field as ``None`` —
    adapters apply their own neutral defaults (e.g. baseColorFactor
    [1,1,1] when a colorMap is present).

    Args:
        mtlx_xml: Full .mtlx XML as a string.
        material_id: For structured warnings — passed through into log
            messages so multi-material bakes are debuggable.
    """
    try:
        root = ET.fromstring(mtlx_xml)
    except ET.ParseError as exc:
        log.warning("%s: malformed mtlx XML (%s); returning empty PBRBlock", material_id, exc)
        return PBRBlock()

    # Find the first standard_surface shader. Walk the tree (the shader
    # is usually a direct child of <materialx> but tolerate nesting).
    shader: ET.Element | None = None
    for elem in root.iter():
        if _strip_ns(elem.tag) == "standard_surface":
            shader = elem
            break

    if shader is None:
        # Distinguish "wrong shader type" from "no shader at all" for log
        # signal. The probe says 100% of gpuopen materials use
        # standard_surface today, so anything else is novel and worth
        # a separate breadcrumb.
        other = next(
            (
                _strip_ns(e.tag)
                for e in root.iter()
                if _strip_ns(e.tag) in {"UsdPreviewSurface", "open_pbr_surface", "surface"}
            ),
            None,
        )
        if other:
            log.warning(
                "%s: unsupported shader type %s (expected standard_surface); empty PBRBlock",
                material_id,
                other,
            )
        else:
            log.warning("%s: no standard_surface shader in mtlx; empty PBRBlock", material_id)
        return PBRBlock()

    # Collect <input> children keyed by name attribute.
    inputs: dict[str, ET.Element] = {}
    for child in shader:
        if _strip_ns(child.tag) != "input":
            continue
        nm = child.attrib.get("name")
        if nm:
            inputs[nm] = child

    block = PBRBlock()

    # Float scalars. Direct `value=` wins; otherwise we attempt a 1-hop
    # nodegraph→<constant> resolution for inputs bound via
    # `nodegraph=`+`output=` (real-corpus pattern: Aluminum Brushed et al).
    for mtlx_name, pbr_attr in _FLOAT_INPUTS.items():
        inp = inputs.get(mtlx_name)
        if inp is None:
            continue
        raw = _input_value_or_graph_constant(root, inp)
        if raw is not None:
            val = _parse_float(raw)
            if val is not None:
                setattr(block, pbr_attr, val)
                # Provenance for metalness only — the field that the
                # Phase 1 issue (#316) cares about for library-browser
                # facets. ``source="scalar"`` for direct value=,
                # "graph_constant" for the 1-hop nodegraph→constant.
                if pbr_attr == "metalness":
                    direct = _is_authored_value(inp)
                    block.metalness_source = "scalar" if direct else "graph_constant"
            continue
        # Texture/graph-bound. For metalness specifically, attempt the
        # <mix> walker — fg=1.0 mixes give us is_conductor + estimate,
        # fully-foldable mixes give us a real scalar. Other shapes
        # leave the field None for the convention helper to fill.
        if pbr_attr == "metalness":
            ng = inp.attrib.get("nodegraph")
            out = inp.attrib.get("output")
            if ng and out:
                metal, is_cond, mean, source = _walk_mix_metalness(root, ng, out)
                if metal is not None:
                    block.metalness = metal
                if is_cond is not None:
                    block.is_conductor = is_cond
                if mean is not None:
                    block.metalness_mean = mean
                if source is not None:
                    block.metalness_source = source

    # base_color (color3) — multiplied by `base` scalar if both authored.
    # Both base_color and base accept the same 1-hop graph→constant
    # promotion; in practice gpuopen graph-bound `base_color` terminates
    # in <image>, so this stays None for those — exactly what we want.
    color_inp = inputs.get("base_color")
    if color_inp is not None:
        raw_rgb = _input_value_or_graph_constant(root, color_inp)
        if raw_rgb is not None:
            rgb = _parse_color3(raw_rgb)
            if rgb is not None:
                base_inp = inputs.get("base")
                base_mul: float | None = None
                if base_inp is not None:
                    raw_base = _input_value_or_graph_constant(root, base_inp)
                    if raw_base is not None:
                        base_mul = _parse_float(raw_base)
                if base_mul is not None:
                    rgb = [c * base_mul for c in rgb]
                block.color_rgb = rgb

    # Additional color3 scalars (#340). Same extraction pattern as
    # base_color, no `base` multiplier coupling. MaterialX 1.38 spec
    # treats `specular_color` as linear RGB by default.
    for mtlx_name, pbr_attr in _COLOR3_INPUTS.items():
        c_inp = inputs.get(mtlx_name)
        if c_inp is None:
            continue
        raw_c = _input_value_or_graph_constant(root, c_inp)
        if raw_c is not None:
            parts = _parse_color3(raw_c)
            if parts is not None:
                setattr(block, pbr_attr, parts)

    # thickness ← transmission_depth, but ONLY when transmission > 0.
    # MaterialX `transmission_depth` is the absorption-distance scalar
    # for transmissive materials (KHR_materials_volume.thicknessFactor
    # equivalent). For opaque materials (transmission=0), the depth
    # value is dead-code authoring scaffold and has no rendering
    # meaning; emit None so the adapter doesn't ship a no-op extension.
    # Survey: only 8/454 gpuopen materials have transmission>0.
    if block.transmission is not None and block.transmission > 0.0:
        depth_inp = inputs.get("transmission_depth")
        if depth_inp is not None:
            raw_d = _input_value_or_graph_constant(root, depth_inp)
            if raw_d is not None:
                d = _parse_float(raw_d)
                if d is not None and d > 0.0:
                    block.thickness = d

    # Lossy inputs — log structured warning per non-zero authored value.
    for name in _LOSSY_INPUTS:
        inp = inputs.get(name)
        if inp is None or not _is_authored_value(inp):
            continue
        raw = inp.attrib["value"]
        # A scalar input is non-zero if either:
        #  - it's a float, and the value isn't 0
        #  - it's a color3, and any component isn't 0
        # The string-split heuristic must NOT be used: real gpuopen
        # corpus emits color3 like " 0.000000, 0.000000, 0.000000"
        # which would falsely trip a "p != '0'" comparison.
        f = _parse_float(raw)
        if f is not None:
            is_nonzero = f != 0.0
        else:
            parts = _parse_color3(raw)
            is_nonzero = parts is not None and any(c != 0.0 for c in parts)
        if is_nonzero:
            # DEBUG (not INFO): ~454 materials × ~3 lossy inputs = ~1.5k
            # lines per bake; only useful when troubleshooting.
            log.debug(
                "%s: lossy coat input %s=%s dropped (no PBRBlock home)",
                material_id,
                name,
                raw,
            )

    return block
