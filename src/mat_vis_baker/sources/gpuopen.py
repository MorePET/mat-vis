"""GPUOpen MaterialX Library fetcher.

API: https://api.matlib.gpuopen.com/api/packages?limit=100&offset=0
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
    normalize_category,
    normalize_channel,
    retry_request,
)

log = logging.getLogger("mat-vis-baker.gpuopen")

API_BASE = "https://api.matlib.gpuopen.com/api"
PAGE_SIZE = 100


# ── discovery ───────────────────────────────────────────────────


def discover(*, session: requests.Session | None = None) -> list[dict]:
    """Paginate the gpuopen packages API."""
    s = session or requests.Session()
    all_packages: list[dict] = []
    offset = 0

    while True:
        url = f"{API_BASE}/packages?limit={PAGE_SIZE}&offset={offset}"
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


def _fetch_package_detail(package_id: str, *, session: requests.Session | None = None) -> dict:
    """Get detailed package info including download URL."""
    s = session or requests.Session()
    resp = retry_request(f"{API_BASE}/packages/{package_id}", session=s)
    return resp.json()


# ── download + extract ──────────────────────────────────────────

_IMG_RE = re.compile(r"\.(png|jpg|jpeg|tif|tiff|exr)$", re.IGNORECASE)


def _extract_from_zip(
    zip_bytes: bytes,
    material_id: str,
    output_dir: Path,
) -> tuple[Path | None, dict[str, Path]]:
    """Extract .mtlx and texture files from a ZIP. Returns (mtlx_path, {channel: path})."""
    mat_dir = output_dir / material_id
    mat_dir.mkdir(parents=True, exist_ok=True)
    mtlx_path: Path | None = None
    textures: dict[str, Path] = {}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            basename = name.rsplit("/", 1)[-1].lower()

            if basename.endswith(".mtlx"):
                mtlx_path = mat_dir / "material.mtlx"
                mtlx_path.write_bytes(zf.read(name))
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


# ── main fetch ──────────────────────────────────────────────────


def fetch(
    tier: str,
    output_dir: Path,
    *,
    limit: int | None = None,
    session: requests.Session | None = None,
) -> list[MaterialRecord]:
    """Fetch gpuopen materials. Layered mtlx graphs are flagged for baking."""
    s = session or requests.Session()
    packages = discover(session=s)
    if limit:
        packages = packages[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[MaterialRecord] = []
    ok = 0
    failed = 0

    for pkg in packages:
        mid = pkg.get("id", "")
        name = pkg.get("title", mid)
        try:
            detail = _fetch_package_detail(mid, session=s)
            dl_url = detail.get("downloadUrl") or detail.get("download_url")
            if not dl_url:
                log.warning("%s: no download URL", mid)
                failed += 1
                records.append(
                    MaterialRecord(
                        id=mid, source="gpuopen", name=name, category="other", status="failed"
                    )
                )
                continue

            resp = retry_request(dl_url, session=s)
            mtlx_path, textures = _extract_from_zip(resp.content, mid, output_dir)

            # If no flat textures but we have mtlx, flag for baking
            needs_bake = bool(mtlx_path) and not textures

            if not textures and not mtlx_path:
                log.warning("%s: no textures or mtlx in ZIP", mid)
                failed += 1
                records.append(
                    MaterialRecord(
                        id=mid, source="gpuopen", name=name, category="other", status="failed"
                    )
                )
                continue

            cat = normalize_category(pkg.get("category", ""))
            tags = pkg.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            texture_paths = dict(textures)
            if mtlx_path:
                texture_paths["_mtlx"] = mtlx_path

            rec = MaterialRecord(
                id=mid,
                source="gpuopen",
                name=name,
                category=cat,
                tags=tags,
                source_url=f"https://matlib.gpuopen.com/main/materials/all?material={mid}",
                source_license="TBV",
                last_updated="",
                available_tiers=[tier] if textures else [],
                maps=sorted(textures.keys()),
                texture_paths=texture_paths,
                needs_mtlx_bake=needs_bake,
            )
            records.append(rec)
            if needs_bake:
                log.info("%s: mtlx only (needs bake)", mid)
            else:
                ok += 1
                log.info("%s: ok (%d textures)", mid, len(textures))

        except Exception:
            log.exception("%s: fetch failed", mid)
            failed += 1
            records.append(
                MaterialRecord(
                    id=mid, source="gpuopen", name=name, category="other", status="failed"
                )
            )

    log.info("gpuopen: %d ok, %d failed / %d total", ok, failed, len(packages))
    return records
