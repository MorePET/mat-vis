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
    check_zip_safety,
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
        url = f"{API_BASE}?type=Material&limit={PAGE_SIZE}&offset={offset}&include=downloadData"
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

# Sub-1k tiers download 1K and let the bake step resize
_TIER_ATTRS = {
    "128": "1K-PNG",
    "256": "1K-PNG",
    "512": "1K-PNG",
    "1k": "1K-PNG",
    "2k": "2K-PNG",
    "4k": "4K-PNG",
    "8k": "8K-PNG",
}


def _extract_download_url(entry: dict, tier: str) -> str | None:
    """Get the ZIP download URL for a given tier from an API entry.

    Downloads live under downloadFolders.default.downloadFiletypeCategories.zip.downloads[]
    with the tier encoded in the `attribute` field (e.g. "1K-PNG").
    """
    folders = entry.get("downloadFolders")
    if not folders:
        return None

    target_attr = _TIER_ATTRS.get(tier)
    if not target_attr:
        return None

    try:
        downloads = folders["default"]["downloadFiletypeCategories"]["zip"]["downloads"]
    except (KeyError, TypeError):
        return None

    for dl in downloads:
        if dl.get("attribute", "").upper() == target_attr:
            return dl.get("fullDownloadPath")

    return None


_CHANNEL_RE = re.compile(r"_([A-Za-z]+)\.(png|jpg)$", re.IGNORECASE)


def _inject_mtlx_comment(mtlx_bytes: bytes, material_id: str, source_url: str) -> bytes:
    """Inject source attribution comment into mtlx XML."""
    comment = (
        f"<!-- source: {source_url} -->\n"
        f"<!-- license: CC0-1.0 -->\n"
        f"<!-- material: {material_id} -->\n"
        f"<!-- fetched-by: mat-vis-baker -->\n"
    ).encode()
    # Insert after XML declaration if present, otherwise prepend
    text = mtlx_bytes
    if text.startswith(b"<?xml"):
        end = text.find(b"?>")
        if end >= 0:
            return text[: end + 2] + b"\n" + comment + text[end + 2 :]
    return comment + text


def _extract_maps_from_zip(
    zip_bytes: bytes, material_id: str, output_dir: Path, *, mtlx_dir: Path | None = None
) -> dict[str, Path]:
    """Extract PNG textures and mtlx from a ZIP, normalize channel names."""
    mat_dir = output_dir / material_id
    mat_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    source_url = f"https://ambientcg.com/a/{material_id}"

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Validate before any read — rejects decompression bombs & zip-slip.
        # Output dir isn't needed for slip check here (we derive paths from
        # normalized channel names, not member names) but we still reject
        # bombs that would OOM the runner.
        check_zip_safety(zf)
        for name in zf.namelist():
            if name.endswith("/"):
                continue

            # Extract .mtlx files
            if name.lower().endswith(".mtlx") and mtlx_dir:
                mtlx_out = mtlx_dir / "ambientcg" / material_id / "material.mtlx"
                mtlx_out.parent.mkdir(parents=True, exist_ok=True)
                raw = zf.read(name)
                mtlx_out.write_bytes(_inject_mtlx_comment(raw, material_id, source_url))
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


MAX_WORKERS = 10


def _fetch_one(entry: dict, tier: str, output_dir: Path, mtlx_dir: Path | None) -> MaterialRecord:
    """Fetch a single material. Called from thread pool."""
    mid = entry.get("assetId", "")
    name = entry.get("displayName", mid)
    try:
        dl_url = _extract_download_url(entry, tier)
        resp = retry_request(dl_url)
        textures = _extract_maps_from_zip(resp.content, mid, output_dir, mtlx_dir=mtlx_dir)

        if not textures:
            return MaterialRecord(
                id=mid, source="ambientcg", name=name, category="other", status="failed"
            )

        cat = normalize_category(entry.get("displayCategory", entry.get("category", "")))
        tags = entry.get("tags", [])
        release_date = (entry.get("releaseDate") or "")[:10]

        return MaterialRecord(
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
    except Exception:
        log.exception("%s: fetch failed", mid)
        return MaterialRecord(
            id=mid, source="ambientcg", name=name, category="other", status="failed"
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
    """Fetch ambientcg materials for a given tier. Downloads in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    s = session or requests.Session()
    entries = discover(session=s)

    entries = _filter_with_downloads(entries, tier)
    log.info("%d materials have downloads for tier %s", len(entries), tier)
    if offset:
        entries = entries[offset:]
    if limit:
        entries = entries[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)

    records: list[MaterialRecord] = []
    ok = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one, entry, tier, output_dir, mtlx_dir): entry for entry in entries
        }
        for i, future in enumerate(as_completed(futures), 1):
            rec = future.result()
            records.append(rec)
            if rec.status == "ok":
                ok += 1
            else:
                failed += 1
            if i % 100 == 0 or i == len(entries):
                log.info("progress: %d/%d fetched (%d ok, %d failed)", i, len(entries), ok, failed)

    log.info("ambientcg: %d ok, %d failed / %d total", ok, failed, len(entries))
    return records
