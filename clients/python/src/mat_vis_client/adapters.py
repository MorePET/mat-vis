"""mat-vis output format adapters — Three.js, glTF, MaterialX.

Converts generic scalars + texture bytes into renderer-specific formats.
Pure Python, zero dependencies (uses only stdlib xml.etree for MaterialX).

All functions take generic dicts — no Material class dependency:

    from adapters import to_threejs, to_gltf, export_mtlx
    result = to_threejs(scalars, textures)

Field name mapping follows docs/specs/field-name-mapping.md.
"""

from __future__ import annotations

import base64
import math
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Literal

from mat_vis_client.schema import (
    GLTF_MAP as _GLTF_TEX_MAP,
    THREEJS_MAP as _THREEJS_TEX_MAP,
    USD_PREVIEW_MAP as _USD_PREVIEW_TEX_MAP,
)

# Pillow is a soft dependency — only needed to pack metalness/roughness
# into a single glTF metallicRoughnessTexture (G=rough, B=metal, R=AO).
# Install via the optional `[gltf]` extra. Without it, to_gltf() emits a
# `_note_no_pillow` placeholder so consumers can detect the limitation.
try:
    from PIL import Image  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised via monkeypatch
    Image = None  # type: ignore[assignment]

# Renderer-prop maps come from schema.CHANNELS — do not hand-maintain
# parallel dicts here. Adding a channel is one edit in schema.py.


# ── Helpers ─────────────────────────────────────────────────────


def _to_data_uri(png_bytes: bytes) -> str:
    """Encode PNG bytes as a base64 data URI."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


# Spec defaults for KHR extensions — emitting an extension entry that
# matches the spec default is a no-op that bloats glTF output, so the
# adapter omits it. mat-vis#290.
_KHR_IOR_DEFAULT = 1.5
_KHR_TRANSMISSION_DEFAULT = 0.0
_KHR_CLEARCOAT_DEFAULT = 0.0


def _ior_at_default(ior: float | None) -> bool:
    """True if ``ior`` is None or matches the KHR spec default (1.5).

    Uses ``math.isclose`` because real corpus emits 1.5000000476837158
    (fp32 round-trip drift) for explicit ``specular_IOR=1.5``; raw
    ``!= 1.5`` would let the no-op extension through.
    """
    return ior is None or math.isclose(ior, _KHR_IOR_DEFAULT, rel_tol=1e-5)


def _transmission_at_default(t: float | None) -> bool:
    """True if ``t`` is None or matches the KHR spec default (0.0)."""
    return t is None or math.isclose(t, _KHR_TRANSMISSION_DEFAULT, abs_tol=1e-9)


def _clearcoat_at_default(c: float | None) -> bool:
    """True if ``c`` is None or matches the KHR_materials_clearcoat default (0.0)."""
    return c is None or math.isclose(c, _KHR_CLEARCOAT_DEFAULT, abs_tol=1e-9)


def _color_hex_to_int(hex_str: str) -> int:
    """Convert '#RRGGBB' hex string to an integer (Three.js color format).

    >>> _color_hex_to_int('#A0522D')
    10506797
    """
    return int(hex_str.lstrip("#"), 16)


def _color_hex_to_srgb_rgba(hex_str: str) -> tuple[float, float, float, float]:
    """Convert '#RRGGBB' to sRGB-encoded float-4 in [0, 1]. Alpha=1.0.

    NOT linear — see :func:`_srgb_to_linear` for the boundary conversion
    that ``_resolve_base_color`` applies before linear-space outputs.
    """
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r / 255.0, g / 255.0, b / 255.0, 1.0)


def _srgb_to_linear(c: float) -> float:
    """sRGB → linear per IEC 61966-2-1 (piecewise transfer function).

    Below 0.04045 the curve is the linear segment ``c / 12.92``;
    above, the gamma segment ``((c + 0.055) / 1.055) ** 2.4``.
    Used at every adapter color boundary so sRGB inputs land in
    linear-aware fields (glTF ``baseColorFactor``, MTLX
    ``diffuseColor``) without the silent over-bright bug that
    shipped through 0.6.x. ADR-0013 §Decision-1 / #304.
    """
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    """linear → sRGB, inverse of :func:`_srgb_to_linear`."""
    if c <= 0.0031308:
        return c * 12.92
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def _resolve_base_color(
    scalars: dict,
) -> tuple[float, float, float, float] | None:
    """Resolve the canonical base color as **linear RGBA** in [0, 1].

    Priority order (first non-None wins):
        1. ``base_color_linear`` (NEW canonical, 4-tuple linear, no transform)
        2. ``color_rgba`` (4-tuple sRGB-RGB + linear alpha, RGB de-gammas)
        3. ``color_hex`` (string sRGB ``#RRGGBB``, de-gammas, alpha=1.0)

    Multiple non-equal non-None values raise ``ValueError``. Equal
    canonical-form values pass.

    Returns ``None`` when no color key is present or all are None.

    ADR-0013 §Decision-1 / #304.
    """
    bcl = scalars.get("base_color_linear")
    rgba_in = scalars.get("color_rgba")
    hexv = scalars.get("color_hex")

    bcl_t: tuple[float, float, float, float] | None = (
        tuple(bcl) if bcl is not None else None  # type: ignore[assignment]
    )
    rgba_linear: tuple[float, float, float, float] | None = None
    if rgba_in is not None:
        r, g, b, a = rgba_in
        rgba_linear = (
            _srgb_to_linear(r),
            _srgb_to_linear(g),
            _srgb_to_linear(b),
            a,
        )
    hex_linear: tuple[float, float, float, float] | None = None
    if hexv is not None:
        sr, sg, sb, sa = _color_hex_to_srgb_rgba(hexv)
        hex_linear = (
            _srgb_to_linear(sr),
            _srgb_to_linear(sg),
            _srgb_to_linear(sb),
            sa,
        )

    candidates = [c for c in (bcl_t, rgba_linear, hex_linear) if c is not None]
    if not candidates:
        return None
    first = candidates[0]
    for cand in candidates[1:]:
        if not all(math.isclose(x, y, rel_tol=1e-6, abs_tol=1e-9) for x, y in zip(first, cand)):
            raise ValueError(
                "scalars contains multiple base-color keys with non-equal "
                "values; pick one of base_color_linear / color_rgba / color_hex"
            )
    return first


def _color_hex_to_rgba(hex_str: str) -> list[float]:
    """Deprecated 0.6.x helper retained for backward import compat. Kept
    as a thin wrapper around :func:`_color_hex_to_srgb_rgba` returning a
    list (the legacy signature). New code paths must use
    :func:`_resolve_base_color` to get the *linear* form.
    """
    return list(_color_hex_to_srgb_rgba(hex_str))


def _resolve_metalness(scalars: dict) -> float | None:
    """Resolve the metalness scalar accepting ``metallic`` as a glTF-spec alias.

    Three.js MeshPhysicalMaterial uses ``metalness``; glTF 2.0 spec calls
    the JSON field ``metallicFactor`` and the property *metallic*; py-mat
    stores it as ``metallic`` on its public ``Vis`` surface. Adapters
    accept either input key. ADR-0013 §Decision-3 / #303.
    """
    metalness = scalars.get("metalness")
    metallic = scalars.get("metallic")
    if metalness is not None and metallic is not None and metalness != metallic:
        raise ValueError(
            "scalars contains both 'metalness' and 'metallic' with non-equal "
            f"values ({metalness!r} vs {metallic!r}); pick one"
        )
    return metalness if metalness is not None else metallic


def _sanitize_material_name(name: str) -> str:
    """Sanitize a material name for filesystem + MaterialX XML safety.

    Real corpora contain spaces ("Stainless Steel 304"), slashes
    ("Saint-Gobain/LYSO"), and other path-unsafe / XML-name-unsafe
    characters. Replaces any non-[A-Za-z0-9_-] character with an
    underscore, strips leading/trailing underscores, falls back to
    "material" if the result is empty. Same rule used for both the
    on-disk filename and the MaterialX ``name=`` attributes (spaces /
    slashes break MTLX parsers anyway).

    ADR-0013 §Decision-4 / #305.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or "material"


# ── Three.js adapter ───────────────────────────────────────────


def to_threejs(
    scalars: dict,
    textures: dict[str, bytes] | None = None,
    *,
    color_format: Literal["hex", "int"] = "hex",
) -> dict:
    """Convert to a Three.js MeshPhysicalMaterial parameter dict.

    Args:
        scalars: Material scalars. Expected keys (all optional):
            - metalness (float 0-1) — also accepted as ``metallic``
              (glTF-spec alias). Setting both with non-equal values
              raises ValueError.
            - roughness (float 0-1)
            - base_color_linear (tuple[float,float,float,float] —
              canonical linear RGBA), or
            - color_rgba (tuple[float,float,float,float] — sRGB RGB
              + linear alpha; legacy alias), or
            - color_hex (str ``#RRGGBB`` — sRGB; legacy alias).
              ``ValueError`` on non-equal multiple base-color keys.
            - ior (float)
            - transmission (float 0-1)
            - emissive (tuple[float,float,float] linear RGB)
            - clearcoat (float 0-1)
        textures: Channel name -> PNG bytes. Keys are mat-vis channel
            names: color, normal, roughness, metalness, ao,
            displacement, emission.
        color_format: Output shape for ``result["color"]``. ``"hex"``
            (default since 0.7.0) emits ``"#RRGGBB"`` sRGB string;
            ``"int"`` emits a hex int (legacy form). Both round-trip
            lossless through ``THREE.MeshPhysicalMaterial`` (its
            ``Color.set`` dispatches by type). py-mat #99 / ADR-0013.

    Returns:
        Dict suitable for `new THREE.MeshPhysicalMaterial(result)`.
        Textures are embedded as base64 data URIs.

    Recommended ergonomic alternative: ``client.asset(src, mid, tier).to_threejs()``.
    """
    textures = textures or {}
    # Adapter is dumb — substrate values flow through verbatim. The
    # glTF-MR neutral-multiplier convention (color=[1,1,1] when a
    # colorMap is bound, metalness=1.0 when a metalnessMap is bound)
    # is materialized in the baker so every consumer (py / js / rust /
    # shell adapters AND search-side `pbr.metalness` readers) inherits
    # it from the substrate. mat-vis#290 baker-side follow-up.

    result: dict = {"type": "MeshPhysicalMaterial"}

    # Scalars
    metalness = _resolve_metalness(scalars)
    if metalness is not None:
        result["metalness"] = metalness
    if "roughness" in scalars and scalars["roughness"] is not None:
        result["roughness"] = scalars["roughness"]
    if color_format not in ("hex", "int"):
        raise ValueError(f"color_format must be 'hex' or 'int', got {color_format!r}")
    base_color = _resolve_base_color(scalars)
    if base_color is not None:
        # Three.js MeshPhysicalMaterial.color is sRGB by default
        # (ColorManagement r152+). Re-encode linear → sRGB regardless
        # of which input form the caller used.
        srgb = tuple(_linear_to_srgb(c) for c in base_color[:3])
        hex_str = "#{:02x}{:02x}{:02x}".format(
            int(round(max(0.0, min(1.0, srgb[0])) * 255)),
            int(round(max(0.0, min(1.0, srgb[1])) * 255)),
            int(round(max(0.0, min(1.0, srgb[2])) * 255)),
        )
        if color_format == "hex":
            # Preserve the input hex literal verbatim when the caller
            # passed color_hex — avoids surprise float-round byte
            # changes. Synthesized form covers tuple inputs.
            literal = scalars.get("color_hex")
            result["color"] = literal if literal is not None else hex_str
        else:  # "int"
            result["color"] = int(hex_str.lstrip("#"), 16)
    if "ior" in scalars and scalars["ior"] is not None:
        result["ior"] = scalars["ior"]
    if "transmission" in scalars and scalars["transmission"] is not None:
        result["transmission"] = scalars["transmission"]
    if scalars.get("emissive") is not None:
        result["emissive"] = list(scalars["emissive"])
    if scalars.get("clearcoat") is not None:
        result["clearcoat"] = scalars["clearcoat"]

    # Textures as data URIs
    for channel, prop in _THREEJS_TEX_MAP.items():
        if channel in textures:
            result[prop] = _to_data_uri(textures[channel])

    return result


# ── glTF adapter ────────────────────────────────────────────────


def to_gltf(
    scalars: dict,
    textures: dict[str, bytes] | None = None,
) -> dict:
    """Convert to a glTF pbrMetallicRoughness material dict.

    Args:
        scalars: Same as to_threejs().
        textures: Same as to_threejs().

    Returns:
        Dict conforming to glTF 2.0 material schema. Textures are
        embedded as base64 data URIs in the 'uri' field. When both
        metalness and roughness PNGs are present AND Pillow is
        installed (via the ``[gltf]`` extra), the two channels are
        packed into a single ``metallicRoughnessTexture`` per the
        glTF 2.0 spec (R=AO if available else 255, G=roughness,
        B=metalness). Without Pillow, a ``_note_no_pillow`` placeholder
        is emitted instead and the separate textures are dropped from
        the output (callers can install ``mat-vis-client[gltf]`` to
        enable packing).

    Recommended ergonomic alternative: ``client.asset(src, mid, tier).to_gltf()``.
    """
    textures = textures or {}
    # Adapter is dumb — substrate values flow through verbatim. The
    # glTF-MR neutral-multiplier convention (color=[1,1,1] when a
    # colorMap is bound, metalness=1.0 when a metalnessMap is bound)
    # is materialized in the baker so every consumer inherits it from
    # the substrate. mat-vis#290 baker-side follow-up.

    pbr: dict = {}
    material: dict = {"pbrMetallicRoughness": pbr}

    # Scalar factors
    metalness = _resolve_metalness(scalars)
    if metalness is not None:
        pbr["metallicFactor"] = metalness
    if "roughness" in scalars and scalars["roughness"] is not None:
        pbr["roughnessFactor"] = scalars["roughness"]
    base_color = _resolve_base_color(scalars)
    if base_color is not None:
        # glTF 2.0 §3.9.2 requires baseColorFactor in linear space.
        # _resolve_base_color de-gammas at the boundary regardless of
        # which input form the caller used. ADR-0013 §Decision-2.
        pbr["baseColorFactor"] = list(base_color)

    # IOR extension — omit when the value matches the spec default 1.5
    # (a no-op extension entry only bloats glTF output). mat-vis#290.
    # ``math.isclose`` tolerates fp32 round-trip drift (1.5000000476837158).
    ior = scalars.get("ior")
    if not _ior_at_default(ior):
        material.setdefault("extensions", {})["KHR_materials_ior"] = {"ior": ior}

    # Transmission extension — omit when zero/None (spec default).
    transmission = scalars.get("transmission")
    if not _transmission_at_default(transmission):
        material.setdefault("extensions", {})["KHR_materials_transmission"] = {
            "transmissionFactor": transmission
        }

    # Emissive — core glTF 2.0 material field (NOT under an extension).
    # Spec default is [0, 0, 0]; we emit on presence and let the no-op
    # case (all zeros) through since callers may want explicit black.
    emissive = scalars.get("emissive")
    if emissive is not None:
        material["emissiveFactor"] = list(emissive)

    # Clearcoat extension — omit when zero/None (spec default), mirrors
    # the KHR_materials_ior / _transmission suppression pattern.
    clearcoat = scalars.get("clearcoat")
    if not _clearcoat_at_default(clearcoat):
        material.setdefault("extensions", {})["KHR_materials_clearcoat"] = {
            "clearcoatFactor": clearcoat
        }

    # Textures
    def _tex_ref(png_bytes: bytes) -> dict:
        return {"source": {"uri": _to_data_uri(png_bytes)}}

    for channel, prop in _GLTF_TEX_MAP.items():
        if channel in textures:
            if prop in ("normalTexture", "occlusionTexture", "emissiveTexture"):
                material[prop] = _tex_ref(textures[channel])
            else:
                pbr[prop] = _tex_ref(textures[channel])

    # metallicRoughnessTexture: pack metalness + roughness into one
    # PNG per the glTF 2.0 spec (G=rough, B=metal, R=AO/255). Needs
    # Pillow — install ``mat-vis-client[gltf]`` to enable.
    if "metalness" in textures and "roughness" in textures:
        if Image is None:
            pbr["_note_no_pillow"] = (
                "Pillow is required to pack metallicRoughnessTexture "
                "(install `mat-vis-client[gltf]`). Separate metalness "
                "and roughness PNGs were dropped from the output."
            )
        else:
            packed_uri = _pack_metallic_roughness(
                metalness_png=textures["metalness"],
                roughness_png=textures["roughness"],
                ao_png=textures.get("ao"),
            )
            pbr["metallicRoughnessTexture"] = {"source": {"uri": packed_uri}}

    return material


def _pack_metallic_roughness(
    *,
    metalness_png: bytes,
    roughness_png: bytes,
    ao_png: bytes | None = None,
) -> str:
    """Pack metalness/roughness (and optional AO) into a glTF-compliant PNG.

    Per glTF 2.0: R=occlusion, G=roughness, B=metalness. If AO is not
    provided, R is filled with 255 (opaque white = no occlusion). The
    metalness image's dimensions are the reference — both roughness and
    AO are resized to match if they differ. Metalness is the reference
    because it is the channel most likely to be authored at the
    material's "true" resolution; roughness is often broadband.
    """
    assert Image is not None  # caller checks
    metal = Image.open(BytesIO(metalness_png)).convert("L")
    rough = Image.open(BytesIO(roughness_png)).convert("L")
    if rough.size != metal.size:
        rough = rough.resize(metal.size)

    if ao_png is not None:
        ao = Image.open(BytesIO(ao_png)).convert("L")
        if ao.size != metal.size:
            ao = ao.resize(metal.size)
        r_channel = ao
    else:
        r_channel = Image.new("L", metal.size, 255)

    packed = Image.merge("RGB", (r_channel, rough, metal))
    buf = BytesIO()
    packed.save(buf, format="PNG")
    return _to_data_uri(buf.getvalue())


# ── MaterialX adapter ──────────────────────────────────────────


def _build_mtlx_tree(
    scalars: dict,
    tex_filenames: dict[str, str],
    material_name: str,
) -> ET.Element:
    """Build a MaterialX 1.38 ElementTree for a UsdPreviewSurface material.

    Pure in-memory — no disk IO. Callers write the tree or serialize it
    to a string via :func:`_mtlx_tree_to_string`.

    Args:
        scalars: Material scalars (metalness, roughness, ior, etc).
        tex_filenames: Channel name -> texture file path string (already
            resolved; empty dict is valid — yields a scalar-only mat).
        material_name: Name for the material in the .mtlx document.

    Returns:
        Root ``<materialx>`` element.
    """
    root = ET.Element("materialx", version="1.38")

    # Nodegraph with image reads
    ng_name = f"{material_name}_textures"
    nodegraph = ET.SubElement(root, "nodegraph", name=ng_name)

    output_refs: dict[str, tuple[str, str]] = {}

    for ch, (usd_input, mtlx_type) in _USD_PREVIEW_TEX_MAP.items():
        if ch not in tex_filenames:
            continue

        img_name = f"img_{ch}"
        img = ET.SubElement(nodegraph, "image", name=img_name, type=mtlx_type)
        file_inp = ET.SubElement(img, "input", name="file", type="filename")
        file_inp.set("value", tex_filenames[ch])
        if ch in ("color", "emission"):
            file_inp.set("colorspace", "srgb_texture")

        if ch == "normal":
            nmap_name = f"normalmap_{ch}"
            nmap = ET.SubElement(nodegraph, "normalmap", name=nmap_name, type="vector3")
            ET.SubElement(nmap, "input", name="in", type="vector3").set("nodename", img_name)
            out_name = f"out_{ch}"
            ET.SubElement(nodegraph, "output", name=out_name, type="vector3").set(
                "nodename", nmap_name
            )
            output_refs[usd_input] = (out_name, "vector3")
        else:
            out_name = f"out_{ch}"
            ET.SubElement(nodegraph, "output", name=out_name, type=mtlx_type).set(
                "nodename", img_name
            )
            output_refs[usd_input] = (out_name, mtlx_type)

    # UsdPreviewSurface shader
    shader_name = f"{material_name}_shader"
    shader = ET.SubElement(root, "UsdPreviewSurface", name=shader_name, type="surfaceshader")

    # Scalar inputs on the shader
    if "roughness" in scalars and scalars["roughness"] is not None:
        if "roughness" not in tex_filenames:
            ET.SubElement(
                shader, "input", name="roughness", type="float", value=str(scalars["roughness"])
            )
    metalness = _resolve_metalness(scalars)
    if metalness is not None and "metalness" not in tex_filenames:
        ET.SubElement(shader, "input", name="metallic", type="float", value=str(metalness))
    if "ior" in scalars and scalars["ior"] is not None:
        ET.SubElement(shader, "input", name="ior", type="float", value=str(scalars["ior"]))

    # Diffuse color on the scalar path — when a "color" texture is bound,
    # the nodegraph path above provides diffuseColor via the <image>
    # node (with srgb_texture colorspace). The scalar fallback is for
    # PBR-scalar-only materials (most metals/plastics) so MTLX renderers
    # don't fall back to white. UsdPreviewSurface diffuseColor is linear
    # by convention — _resolve_base_color de-gammas at the boundary
    # regardless of input form. ADR-0013 §Decision-2 / #317 / #304.
    if "color" not in tex_filenames:
        base_color = _resolve_base_color(scalars)
        if base_color is not None:
            rgb = ",".join(f"{c:g}" for c in base_color[:3])
            ET.SubElement(shader, "input", name="diffuseColor", type="color3", value=rgb)

    # Emissive RGB on the shader scalar path. Texture-bound emission is
    # already routed through the nodegraph above (channel "emission").
    emissive = scalars.get("emissive")
    if emissive is not None and "emission" not in tex_filenames:
        rgb = ",".join(f"{c:g}" for c in tuple(emissive)[:3])
        ET.SubElement(shader, "input", name="emissiveColor", type="color3", value=rgb)

    # Connect texture outputs to shader
    for usd_input, (out_name, mtlx_type) in output_refs.items():
        inp = ET.SubElement(shader, "input", name=usd_input, type=mtlx_type)
        inp.set("nodegraph", ng_name)
        inp.set("output", out_name)

    # Surface material
    mat = ET.SubElement(root, "surfacematerial", name=material_name, type="material")
    ET.SubElement(
        mat, "input", name="surfaceshader", type="surfaceshader", nodename=f"{shader_name}"
    )

    ET.indent(root, space="  ")
    return root


def _mtlx_tree_to_string(root: ET.Element) -> str:
    """Serialize a MaterialX ElementTree to a string with XML declaration."""
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _resolve_tex_filenames(
    textures: dict[str, bytes] | None,
    output_dir: Path,
    material_name: str,
    texture_dir: str | Path | None,
    channels: list[str] | None,
) -> dict[str, str]:
    """Resolve the channel -> file path map for a mtlx document.

    In ``texture_dir`` mode: returns paths to existing PNGs in that dir.
    Otherwise: writes the ``textures`` dict as PNGs into ``output_dir``
    and returns the written basenames.
    """
    textures = textures or {}
    if texture_dir is not None:
        tex_dir = Path(texture_dir)
        available_channels = channels or []
        tex_filenames: dict[str, str] = {}
        for ch in available_channels:
            png_path = tex_dir / f"{ch}.png"
            if png_path.exists():
                tex_filenames[ch] = str(png_path)
        return tex_filenames

    tex_filenames = {}
    for channel, png_bytes in textures.items():
        if channel not in _USD_PREVIEW_TEX_MAP:
            continue
        png_filename = f"{material_name}_{channel}.png"
        (output_dir / png_filename).write_bytes(png_bytes)
        tex_filenames[channel] = png_filename
    return tex_filenames


def generate_mtlx_xml(
    scalars: dict,
    *,
    material_name: str = "Material",
    texture_dir: str | Path | None = None,
    channels: list[str] | None = None,
) -> str:
    """Return a MaterialX 1.38 XML document as a string.

    Pure in-memory — no files written. Used by :class:`MtlxSource.xml`
    to expose the synthesized document without materializing textures.

    Args:
        scalars: Material scalars (metalness, roughness, color_hex, ior, etc).
        material_name: Name for the material in the .mtlx document.
        texture_dir: Directory of existing texture PNGs to reference.
            If None, no texture nodes are emitted.
        channels: Channel names (color, normal, roughness, ...) present
            in ``texture_dir``; others are skipped.
    """
    safe_name = _sanitize_material_name(material_name)
    tex_filenames: dict[str, str] = {}
    if texture_dir is not None:
        tex_dir = Path(texture_dir)
        for ch in channels or []:
            png_path = tex_dir / f"{ch}.png"
            if png_path.exists():
                tex_filenames[ch] = str(png_path)
    root = _build_mtlx_tree(scalars, tex_filenames, safe_name)
    return _mtlx_tree_to_string(root)


def export_mtlx(
    scalars: dict,
    textures: dict[str, bytes] | None = None,
    output_dir: str | Path = ".",
    *,
    material_name: str = "Material",
    texture_dir: str | Path | None = None,
    channels: list[str] | None = None,
) -> Path:
    """Export as MaterialX .mtlx XML with referenced PNG files.

    Uses UsdPreviewSurface with a nodegraph for texture reads — valid
    MaterialX 1.38 that works with USD/Hydra renderers.

    Two modes:
        1. Pass ``textures`` dict: PNGs are written to output_dir,
           mtlx references them by filename.
        2. Pass ``texture_dir`` + ``channels``: no PNG writing, mtlx
           references existing files in texture_dir.

    Args:
        scalars: Material scalars (metalness, roughness, color_hex, ior, etc).
        textures: Channel name -> PNG bytes. Written to output_dir.
        output_dir: Directory for .mtlx (and .png files if textures provided).
        material_name: Name for the material in the .mtlx document.
        texture_dir: Path to existing texture PNGs. If set, textures param
            is ignored and no PNGs are written.
        channels: Channel names when using texture_dir mode.

    Returns:
        Path to the written .mtlx file.

    Recommended ergonomic alternative: ``client.asset(src, mid, tier).to_mtlx().export(dir)``.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    safe_name = _sanitize_material_name(material_name)
    tex_filenames = _resolve_tex_filenames(textures, out, safe_name, texture_dir, channels)
    root = _build_mtlx_tree(scalars, tex_filenames, safe_name)

    mtlx_path = out / f"{safe_name}.mtlx"
    mtlx_path.write_text(_mtlx_tree_to_string(root), encoding="utf-8")
    return mtlx_path
