"""GPUOpen MaterialX Library fetcher.

API: https://api.matlib.gpuopen.com/api/packages/?limit=100&offset=0
License: TBV (per material)
Format: ZIP with .mtlx + textures. Some materials have layered graphs.
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

log = logging.getLogger("mat-vis-baker.gpuopen")

API_BASE = "https://api.matlib.gpuopen.com/api"
PAGE_SIZE = 100
MAX_WORKERS = 10


# ── discovery ───────────────────────────────────────────────────


def discover(*, session: requests.Session | None = None) -> list[dict]:
    """Paginate the gpuopen packages API."""
    s = session or requests.Session()
    all_packages: list[dict] = []
    offset = 0

    while True:
        url = f"{API_BASE}/packages/?limit={PAGE_SIZE}&offset={offset}"
        resp = retry_request(url, session=s)
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        all_packages.extend(results)
        log.info("discovered %d packages (offset=%d)", len(all_packages), offset)
        if len(results) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    log.info("total: %d packages", len(all_packages))
    return all_packages


# ── download + extract ──────────────────────────────────────────

_IMG_RE = re.compile(r"\.(png|jpg|jpeg|tif|tiff|exr)$", re.IGNORECASE)


def _inject_mtlx_comment(mtlx_bytes: bytes, material_id: str, source_url: str) -> bytes:
    """Inject source attribution comment into mtlx XML."""
    comment = (
        f"<!-- source: {source_url} -->\n"
        f"<!-- license: TBV -->\n"
        f"<!-- material: {material_id} -->\n"
        f"<!-- fetched-by: mat-vis-baker -->\n"
    ).encode()
    text = mtlx_bytes
    if text.startswith(b"<?xml"):
        end = text.find(b"?>")
        if end >= 0:
            return text[: end + 2] + b"\n" + comment + text[end + 2 :]
    return comment + text


def _extract_from_zip(
    zip_bytes: bytes,
    material_id: str,
    output_dir: Path,
    *,
    mtlx_dir: Path | None = None,
) -> tuple[Path | None, dict[str, Path]]:
    """Extract .mtlx and texture files from a ZIP. Returns (mtlx_path, {channel: path})."""
    mat_dir = output_dir / material_id
    mat_dir.mkdir(parents=True, exist_ok=True)
    mtlx_path: Path | None = None
    textures: dict[str, Path] = {}

    source_url = f"https://matlib.gpuopen.com/main/materials/all?material={material_id}"

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Validate before any read — rejects decompression bombs. Output
        # paths are derived from normalized channel names (not member
        # names), so zip-slip is already avoided; this check covers the
        # bomb case where a malicious member has huge uncompressed size.
        check_zip_safety(zf)
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            basename = name.rsplit("/", 1)[-1].lower()

            if basename.endswith(".mtlx"):
                # Save to working dir for bake pipeline
                mtlx_path = mat_dir / "material.mtlx"
                raw = zf.read(name)
                mtlx_path.write_bytes(raw)
                # Also save attributed copy to mtlx_dir for git
                if mtlx_dir:
                    git_mtlx = mtlx_dir / "gpuopen" / material_id / "material.mtlx"
                    git_mtlx.parent.mkdir(parents=True, exist_ok=True)
                    git_mtlx.write_bytes(_inject_mtlx_comment(raw, material_id, source_url))
                continue

            if _IMG_RE.search(basename):
                # Try to extract channel from filename
                stem = basename.rsplit(".", 1)[0]
                # Common patterns: basecolor.png, *_basecolor.png, *_normal.png
                parts = re.split(r"[_\-]", stem)
                channel = None
                for part in reversed(parts):
                    channel = normalize_channel("gpuopen", part)
                    if channel:
                        break
                if channel and channel not in textures:
                    ext = basename.rsplit(".", 1)[-1]
                    out_path = mat_dir / f"{channel}.{ext}"
                    out_path.write_bytes(zf.read(name))
                    textures[channel] = out_path

    return mtlx_path, textures


# ── per-material worker ───────────────────────────────────────


def _fetch_one(
    pkg: dict,
    tier: str,
    output_dir: Path,
    mtlx_dir: Path | None,
) -> MaterialRecord:
    """Download and extract a single gpuopen package. Called from thread pool."""
    mid = pkg.get("id", "")
    name = pkg.get("label", mid)

    try:
        dl_url = pkg.get("file_url")
        if not dl_url:
            log.warning("%s: no file_url", mid)
            return MaterialRecord(
                id=mid, source="gpuopen", name=name, category="other", status="failed"
            )

        resp = retry_request(dl_url)
        mtlx_path, textures = _extract_from_zip(resp.content, mid, output_dir, mtlx_dir=mtlx_dir)

        # If no flat textures but we have mtlx, flag for baking
        needs_bake = bool(mtlx_path) and not textures

        if not textures and not mtlx_path:
            log.warning("%s: no textures or mtlx in ZIP", mid)
            return MaterialRecord(
                id=mid, source="gpuopen", name=name, category="other", status="failed"
            )

        cat = normalize_category(pkg.get("category", ""))
        tags = pkg.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]

        texture_paths = dict(textures)
        if mtlx_path:
            texture_paths["_mtlx"] = mtlx_path

        return MaterialRecord(
            id=mid,
            source="gpuopen",
            name=name,
            category=cat,
            tags=tags,
            source_url=f"https://matlib.gpuopen.com/main/materials/all?material={mid}",
            source_license="TBV",
            last_updated=pkg.get("updated_date", ""),
            available_tiers=[tier] if textures else [],
            maps=sorted(textures.keys()),
            texture_paths=texture_paths,
            needs_mtlx_bake=needs_bake,
        )

    except Exception:
        log.exception("%s: fetch failed", mid)
        return MaterialRecord(
            id=mid, source="gpuopen", name=name, category="other", status="failed"
        )


# ── main fetch ──────────────────────────────────────────────────


def fetch(
    tier: str,
    output_dir: Path,
    *,
    limit: int | None = None,
    offset: int = 0,
    session: requests.Session | None = None,
    mtlx_dir: Path | None = None,
) -> list[MaterialRecord]:
    """Fetch gpuopen materials. Layered mtlx graphs are flagged for baking."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    s = session or requests.Session()
    packages = discover(session=s)
    if offset:
        packages = packages[offset:]
    if limit:
        packages = packages[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[MaterialRecord] = []
    ok = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one, pkg, tier, output_dir, mtlx_dir): pkg for pkg in packages
        }
        for i, future in enumerate(as_completed(futures), 1):
            rec = future.result()
            records.append(rec)
            if rec.status == "failed":
                failed += 1
            elif rec.needs_mtlx_bake:
                log.info("%s: mtlx only (needs bake) [%d/%d]", rec.id, i, len(packages))
            else:
                ok += 1
                log.info("%s: ok (%d textures) [%d/%d]", rec.id, len(rec.maps), i, len(packages))

    log.info("gpuopen: %d ok, %d failed / %d total", ok, failed, len(packages))
    return records
