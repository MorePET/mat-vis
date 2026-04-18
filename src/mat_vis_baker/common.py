"""Shared types, constants, and utilities for the mat-vis baker."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
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
