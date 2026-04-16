"""Shared types, constants, and utilities for the mat-vis baker."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger("mat-vis-baker")

# ── canonical enums (source of truth: docs/specs/*.json) ────────

CANONICAL_CATEGORIES = frozenset(
    {
        "metal",
        "wood",
        "stone",
        "fabric",
        "plastic",
        "concrete",
        "ceramic",
        "glass",
        "organic",
        "other",
    }
)

CANONICAL_CHANNELS = ["color", "normal", "roughness", "metalness", "ao", "displacement", "emission"]

VALID_TIERS = ["1k", "2k", "4k", "8k"]

TIER_TO_PX = {"1k": 1024, "2k": 2048, "4k": 4096, "8k": 8192}

BAKER_VERSION = "0.1.0"

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


def normalize_category(raw: str) -> str:
    """Map a freeform upstream category to one of the 10 canonical categories."""
    if not raw:
        return "other"
    # ambientcg uses hierarchical like "Metal/Steel" — take first segment
    first = raw.split("/")[0].strip().lower()
    if first in _CATEGORY_MAP:
        return _CATEGORY_MAP[first]
    # try individual words
    for word in first.split():
        if word in _CATEGORY_MAP:
            return _CATEGORY_MAP[word]
    return "other"


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
    },
}


def normalize_channel(source: str, raw_name: str) -> str | None:
    """Map a source-specific channel name to canonical. Returns None to skip."""
    cmap = _CHANNEL_MAPS.get(source, {})
    return cmap.get(raw_name.lower().replace(" ", "").replace("_", ""))


# ── data types ──────────────────────────────────────────────────


@dataclass
class MaterialRecord:
    """Intermediate record passed between pipeline stages."""

    id: str
    source: str
    name: str
    category: str
    tags: list[str] = field(default_factory=list)
    source_url: str = ""
    source_license: str = "CC0-1.0"
    source_mtlx_url: str | None = None
    color_hex: str | None = None
    roughness: float | None = None
    metalness: float | None = None
    ior: float | None = None
    last_updated: str = ""
    available_tiers: list[str] = field(default_factory=list)
    maps: list[str] = field(default_factory=list)
    texture_paths: dict[str, Path] = field(default_factory=dict)
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
