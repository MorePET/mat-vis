"""Shared types, constants, and utilities for the mat-vis baker."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import requests

# Source of truth: docs/specs/index-schema.json, synced into
# src/mat_vis_baker/_spec/ by scripts/sync-spec.py (pre-commit hook).
# Loaded once via spec.py; do NOT hardcode these lists anywhere else.
from mat_vis_baker.spec import CATEGORIES as _CATEGORIES_FN
from mat_vis_baker.spec import CHANNELS as _CHANNELS_FN
from mat_vis_baker.spec import SOURCES as _SOURCES_FN

log = logging.getLogger("mat-vis-baker")

# ── canonical enums ─────────────────────────────────────────────

CANONICAL_CATEGORIES = frozenset(_CATEGORIES_FN())
CANONICAL_CHANNELS = list(_CHANNELS_FN())
CANONICAL_SOURCES = tuple(_SOURCES_FN())

# Texture tiers — NOT in the schema enum (list grows dynamically); validated
# against the manifest instead. Keep this as the authoritative baker-side
# list for CLI `choices=` constraints.
VALID_TIERS = ["128", "256", "512", "1k", "2k", "4k", "8k"]

TIER_TO_PX = {"128": 128, "256": 256, "512": 512, "1k": 1024, "2k": 2048, "4k": 4096, "8k": 8192}

# SSoT for baker version: pyproject.toml. Baker stamps this into parquet
# metadata and HTTP User-Agent; derive from installed metadata so the two
# can never drift from each other or from the wheel.
try:
    BAKER_VERSION = _pkg_version("mat-vis")
except PackageNotFoundError:
    BAKER_VERSION = "0.0.0+dev"

USER_AGENT = f"mat-vis-baker/{BAKER_VERSION}"

# ── category normalization ──────────────────────────────────────

_CATEGORY_MAP: dict[str, str] = {}
for _cat, _keywords in {
    "metal": [
        "metal",
        "steel",
        "iron",
        "aluminum",
        "aluminium",
        "copper",
        "brass",
        "bronze",
        "chrome",
        "gold",
        "silver",
        "rust",
        "rusty",
        "corroded",
        "titanium",
        "zinc",
        "lead",
        "tin",
    ],
    "wood": [
        "wood",
        "plywood",
        "bark",
        "timber",
        "lumber",
        "oak",
        "pine",
        "birch",
        "walnut",
        "mahogany",
        "bamboo",
        "cork",
        "parquet",
        "plank",  # covers ambientcg's "Planks" category (59 records) via
        # the plural→singular fallback in _lookup_token
    ],
    "stone": [
        "stone",
        "rock",
        "marble",
        "granite",
        "slate",
        "sandstone",
        "limestone",
        "cobblestone",
        "pebble",
        "gravel",
        "cliff",
        "lava",
        "basalt",
        "quartzite",
    ],
    "fabric": [
        "fabric",
        "cloth",
        "textile",
        "leather",
        "denim",
        "silk",
        "wool",
        "cotton",
        "linen",
        "carpet",
        "rug",
        "knit",
        "woven",
        "burlap",
        "canvas",
    ],
    "plastic": [
        "plastic",
        "rubber",
        "foam",
        "nylon",
        "vinyl",
        "acrylic",
        "pvc",
        "resin",
        "silicone",
        "synthetic",
    ],
    "concrete": [
        "concrete",
        "cement",
        "asphalt",
        "stucco",
        "plaster",
        "mortar",
        "pavement",
        "sidewalk",
        "paving",
        "terrazzo",
    ],
    "ceramic": ["ceramic", "porcelain", "tile", "terracotta", "clay", "brick", "pottery"],
    "glass": ["glass", "mirror", "crystal", "window", "translucent", "transparent"],
    "organic": [
        "organic",
        "soil",
        "dirt",
        "mud",
        "sand",
        "snow",
        "ice",
        "grass",
        "moss",
        "leaf",
        "leaves",
        "bark",
        "ground",
        "terrain",
        "earth",
        "peat",
        "hay",
        "straw",
        "coral",
        "bone",
        "shell",
        "food",
    ],
}.items():
    for kw in _keywords:
        _CATEGORY_MAP[kw] = _cat


def _tokenize_category(first: str) -> list[str]:
    """Split a lower-case first segment into candidate match tokens.

    Handles dashes, underscores, spaces, and CamelCase run-ons like
    "WoodFloor" / "PaintedPlaster" / "BaseMaterials" (which arrive as
    "woodfloor" after .lower(), so we split on the original casing
    before lower-casing — see normalize_category).
    """
    # split on any non-alphanumeric delimiter
    tokens: list[str] = []
    buf = ""
    for ch in first:
        if ch.isalnum():
            buf += ch
        else:
            if buf:
                tokens.append(buf)
            buf = ""
    if buf:
        tokens.append(buf)
    return tokens


def _split_camel(token: str) -> list[str]:
    """Split a CamelCase/PascalCase token into lower-case sub-words.

    "WoodFloor" -> ["wood", "floor"]; "SciFi" -> ["sci", "fi"];
    "PaintedPlaster" -> ["painted", "plaster"]; a token with no upper
    transitions returns itself (lower-cased) as a single element.
    """
    if not token:
        return []
    parts: list[str] = []
    start = 0
    for i in range(1, len(token)):
        if token[i].isupper() and token[i - 1].islower():
            parts.append(token[start:i])
            start = i
    parts.append(token[start:])
    return [p.lower() for p in parts if p]


def _lookup_token(word: str) -> str | None:
    """Look up a single lower-case word in _CATEGORY_MAP with plural fallback.

    Tries the word as-is, then strips a trailing "s" (bricks -> brick,
    rocks -> rock, tiles -> tile, fabrics -> fabric, leaves unchanged
    if <=2 chars so we don't match empty strings or single letters).
    Deterministic: no fuzzy matching.
    """
    if not word:
        return None
    if word in _CATEGORY_MAP:
        return _CATEGORY_MAP[word]
    if len(word) > 2 and word.endswith("s"):
        stem = word[:-1]
        if stem in _CATEGORY_MAP:
            return _CATEGORY_MAP[stem]
    return None


def normalize_category(raw: str, tags: list[str] | None = None) -> str:
    """Map a freeform upstream category to one of the 10 canonical categories.

    Handles:
      - Hierarchical paths ("Metal/Steel" -> first segment)
      - Plurals ("Bricks" -> brick -> ceramic; "Rocks" -> rock -> stone)
      - Multi-word display strings ("Brick Wall", "Interior Flooring")
      - CamelCase run-ons ("WoodFloor", "PaintedPlaster", "BaseMaterials")
      - dash / underscore separators

    When the primary path doesn't find a hit and ``tags`` is supplied,
    falls back to looking up each tag against ``_CATEGORY_MAP`` (with
    the same plural handling). This rescues sources whose top-level
    category is context-only (polyhaven's ``[outdoor, natural, floor]``
    with a material token hidden in ``tags=[rocks, stones, dirt]``)
    or whose category is a multi-material bucket whose individual
    records are tagged with the actual material (ambientcg's
    ``"Planks"`` records tagged ``["wood", "planks"]``).

    Known fall-throughs that intentionally stay "other" even with tag
    fallback, because their tags are stylistic / color / format only:
      - "Liquid", "Manmade", "Human" (physicallybased)
      - "Atlas", "Decal", "Sign", "OnlyPBR" (ambientcg)
      - "SciFi", "Wallpaper" (gpuopen)
      - "Facade", "Roofing", "Interior/Exterior Flooring",
        "Base Materials" (multi-material buckets)

    Deterministic only — no fuzzy / Levenshtein matching (see mat-vis#150).
    Unmatched inputs fall through to "other".
    """
    if not raw and not tags:
        return "other"
    if raw:
        # ambientcg uses hierarchical like "Metal/Steel" — take first segment.
        # Preserve original casing so we can split CamelCase afterwards.
        first_cased = raw.split("/")[0].strip()
        first = first_cased.lower()
        # Fast path: whole-segment match (covers legacy behavior).
        hit = _lookup_token(first)
        if hit is not None:
            return hit
        # Split on delimiters, then CamelCase-split each token.
        for delim_token in _tokenize_category(first_cased):
            for sub in _split_camel(delim_token):
                hit = _lookup_token(sub)
                if hit is not None:
                    return hit
    # Tag fallback — only reached when category failed (or was empty).
    # Tags are curated by upstream authors, so exact matching against
    # _CATEGORY_MAP is sufficient; we don't split / camel-case them.
    #
    # A few metal-alias keywords ("gold", "silver", "copper", "brass",
    # "bronze", "chrome") double as English color words on stylistic
    # items (a "gold"-colored wallpaper, a "copper"-tone fabric). Those
    # would produce false-positive `metal` classifications if they
    # appear as tags. The exclusion set only applies in the tag path —
    # when the UPSTREAM CATEGORY says "Gold", we still correctly map
    # to metal because the primary branch above already returned.
    if tags:
        for tag in tags:
            if not isinstance(tag, str):
                continue
            token = tag.strip().lower()
            if token in _TAG_AMBIGUOUS_COLOR:
                continue
            hit = _lookup_token(token)
            if hit is not None:
                return hit
    return "other"


# Metal aliases that double as color words on stylistic items. Skipped
# in the tag-fallback path only — primary category matches still work.
_TAG_AMBIGUOUS_COLOR: frozenset[str] = frozenset(
    {"gold", "silver", "copper", "brass", "bronze", "chrome"}
)


# ── SPDX license normalization ──────────────────────────────────

_SPDX_MAP: dict[str, str] = {
    # upstream strings → SPDX identifiers. Extend per-source as new
    # upstream license strings appear (covered by CI schema-diff gate).
    "MIT Public Domain": "MIT",  # gpuopen (issue #168)
}


def normalize_spdx(raw: str | None) -> str:
    """Map an upstream license string to a valid SPDX identifier.

    Returns ``"NOASSERTION"`` (SPDX-valid, semantically ``unknown``)
    when the input is empty or unmapped — safer than raising mid-bake
    and schema-valid (``minLength: 1``).
    """
    if not raw or not raw.strip():
        return "NOASSERTION"
    key = raw.strip()
    if key in _SPDX_MAP:
        return _SPDX_MAP[key]
    log.warning("normalize_spdx: unknown upstream license %r → NOASSERTION", key)
    return "NOASSERTION"


# ── channel normalization (per-source) ──────────────────────────

_CHANNEL_MAPS: dict[str, dict[str, str]] = {
    "ambientcg": {
        "color": "color",
        "normalgl": "normal",
        "normaldx": "normal",
        "normal": "normal",
        "roughness": "roughness",
        "metalness": "metalness",
        "metallic": "metalness",
        "ambientocclusion": "ao",
        "displacement": "displacement",
        "emission": "emission",
        "opacity": None,  # skip
    },
    "polyhaven": {
        "diffuse": "color",
        "diff": "color",
        "col": "color",
        "nor_gl": "normal",
        "norgl": "normal",
        "nor_dx": "normal",
        "nordx": "normal",
        "rough": "roughness",
        "metal": "metalness",
        "ao": "ao",
        "disp": "displacement",
        "displacement": "displacement",
        "emission": "emission",
        "arm": None,  # packed ARM, skip
    },
    "gpuopen": {
        "basecolor": "color",
        "base_color": "color",
        "color": "color",
        "normal": "normal",
        "roughness": "roughness",
        "metallic": "metalness",
        "metalness": "metalness",
        "ambientocclusion": "ao",
        "ao": "ao",
        "displacement": "displacement",
        "height": "displacement",
        "emissive": "emission",
        "emission": "emission",
        "opacity": "opacity",
        "alpha": "opacity",
    },
}


def normalize_channel(source: str, raw_name: str) -> str | None:
    """Map a source-specific channel name to canonical. Returns None to skip."""
    cmap = _CHANNEL_MAPS.get(source, {})
    return cmap.get(raw_name.lower().replace(" ", "").replace("_", ""))


# ── data types ──────────────────────────────────────────────────


# ── Layer 1: mat_vis curated block (ADR-0011 / mat-vis#152) ────
#
# Stable, unified, cross-source-normalized fields. Every index entry
# carries exactly this shape; missing upstream values are ``None``, not
# absent, so the key set is stable. This block is the ONLY query surface
# — ``client.search()`` / ``client.index()`` look here and nowhere else.


@dataclass
class PhysicalBlock:
    """Physical dimensions / resolution, normalized to SI where applicable."""

    dimensions_m: list[float | None] | None = None  # [x, y, z?] in metres
    max_resolution_px: list[int] | None = None  # [w, h]


@dataclass
class PBRBlock:
    """Physically-based rendering scalars, mostly from physicallybased.info."""

    color_rgb: list[float] | None = None  # [r, g, b] float 0..1
    roughness: float | None = None
    metalness: float | None = None
    ior: float | None = None
    specular_f0: list[float] | None = None  # [r, g, b] float
    transmission: float | None = None
    complex_ior: list[float] | None = None  # 6-float wavelength-resolved

    # Procedural-PBR Phase 1 (#316). Library-browser facets need a
    # queryable "this is metal" signal even when the per-pixel scalar
    # can't be honestly collapsed (e.g. Bronze Oxydized authors metalness
    # as a <mix> of pure-metal and dielectric blended via a texture
    # mask). ``metalness`` itself stays the trustworthy-scalar contract;
    # these fields carry the metadata around procedural cases.
    is_conductor: bool | None = None  # True/False/None (unknown)
    metalness_mean: float | None = None  # whole-material mean estimate
    # Provenance for ``metalness``. None when the field is unset.
    #   "scalar"          — direct ``value=`` on the shader input
    #   "graph_constant"  — 1-hop nodegraph→<constant> OR fully-foldable <mix>
    #   "graph_estimate"  — fg=1.0 graph-walker estimate; metalness still None
    #   "texture"         — populated by ``apply_pbr_neutral_multiplier_conventions``
    #
    # Combined state: a material with a procedural metalness <mix> graph
    # AND a metalness texture in the bake will land as ``metalness=1.0``
    # (set by the convention helper) + ``metalness_source="graph_estimate"``
    # (preserved from the parser walker). The source string still
    # provenances the graph signal — consumers reading metalness as a
    # scalar see the convention default; consumers reading is_conductor
    # / metalness_mean see the graph estimate.
    metalness_source: str | None = None

    # Full MeshPhysicalMaterial PBR surface coverage (#340). Each field
    # extracted from <standard_surface> at bake time when authored;
    # left None when the input is at MaterialX default. Adapter layer
    # routes these to KHR_materials_specular / _volume / _dispersion /
    # _clearcoat extensions for glTF and to MeshPhysicalMaterial native
    # properties for Three.js. py-mat #100.
    # Clearcoat enabled/disabled switch (#396). Sibling of
    # ``clearcoat_roughness`` — without this, consumers can't tell when
    # clearcoat is intended (the roughness defaults to 0.1 for the whole
    # gpuopen corpus regardless of whether ``coat`` is authored).
    clearcoat: float | None = None  # <standard_surface>.coat
    clearcoat_roughness: float | None = None  # <standard_surface>.coat_roughness
    specular_intensity: float | None = None  # <standard_surface>.specular
    specular_color: list[float] | None = None  # <standard_surface>.specular_color, LINEAR RGB
    # KHR_materials_volume.thicknessFactor — only meaningful when
    # transmission > 0; baker emits None for opaque materials.
    thickness: float | None = None  # <standard_surface>.transmission_depth
    dispersion: float | None = None  # <standard_surface>.transmission_dispersion
    # Emission — Phase 3a of #405 (#406). ``emission`` is the scalar HDR
    # factor (>= 0; values > 1 emit KHR_materials_emissive_strength on
    # the glTF side / route through ``emissiveIntensity`` on Three.js).
    # ``emission_color`` is the linear RGB tint that the factor
    # multiplies. Both stay None when the material is non-emissive
    # (the gpuopen+polyhaven+ambientcg corpus authors emission=0 for all
    # 3160 entries today; schema is sticky pre-v0.7, hence the additive
    # land). Adapter contract:
    #   - Three.js: emissive = emission_color * min(emission, 1),
    #               emissiveIntensity = max(emission, 1)
    #   - glTF:     emissiveFactor = emission_color * min(emission, 1);
    #               emission > 1 ⇒ KHR_materials_emissive_strength.emissiveStrength
    emission: float | None = None  # <standard_surface>.emission
    emission_color: list[float] | None = None  # <standard_surface>.emission_color, LINEAR RGB

    # Subsurface scattering (#409). 13 gpuopen entries author
    # ``subsurface > 0`` (wax, resin, semi-translucent). Three.js
    # MeshPhysicalMaterial has no native SSS field — adapter is a
    # documented no-op there; glTF adapter emits the draft
    # ``KHR_materials_subsurface`` extension (vendor-prefixed because
    # the Khronos draft is not yet ratified). Units: ``subsurface``
    # is a unitless 0..1 mix factor; ``subsurface_color`` is linear
    # RGB; ``subsurface_radius`` is per-channel mean-free-path in
    # MaterialX scene units (typically mm). The glTF extension carries
    # these values verbatim — same per-channel length semantics, no
    # unit conversion.
    # subsurface_radius is per-channel mean-free-path in MaterialX scene units.
    subsurface: float | None = None  # <standard_surface>.subsurface
    subsurface_color: list[float] | None = None  # <standard_surface>.subsurface_color (linear RGB)
    subsurface_radius: list[float] | None = None  # <standard_surface>.subsurface_radius


def apply_pbr_neutral_multiplier_conventions(
    pbr: PBRBlock,
    textures: dict,
) -> PBRBlock:
    """glTF-MR neutral-multiplier conventions (mat-vis#290 follow-up).

    When a PBR scalar input is texture-bound (the ``pbr`` field is ``None``)
    AND the corresponding texture is present in the baked texture set,
    write the glTF-MR neutral multiplier into the substrate so
    ``renderer × texture = authored intent``. Three.js and the glTF-MR
    spec multiply the scalar factor by the sampled texel; without the
    convention the default scalar (mid-grey for ``baseColorFactor``,
    ``0`` for ``metallicFactor``) double-tints / nulls the texture.
    Mirrors MaterialX.TextureBaker post-eval semantics.
    Materializing this fact at bake time keeps every consumer (py / js
    / rust / shell adapters AND search-side ``pbr.metalness`` filters)
    inheriting the convention from the substrate — no per-language
    reimplementation, no per-adapter drift.

    Texture channel naming follows :func:`normalize_channel` output:
    ``color``, ``normal``, ``roughness``, ``metalness``, ``ao``,
    ``displacement``, ``emission``. Authored scalars (non-``None``) are
    NEVER overridden — the convention only fills genuine None gaps.

    Roughness defaults to ``1.0`` in glTF-MR which happens to match
    Three.js's renderer default, so the override is mostly a no-op for
    rendering — but the substrate index needs it for query correctness
    (``client.search(roughness>=...)`` should match materials with a
    bound roughnessMap), and emitting it explicitly is spec-aligned and
    informative.

    Modifies ``pbr`` in place AND returns it so callers can chain.
    """
    if pbr.color_rgb is None and "color" in textures:
        pbr.color_rgb = [1.0, 1.0, 1.0]
    if pbr.metalness is None and "metalness" in textures:
        pbr.metalness = 1.0
        # Provenance — only stamp when WE filled the gap. Authored
        # scalars (already set by the parser) keep their pre-existing
        # source string. #316.
        if pbr.metalness_source is None:
            pbr.metalness_source = "texture"
    if pbr.roughness is None and "roughness" in textures:
        pbr.roughness = 1.0
    return pbr


# Fields that the MTLX parser may populate. Used by
# :func:`merge_mtlx_pbr_additive` to enumerate which attributes are
# eligible for the "fill-only-if-None" merge.
#
# NOTE: ``metalness_source`` rides with ``metalness`` — when we copy a
# parser-authored metalness value over, we also copy the provenance
# string so downstream consumers (#316 library-browser facets) see the
# original source. Other "side-band" fields (``is_conductor``,
# ``metalness_mean``) follow the same rule.
_MTLX_PBR_MERGE_FIELDS: tuple[str, ...] = (
    "color_rgb",
    "roughness",
    "metalness",
    "ior",
    "transmission",
    "is_conductor",
    "metalness_mean",
    "metalness_source",
    "clearcoat_roughness",
    "specular_intensity",
    "specular_color",
    "thickness",
    "dispersion",
)


def merge_mtlx_pbr_additive(target: PBRBlock, parsed: PBRBlock) -> PBRBlock:
    """Copy MTLX-parsed fields into ``target`` ONLY where target is None.

    Per-source upstream JSON fetchers (#397) may already have authored
    base PBR fields (``color_rgb``, ``roughness``, ``metalness``, ``ior``)
    from their JSON catalog. The MTLX scalar parser is additive: it
    fills the Phase-2 fields (``clearcoat_roughness``, ``specular_*``,
    ``transmission``, ``thickness``, ``dispersion``) the JSON couldn't
    carry, and only touches the base fields if the JSON left them None.
    Mirrors the gpuopen reference path (which has no upstream JSON PBR
    today, but the same rule applies uniformly).
    Modifies ``target`` in place AND returns it so callers can chain.
    """
    for attr in _MTLX_PBR_MERGE_FIELDS:
        if getattr(target, attr) is None:
            mtlx_val = getattr(parsed, attr)
            if mtlx_val is not None:
                setattr(target, attr, mtlx_val)
    return target


@dataclass
class AttributionBlock:
    """Upstream attribution / licensing (SPDX where known)."""

    authors: list[str] = field(default_factory=list)
    license_spdx: str = "CC0-1.0"
    source_url: str = ""


@dataclass
class DatesBlock:
    """Upstream publish / update dates, normalized to ISO-8601 (YYYY-MM-DD)."""

    published: str | None = None
    updated: str | None = None


@dataclass
class MatVisBlock:
    """Layer-1 curated contract. Semver-stable across v0.6.x."""

    name: str = ""
    category: str = "other"
    tags: list[str] = field(default_factory=list)
    description: str | None = None
    physical: PhysicalBlock = field(default_factory=PhysicalBlock)
    pbr: PBRBlock = field(default_factory=PBRBlock)
    attribution: AttributionBlock = field(default_factory=AttributionBlock)
    dates: DatesBlock = field(default_factory=DatesBlock)
    upstream_id: str = ""


# ── Layer 2: upstream verbatim mirror (ADR-0011 / mat-vis#152) ─
#
# Per-source, allowlisted passthrough of the upstream JSON response.
# Explicitly NOT semver-stable: shape follows upstream and may shift.
# Stripped from ``client.index()`` / ``client.search()`` results;
# exposed only via ``client.upstream(source, material_id)``.


@dataclass
class UpstreamBlock:
    """Verbatim upstream metadata, trimmed to a per-source allowlist.

    ``source``: upstream identifier (``"ambientcg"`` / ``"polyhaven"`` / ...).
    ``schema_version``: bumped when this block's CONTRACT (keys ``source`` /
    ``fetched_at`` / ``raw``) changes, NOT when upstream adds a field.
    ``fetched_at``: ISO-8601 UTC timestamp of the bake-time fetch, e.g.
    ``"2026-04-20T16:00:00Z"``.
    ``raw``: allowlisted subset of the upstream response. ``{}`` when the
    allowlist emptied everything (preferred over ``None`` for a stable
    downstream shape). ``None`` only when the record has no upstream
    payload at all (should be rare).
    """

    source: str = ""
    schema_version: int = 1
    fetched_at: str | None = None
    raw: dict | None = None


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 ``Z`` string.

    Used as ``UpstreamBlock.fetched_at`` at bake time. Second precision is
    plenty — the field is for downstream staleness diagnosis, not
    sub-millisecond ordering.
    """
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter_upstream(raw: dict, allowlist: frozenset[str]) -> dict:
    """Return a shallow copy of ``raw`` keeping only keys in ``allowlist``.

    Shallow by design: the allowlist is a single flat set of top-level keys.
    Nested dicts (e.g. gpuopen's package metadata, ambientcg's
    ``downloadFolders``) pass through verbatim when their top-level key is
    allowed, or are dropped entirely when it isn't. Per-field pruning of
    nested structures is a future concern — Phase C's goal is a blunt,
    auditable filter, not a deep reshape.

    Missing keys are not inserted (``None`` placeholders would bloat every
    record with keys upstream has never had). Non-dict input returns ``{}``.
    """
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in allowlist}


@dataclass
class MaterialRecord:
    """Intermediate record passed between pipeline stages.

    Top-level carries bake-pipeline fields only (``id``, ``source``, tier /
    channel / hash state). Semantic data splits between two layers:

    - ``mat_vis`` (Layer 1): stable, unified, semver-stable across v0.6.x.
    - ``upstream`` (Layer 2, ADR-0011): verbatim allowlisted mirror of the
      upstream JSON. Shape follows upstream; NOT semver-stable.
    """

    id: str
    source: str
    mat_vis: MatVisBlock = field(default_factory=MatVisBlock)
    upstream: UpstreamBlock | None = None
    available_tiers: list[str] = field(default_factory=list)
    maps: list[str] = field(default_factory=list)
    texture_paths: dict[str, Path] = field(default_factory=dict)
    texture_hashes: dict[str, dict[str, str | int]] = field(default_factory=dict)
    status: str = "ok"
    needs_mtlx_bake: bool = False


# ── HTTP retry ──────────────────────────────────────────────────


def retry_request(
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    session: requests.Session | None = None,
    timeout: float = 60,
) -> requests.Response:
    """GET with exponential backoff on 429/5xx."""
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", USER_AGENT)

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = s.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = backoff_base * (2**attempt)
                log.warning("HTTP %d from %s, retry in %.1fs", resp.status_code, url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            wait = backoff_base * (2**attempt)
            log.warning("%s from %s, retry in %.1fs", exc, url, wait)
            time.sleep(wait)

    if last_exc:
        raise last_exc
    raise requests.HTTPError(f"Failed after {max_retries} retries: {url}")


# ── hashing ─────────────────────────────────────────────────────


def hash_png(path: Path) -> dict[str, str | int]:
    """Compute SHA-256 and size of a PNG file. Returns {"sha256": ..., "size": ...}."""
    data = path.read_bytes()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def hash_textures(record: MaterialRecord) -> MaterialRecord:
    """Populate texture_hashes for all channels in a record."""
    for channel, path in record.texture_paths.items():
        if channel.startswith("_"):  # skip _mtlx
            continue
        if path.exists():
            record.texture_hashes[channel] = hash_png(path)
    return record


# ── safe ZIP extraction (zip-slip + decompression bomb defense) ─


class UnsafeZipError(ValueError):
    """Raised when a ZIP trips one of the safety checks."""


def check_zip_safety(
    zf,
    *,
    output_dir: Path | None = None,
    max_total_mb: int = 500,
    max_per_file_mb: int = 200,
    max_compression_ratio: float = 100.0,
) -> None:
    """Validate a zipfile.ZipFile against zip-slip + decompression-bomb attacks.

    Does NOT extract — callers choose extraction strategy (extractall or
    selective via zf.read()). Use before any read.

    Checks:
      - Zip-slip (CWE-22): if output_dir is given, every member's
        normalized path must stay inside it.
      - Decompression bomb (CWE-409): total uncompressed, per-file,
        and compression ratio limits.

    Raises UnsafeZipError with a clear message on violation.

    Args:
        zf: an open zipfile.ZipFile
        output_dir: destination dir (only needed for zip-slip check)
        max_total_mb: reject if total uncompressed size exceeds
        max_per_file_mb: reject if any single file exceeds
        max_compression_ratio: reject if uncompressed / compressed >
    """
    max_total_bytes = max_total_mb * 1024 * 1024
    max_per_file_bytes = max_per_file_mb * 1024 * 1024
    resolved_out = Path(output_dir).resolve() if output_dir else None

    total_uncompressed = 0
    total_compressed = 0

    for member in zf.infolist():
        if resolved_out is not None:
            target = (resolved_out / member.filename).resolve()
            try:
                target.relative_to(resolved_out)
            except ValueError as e:
                raise UnsafeZipError(
                    f"zip-slip: {member.filename!r} would escape {resolved_out}"
                ) from e

        if member.file_size > max_per_file_bytes:
            raise UnsafeZipError(
                f"decompression bomb: {member.filename!r} "
                f"uncompressed size {member.file_size} > limit {max_per_file_bytes}"
            )

        total_uncompressed += member.file_size
        total_compressed += member.compress_size

        if total_uncompressed > max_total_bytes:
            raise UnsafeZipError(
                f"decompression bomb: archive total uncompressed size "
                f"{total_uncompressed} > limit {max_total_bytes}"
            )

    if total_compressed > 0:
        ratio = total_uncompressed / total_compressed
        if ratio > max_compression_ratio:
            raise UnsafeZipError(
                f"decompression bomb: compression ratio {ratio:.1f}x "
                f"exceeds {max_compression_ratio}x limit"
            )


def safe_zip_extract(
    zf,
    output_dir: Path,
    *,
    max_total_mb: int = 500,
    max_per_file_mb: int = 200,
    max_compression_ratio: float = 100.0,
) -> None:
    """Extract a zipfile.ZipFile to output_dir with zip-slip + bomb defenses.

    Convenience wrapper: calls check_zip_safety then zf.extractall.
    For selective extraction, call check_zip_safety directly then use
    zf.read() per-member.
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    check_zip_safety(
        zf,
        output_dir=output_dir,
        max_total_mb=max_total_mb,
        max_per_file_mb=max_per_file_mb,
        max_compression_ratio=max_compression_ratio,
    )
    zf.extractall(output_dir)
