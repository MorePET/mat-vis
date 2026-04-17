"""AmbientCG source fetcher.

API: https://ambientcg.com/api/v2/full_json?type=Material&limit=100&offset=0
License: CC0-1.0 (all materials)
Format: ZIP per resolution containing flat ONGs — no mtlx baking needed.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

import requests

from mat_vis_baker.common import (
    MaterialRecord,
    normalize_category,
    normalize_channel,
    retry_request,
)

log = logging.getLogger("mat-vis-baker.ambientcg")

API_BASE = "https://ambientcg.com/api/v2/full_json"
PAGE_SIZE = 100


# ── discovery ───────────────────────────────────────────────────


def discover(*, session: requests.Session | None = None) -> list[dict]:
    """Paginate the ambientcg API and return all material entries."""
    s = session or requests.Session()
    all_assets: list[dict] = []
    offset = 0

    while True:
        url = f"{API_BASE}?type=Material&limit={PAGE_SIZE}&offset={offset}"
        resp = retry_request(url, session=s)
        data = resp.json()
        assets = data.get("foundAssets", [])
        if not assets:
            break
        all_assets.extend(assets)
        log.info("discovered %d materials (offset=%d)", len(all_assets), offset)
        if len(assets) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    log.info("total: %d materials", len(all_assets))
    return all_assets


# ── download + extract ──────────────────────────────────────────

_TIER_KEYS = {"1k": "1k-png", "2k": "2k-png", "4k": "4k-png", "8k": "8k-png"}


def _extract_download_url(entry: dict, tier: str) -> str | None:
    """Get the ZIP download URL for a given tier from an API entry."""
    folders = entry.get("downloadFolders")
    if not folders:
        return None

    tier_key = _TIER_KEYS.get(tier)
    if not tier_key:
        return None

    # try exact key, then case-insensitive
    folder = folders.get(tier_key)
    if not folder:
        for k, v in folders.items():
            if k.lower() == tier_key.lower():
                folder = v
                break
    if not folder:
        return None

    try:
        cats = folder["downloadFiletypeCategories"]
        zips = cats["zip"]["downloads"]
        return zips[0]["fullDownloadPath"]
    except (KeyError, IndexError):
        return None


_CHANNEL_RE = re.compile(r"_([A-Za-z]+)\.(png|jpg)$", re.IGNORECASE)


def _extract_maps_from_zip(zip_bytes: bytes, material_id: str, output_dir: Path) -> dict[str, Path]:
    """Extract PNG textures from a ZIP, normalize channel names."""
    mat_dir = output_dir / material_id
    mat_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            m = _CHANNEL_RE.search(name)
            if not m:
                continue
            raw_channel = m.group(1)
            channel = normalize_channel("ambientcg", raw_channel)
            if channel is None:
                continue
            if channel in result:
                continue

            out_path = mat_dir / f"{channel}.png"
            out_path.write_bytes(zf.read(name))
            result[channel] = out_path

    return result


# ── main fetch ──────────────────────────────────────────────────


def _filter_with_downloads(entries: list[dict], tier: str) -> list[dict]:
    """Filter to entries that have a download URL for the given tier."""
    return [e for e in entries if _extract_download_url(e, tier) is not None]


def fetch(
    tier: str,
    output_dir: Path,
    *,
    limit: int | None = None,
    session: requests.Session | None = None,
) -> list[MaterialRecord]:
    """Fetch ambientcg materials for a given tier."""
    s = session or requests.Session()
    entries = discover(session=s)

    # Filter to entries with downloads for this tier, then apply limit
    entries = _filter_with_downloads(entries, tier)
    log.info("%d materials have downloads for tier %s", len(entries), tier)
    if limit:
        entries = entries[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[MaterialRecord] = []
    ok = 0
    failed = 0

    for entry in entries:
        mid = entry.get("assetId", "")
        name = entry.get("displayName", mid)
        try:
            dl_url = _extract_download_url(entry, tier)
            resp = retry_request(dl_url, session=s)
            textures = _extract_maps_from_zip(resp.content, mid, output_dir)

            if not textures:
                log.warning("%s: no textures in ZIP", mid)
                failed += 1
                records.append(
                    MaterialRecord(
                        id=mid, source="ambientcg", name=name, category="other", status="failed"
                    )
                )
                continue

            cat = normalize_category(entry.get("displayCategory", entry.get("category", "")))
            tags = entry.get("tags", [])
            release_date = (entry.get("releaseDate") or "")[:10]

            rec = MaterialRecord(
                id=mid,
                source="ambientcg",
                name=name,
                category=cat,
                tags=tags,
                source_url=f"https://ambientcg.com/a/{mid}",
                source_license="CC0-1.0",
                last_updated=release_date,
                available_tiers=[tier],
                maps=sorted(textures.keys()),
                texture_paths=textures,
            )
            records.append(rec)
            ok += 1
            log.info("%s: ok (%d textures)", mid, len(textures))

        except Exception:
            log.exception("%s: fetch failed", mid)
            failed += 1
            records.append(
                MaterialRecord(
                    id=mid, source="ambientcg", name=name, category="other", status="failed"
                )
            )

    log.info("ambientcg: %d ok, %d failed / %d total", ok, failed, len(entries))
    return records
