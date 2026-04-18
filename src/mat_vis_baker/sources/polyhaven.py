"""Polyhaven source fetcher.

API: https://api.polyhaven.com/assets?t=textures
License: CC0-1.0 (all assets)
Format: Individual PNG downloads per map per resolution (no ZIP).
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from mat_vis_baker.common import (
    MaterialRecord,
    normalize_category,
    normalize_channel,
    retry_request,
)

log = logging.getLogger("mat-vis-baker.polyhaven")

API_BASE = "https://api.polyhaven.com"

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


# ── main fetch ──────────────────────────────────────────────────


MAX_WORKERS = 8  # polyhaven does per-map downloads, so fewer workers to be polite


def _fetch_one(slug: str, meta: dict, tier: str, output_dir: Path) -> MaterialRecord:
    """Fetch a single polyhaven material. Called from thread pool."""
    name = meta.get("name", slug)
    try:
        file_info = _fetch_files(slug)
        textures = _download_maps(file_info, tier, output_dir, slug)

        if not textures:
            return MaterialRecord(
                id=slug, source="polyhaven", name=name, category="other", status="failed"
            )

        raw_cats = meta.get("categories", [])
        if isinstance(raw_cats, dict):
            cat_str = next(iter(raw_cats.keys()), "")
        elif isinstance(raw_cats, list) and raw_cats:
            cat_str = raw_cats[0]
        else:
            cat_str = ""
        cat = normalize_category(cat_str)
        tags = meta.get("tags", [])

        return MaterialRecord(
            id=slug,
            source="polyhaven",
            name=name,
            category=cat,
            tags=tags,
            source_url=f"https://polyhaven.com/a/{slug}",
            source_license="CC0-1.0",
            last_updated="",
            available_tiers=[tier],
            maps=sorted(textures.keys()),
            texture_paths=textures,
        )
    except Exception:
        log.exception("%s: fetch failed", slug)
        return MaterialRecord(
            id=slug, source="polyhaven", name=name, category="other", status="failed"
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
    assets = discover(session=s)

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
            pool.submit(_fetch_one, slug, assets[slug], tier, output_dir): slug for slug in slugs
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
