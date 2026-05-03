"""Polyhaven source fetcher.

API: https://api.polyhaven.com/assets?t=textures
License: CC0-1.0 (all assets)
Format: Individual PNG downloads per map per resolution (no ZIP).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from mat_vis_baker.common import (
    AttributionBlock,
    DatesBlock,
    MaterialRecord,
    MatVisBlock,
    PhysicalBlock,
    UpstreamBlock,
    _filter_upstream,
    normalize_category,
    normalize_channel,
    retry_request,
    utc_now_iso,
)

log = logging.getLogger("mat-vis-baker.polyhaven")

API_BASE = "https://api.polyhaven.com"


# ── upstream allowlist (Layer 2, ADR-0011 / mat-vis#152 phase-c) ─
#
# Polyhaven's ``/assets`` response is compact and mostly semantic already.
# Dropped: files_hash (per-file MD5 tree, large + bake-internal),
# thumbnail_url (CDN link — not indexable), sponsors (noise),
# staging (editorial flag), old_id (legacy).
UPSTREAM_ALLOWLIST: frozenset[str] = frozenset(
    {
        "name",
        "type",
        "date_published",
        "categories",
        "tags",
        "authors",
        "dimensions",
        "max_resolution",
        "description",
        "download_count",
    }
)

# Sub-1k tiers download 1k and let the bake step resize
_TIER_KEYS = {
    "128": "1k",
    "256": "1k",
    "512": "1k",
    "1k": "1k",
    "2k": "2k",
    "4k": "4k",
    "8k": "8k",
}


# ── discovery ───────────────────────────────────────────────────
#
# Module-level memoization cache for discover(). Same rationale as
# ambientcg — ``bake_one`` calls ``fetch()`` per batch; without the
# cache each call re-hits the /assets endpoint.
_DISCOVER_CACHE: dict | None = None


def _reset_discover_cache() -> None:
    """Forget the cached asset map; next fetch re-hits /assets."""
    global _DISCOVER_CACHE
    _DISCOVER_CACHE = None


def _cached_assets(session: requests.Session | None = None) -> dict:
    global _DISCOVER_CACHE
    if _DISCOVER_CACHE is None:
        _DISCOVER_CACHE = discover(session=session)
    return _DISCOVER_CACHE


def discover(*, session: requests.Session | None = None) -> dict:
    """Fetch all texture assets in a single call. Returns dict keyed by slug."""
    s = session or requests.Session()
    resp = retry_request(f"{API_BASE}/assets?t=textures", session=s)
    data = resp.json()
    log.info("discovered %d texture assets", len(data))
    return data


def _fetch_files(slug: str, *, session: requests.Session | None = None) -> dict:
    """Get per-resolution file map for a single asset."""
    s = session or requests.Session()
    resp = retry_request(f"{API_BASE}/files/{slug}", session=s)
    return resp.json()


# ── download ────────────────────────────────────────────────────


def _download_maps(
    file_info: dict,
    tier: str,
    output_dir: Path,
    material_id: str,
    *,
    session: requests.Session | None = None,
) -> dict[str, Path]:
    """Download PNG maps for a given tier. Returns {channel: path}.

    Polyhaven structure: response[MapName][tier][format] = {url, size, md5}
    e.g. response["Diffuse"]["1k"]["png"] = {"url": "...", "size": 123}
    """
    s = session or requests.Session()
    tier_key = _TIER_KEYS.get(tier)
    if not tier_key:
        return {}

    mat_dir = output_dir / material_id
    mat_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    for map_key, tier_data in file_info.items():
        # Skip non-map keys (blend, gltf, mtlx, etc.)
        if not isinstance(tier_data, dict) or tier_key not in tier_data:
            continue

        channel = normalize_channel("polyhaven", map_key)
        if channel is None:
            continue
        if channel in result:
            continue

        formats = tier_data[tier_key]
        # Prefer PNG, fall back to JPG
        fmt_data = formats.get("png") or formats.get("jpg")
        if not fmt_data or "url" not in fmt_data:
            continue

        url = fmt_data["url"]
        try:
            resp = retry_request(url, session=s)
            out_path = mat_dir / f"{channel}.png"
            out_path.write_bytes(resp.content)
            result[channel] = out_path
        except Exception:
            log.warning("%s/%s: download failed from %s", material_id, channel, url)

    return result


# ── curated-field extraction (Phase B, mat-vis#152) ─────────────


def _dimensions_m(raw: object) -> list[float | None] | None:
    """Extract ``[x, y, None]`` in metres from polyhaven's mm 2-tuple.

    polyhaven's ``dimensions`` is a 2-element list in millimetres (no
    Z-axis upstream). Convert to metres, add ``None`` for the Z slot so
    callers see a consistent ``[x, y, z]`` shape. Handles list / dict /
    missing / zero defensively; returns ``None`` when upstream has
    nothing usable.
    """
    if raw is None:
        return None
    # Some entries upstream have dict-shaped dimensions in edge cases;
    # be permissive here — take numeric-keyed values if present.
    if isinstance(raw, dict):
        raw = [raw.get("x"), raw.get("y")]
    if not isinstance(raw, list) or len(raw) < 2:
        return None

    def _one(v: object) -> float | None:
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f == 0 else f / 1000.0

    x, y = _one(raw[0]), _one(raw[1])
    if x is None and y is None:
        return None
    return [x, y, None]


def _max_resolution_px(raw: object) -> list[int] | None:
    """Normalize polyhaven's ``max_resolution`` (already px) to ``[w, h]``."""
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    try:
        return [int(raw[0]), int(raw[1])]
    except (TypeError, ValueError):
        return None


def _authors(meta: dict) -> list[str]:
    """polyhaven's ``authors`` is ``{name: role}``; we want the name list."""
    raw = meta.get("authors")
    if not isinstance(raw, dict):
        return []
    return list(raw.keys())


def _published_date(meta: dict) -> str | None:
    """polyhaven's ``date_published`` is a Unix epoch (int); return ISO date (UTC)."""
    raw = meta.get("date_published")
    if raw is None:
        return None
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ── main fetch ──────────────────────────────────────────────────


MAX_WORKERS = 8  # polyhaven does per-map downloads, so fewer workers to be polite


def _download_mtlx(
    file_info: dict,
    tier: str,
    mtlx_dir: Path,
    slug: str,
    session: requests.Session | None = None,
) -> Path | None:
    """Download polyhaven's per-tier .mtlx file, when present.

    Polyhaven publishes a tier-specific MaterialX document for most
    textures (sampled 30/30 had it). Shape from the API:

        file_info["mtlx"][<tier_key>]["mtlx"] = {"url", "md5", "size", "include"}

    The ``include`` map references the textures the .mtlx points at —
    we don't download those here (they overlap with the texture-map
    download path) and the .mtlx itself uses relative paths the client
    rewrites at export time.

    Writes to ``mtlx_dir/polyhaven/{slug}.mtlx``. ``pack-mtlx`` later
    reads the same layout (per ``mtlx_tier.pack_original_mtlx_json``)
    and bundles every source's .mtlx files into ``{source}-mtlx.json``.

    Returns the written path, or None if no MTLX is available for this
    material/tier or the download failed (logged, non-fatal — most
    callers want the texture maps to land regardless of MTLX presence).
    """
    s = session or requests.Session()
    tier_key = _TIER_KEYS.get(tier)
    if not tier_key:
        return None

    mtlx_section = file_info.get("mtlx")
    if not isinstance(mtlx_section, dict):
        return None

    tier_block = mtlx_section.get(tier_key, {})
    if not isinstance(tier_block, dict):
        return None
    inner = tier_block.get("mtlx")
    if not isinstance(inner, dict) or "url" not in inner:
        return None

    url = inner["url"]
    out_path = mtlx_dir / "polyhaven" / f"{slug}.mtlx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = retry_request(url, session=s)
        out_path.write_bytes(resp.content)
        return out_path
    except Exception:
        log.warning("%s: mtlx download failed from %s", slug, url)
        return None


def _fetch_one(
    slug: str, meta: dict, tier: str, output_dir: Path, mtlx_dir: Path | None = None
) -> MaterialRecord:
    """Fetch a single polyhaven material. Called from thread pool."""
    name = meta.get("name", slug)
    upstream = UpstreamBlock(
        source="polyhaven",
        schema_version=1,
        fetched_at=utc_now_iso(),
        raw=_filter_upstream(meta, UPSTREAM_ALLOWLIST),
    )
    try:
        file_info = _fetch_files(slug)
        textures = _download_maps(file_info, tier, output_dir, slug)

        # MTLX is best-effort and runs after textures land — mtlx_dir is
        # optional; when None we don't bother (matches the pre-#96 behavior).
        if mtlx_dir is not None:
            _download_mtlx(file_info, tier, mtlx_dir, slug)

        if not textures:
            return MaterialRecord(
                id=slug,
                source="polyhaven",
                mat_vis=MatVisBlock(
                    name=name,
                    upstream_id=slug,
                    attribution=AttributionBlock(
                        license_spdx="CC0-1.0",
                        source_url=f"https://polyhaven.com/a/{slug}",
                    ),
                ),
                upstream=upstream,
                status="failed",
            )

        raw_cats = meta.get("categories", [])
        if isinstance(raw_cats, dict):
            cat_list = list(raw_cats.keys())
        elif isinstance(raw_cats, list):
            cat_list = raw_cats
        else:
            cat_list = []
        cat_str = cat_list[0] if cat_list else ""
        tags = meta.get("tags", []) or []
        # Polyhaven's ``categories`` is a list of tokens (many of which
        # are context like "outdoor"/"floor" rather than materials). Feed
        # the whole list plus upstream tags as fallback candidates so the
        # material token (if any) gets picked up even when categories[0]
        # is a context label.
        cat = normalize_category(cat_str, [*cat_list[1:], *tags])
        description = meta.get("description") or None

        return MaterialRecord(
            id=slug,
            source="polyhaven",
            mat_vis=MatVisBlock(
                name=name,
                category=cat,
                tags=tags,
                description=description,
                upstream_id=slug,
                physical=PhysicalBlock(
                    dimensions_m=_dimensions_m(meta.get("dimensions")),
                    max_resolution_px=_max_resolution_px(meta.get("max_resolution")),
                ),
                attribution=AttributionBlock(
                    authors=_authors(meta),
                    license_spdx="CC0-1.0",
                    source_url=f"https://polyhaven.com/a/{slug}",
                ),
                dates=DatesBlock(published=_published_date(meta)),
            ),
            upstream=upstream,
            available_tiers=[tier],
            maps=sorted(textures.keys()),
            texture_paths=textures,
        )
    except Exception:
        log.exception("%s: fetch failed", slug)
        return MaterialRecord(
            id=slug,
            source="polyhaven",
            mat_vis=MatVisBlock(
                name=name,
                upstream_id=slug,
                attribution=AttributionBlock(
                    license_spdx="CC0-1.0",
                    source_url=f"https://polyhaven.com/a/{slug}",
                ),
            ),
            upstream=upstream,
            status="failed",
        )


def fetch(
    tier: str,
    output_dir: Path,
    *,
    limit: int | None = None,
    offset: int = 0,
    session: requests.Session | None = None,
    mtlx_dir: Path | None = None,
) -> list[MaterialRecord]:
    """Fetch polyhaven materials for a given tier. Downloads in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    s = session or requests.Session()
    assets = _cached_assets(s)

    slugs = list(assets.keys())
    if offset:
        slugs = slugs[offset:]
    if limit:
        slugs = slugs[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)

    records: list[MaterialRecord] = []
    ok = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one, slug, assets[slug], tier, output_dir, mtlx_dir): slug
            for slug in slugs
        }
        for i, future in enumerate(as_completed(futures), 1):
            rec = future.result()
            records.append(rec)
            if rec.status == "ok":
                ok += 1
            else:
                failed += 1
            if i % 50 == 0 or i == len(slugs):
                log.info("progress: %d/%d fetched (%d ok, %d failed)", i, len(slugs), ok, failed)

    log.info("polyhaven: %d ok, %d failed / %d total", ok, failed, len(slugs))
    return records
