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


def _color_hex_to_int(hex_str: str) -> int:
    """Convert '#RRGGBB' hex string to an integer (Three.js color format).

    >>> _color_hex_to_int('#A0522D')
    10506797
    """
    return int(hex_str.lstrip("#"), 16)


def _color_hex_to_rgba(hex_str: str) -> list[float]:
    """Convert '#RRGGBB' to glTF [R, G, B, A] floats in [0, 1]."""
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return [r / 255.0, g / 255.0, b / 255.0, 1.0]


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
) -> dict:
    """Convert to a Three.js MeshPhysicalMaterial parameter dict.

    Args:
        scalars: Material scalars. Expected keys (all optional):
            - metalness (float 0-1) — also accepted as ``metallic``
              (glTF-spec alias). Setting both with non-equal values
              raises ValueError.
            - roughness (float 0-1)
            - color_hex (str '#RRGGBB')
            - ior (float)
            - transmission (float 0-1)
        textures: Channel name -> PNG bytes. Keys are mat-vis channel
            names: color, normal, roughness, metalness, ao,
            displacement, emission.

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
    if "color_hex" in scalars and scalars["color_hex"] is not None:
        result["color"] = _color_hex_to_int(scalars["color_hex"])
    if "ior" in scalars and scalars["ior"] is not None:
        result["ior"] = scalars["ior"]
    if "transmission" in scalars and scalars["transmission"] is not None:
        result["transmission"] = scalars["transmission"]

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
    if "color_hex" in scalars and scalars["color_hex"] is not None:
        pbr["baseColorFactor"] = _color_hex_to_rgba(scalars["color_hex"])

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
