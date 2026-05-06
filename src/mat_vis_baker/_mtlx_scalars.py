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
}

# Inputs that have no PBRBlock home today. Non-zero values are dropped
# with a structured warning so a future schema add knows where to look.
_LOSSY_INPUTS: tuple[str, ...] = (
    "coat",
    "coat_roughness",
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
        if raw is None:
            continue
        val = _parse_float(raw)
        if val is not None:
            setattr(block, pbr_attr, val)

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
