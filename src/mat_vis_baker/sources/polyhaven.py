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

_TIER_KEYS = {"1k": "1k", "2k": "2k", "4k": "4k", "8k": "8k"}


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


def fetch(
    tier: str,
    output_dir: Path,
    *,
    limit: int | None = None,
    session: requests.Session | None = None,
    mtlx_dir: Path | None = None,  # polyhaven has no mtlx, param for interface consistency
) -> list[MaterialRecord]:
    """Fetch polyhaven materials for a given tier."""
    s = session or requests.Session()
    assets = discover(session=s)

    slugs = list(assets.keys())
    if limit:
        slugs = slugs[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[MaterialRecord] = []
    ok = 0
    failed = 0

    for slug in slugs:
        meta = assets[slug]
        name = meta.get("name", slug)
        try:
            file_info = _fetch_files(slug, session=s)
            textures = _download_maps(file_info, tier, output_dir, slug, session=s)

            if not textures:
                log.warning("%s: no textures for tier %s", slug, tier)
                failed += 1
                records.append(
                    MaterialRecord(
                        id=slug, source="polyhaven", name=name, category="other", status="failed"
                    )
                )
                continue

            cats = meta.get("categories", {})
            cat_str = next(iter(cats.keys()), "") if cats else ""
            cat = normalize_category(cat_str)
            tags = meta.get("tags", [])

            rec = MaterialRecord(
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
            records.append(rec)
            ok += 1
            log.info("%s: ok (%d textures)", slug, len(textures))

        except Exception:
            log.exception("%s: fetch failed", slug)
            failed += 1
            records.append(
                MaterialRecord(
                    id=slug, source="polyhaven", name=name, category="other", status="failed"
                )
            )

    log.info("polyhaven: %d ok, %d failed / %d total", ok, failed, len(slugs))
    return records
