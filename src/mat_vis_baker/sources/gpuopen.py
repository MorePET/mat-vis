"""GPUOpen MaterialX Library fetcher.

API: https://api.matlib.gpuopen.com/api
License: MIT (©2022 AMD; verified via matlib.gpuopen.com per-material display)
Format: ZIP with .mtlx + textures. Some materials have layered graphs.

The gpuopen API is two-level:

- ``/materials/`` — 454 material records with semantic ``title`` / ``tags`` /
  ``category`` (the last two are UUIDs pointing at ``/categories/`` and
  ``/tags/`` lookup tables).
- ``/packages/`` — 2254 tier/bit-depth variants. Each material has several
  packages (``"1k 8b"``, ``"1k 16b"``, ``"2k 8b"``, ...); the package is
  what we download, but the material is what the gpuopen website links to
  and what users identify by UUID (mat-vis#142).

Prior to mat-vis#142 this fetcher iterated ``/packages/`` directly and used
each package as a material — which produced 2254 index entries with
``name="1k 8b"`` / ``category="other"`` / ``tags=[]``.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

import requests

from mat_vis_baker._mtlx_scalars import parse_standard_surface_scalars
from mat_vis_baker.common import (
    TIER_TO_PX,
    AttributionBlock,
    DatesBlock,
    MaterialRecord,
    MatVisBlock,
    PBRBlock,
    PhysicalBlock,
    UpstreamBlock,
    _filter_upstream,
    check_zip_safety,
    normalize_category,
    normalize_channel,
    normalize_spdx,
    retry_request,
    utc_now_iso,
)

log = logging.getLogger("mat-vis-baker.gpuopen")

API_BASE = "https://api.matlib.gpuopen.com/api"
PAGE_SIZE = 100
MAX_WORKERS = 10


# ── upstream allowlist (Layer 2, ADR-0011 / mat-vis#152 phase-c) ─
#
# gpuopen's ``/materials`` response carries a lot of UI-state fodder
# (``favorite``, ``notification_status``, viewer flags) and byte-heavy
# payload trees (``packages``, ``renders``, ``renders_order``,
# ``viewer_package``) we don't want to mirror. Keep only semantic
# + provenance keys. The MaterialX filename is worth keeping — it's
# the upstream anchor for the .mtlx we already publish.
#
# Fetcher-internal underscore-prefixed keys (``_category_title``,
# ``_tag_titles``, ``_packages_detail``) are never allowlisted, so
# _filter_upstream drops them automatically.
UPSTREAM_ALLOWLIST: frozenset[str] = frozenset(
    {
        "id",
        "title",
        "author",
        "license",
        "material_type",
        "status",
        "created_date",
        "updated_date",
        "published_date",
        "description",
        "mtlx_filename",
        "mtlx_material_name",
    }
)


# ── discovery ───────────────────────────────────────────────────


def _paginate(path: str, session: requests.Session) -> list[dict]:
    """Walk a DRF-paginated endpoint and collect every ``results`` entry."""
    out: list[dict] = []
    offset = 0
    while True:
        url = f"{API_BASE}{path}?limit={PAGE_SIZE}&offset={offset}"
        resp = retry_request(url, session=session)
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        out.extend(results)
        if len(results) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


# Module-level memoization cache for discover(). Rationale: same as
# the other fetchers — ``bake_one`` calls ``fetch()`` per batch and
# without this cache each call re-paginates /materials /packages
# /categories /tags (four paginated endpoints).
_DISCOVER_CACHE: list[dict] | None = None


def _reset_discover_cache() -> None:
    """Forget the cached material list; next fetch re-paginates."""
    global _DISCOVER_CACHE
    _DISCOVER_CACHE = None


def _cached_materials(session: requests.Session | None = None) -> list[dict]:
    global _DISCOVER_CACHE
    if _DISCOVER_CACHE is None:
        _DISCOVER_CACHE = discover(session=session)
    return _DISCOVER_CACHE


def discover(*, session: requests.Session | None = None) -> list[dict]:
    """Return gpuopen materials enriched with resolved category/tag titles
    and the full package dict for each variant.

    Each entry carries:

    - Everything the ``/materials/`` endpoint returns (``id``, ``title``,
      ``packages`` as UUIDs, ``category`` as UUID, ``tags`` as UUIDs, ...).
    - ``_category_title``: category title resolved via ``/categories/``.
    - ``_tag_titles``: list of tag titles resolved via ``/tags/``.
    - ``_packages_detail``: list of package dicts (``{id, label, file_url, ...}``)
      for this material, looked up from ``/packages/``.

    Underscore-prefixed keys are fetcher-internal; they are not written into
    the index JSON.
    """
    s = session or requests.Session()

    materials = _paginate("/materials/", s)
    packages = {p["id"]: p for p in _paginate("/packages/", s) if p.get("id")}
    categories = {c["id"]: c.get("title", "") for c in _paginate("/categories/", s) if c.get("id")}
    tags = {t["id"]: t.get("title", "") for t in _paginate("/tags/", s) if t.get("id")}

    log.info(
        "discovered: %d materials, %d packages, %d categories, %d tags",
        len(materials),
        len(packages),
        len(categories),
        len(tags),
    )

    for m in materials:
        m["_category_title"] = categories.get(m.get("category", ""), "")
        m["_tag_titles"] = [tags[t] for t in m.get("tags") or [] if t in tags and tags[t]]
        m["_packages_detail"] = [
            packages[pid] for pid in m.get("packages") or [] if pid in packages
        ]

    return materials


def _pick_package_for_tier(mat: dict, tier: str) -> dict | None:
    """Pick the best package for the requested tier.

    gpuopen package labels are ``"<tier> <bit-depth>"`` (``"1k 8b"``, ``"2k 16b"``, ...).
    Prefer 8-bit (smaller, matches the mat-vis PBR PNG pipeline) over 16-bit.
    """
    prefix = f"{tier} "
    candidates = [
        p for p in mat.get("_packages_detail", []) if p.get("label", "").startswith(prefix)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: ("16b" in p.get("label", ""), p.get("label", "")))
    return candidates[0]


# ── download + extract ──────────────────────────────────────────

_IMG_RE = re.compile(r"\.(png|jpg|jpeg|tif|tiff|exr)$", re.IGNORECASE)


def _inject_mtlx_comment(mtlx_bytes: bytes, material_id: str, source_url: str) -> bytes:
    """Inject source attribution comment into mtlx XML."""
    comment = (
        f"<!-- source: {source_url} -->\n"
        f"<!-- license: MIT (c)2022 AMD -->\n"
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


def _authors(mat: dict) -> list[str]:
    """gpuopen exposes a single ``author`` string (typically ``"AMD"``).

    Wrap in a list to match the ``attribution.authors`` contract. Empty
    / missing → ``[]``.
    """
    raw = mat.get("author")
    if not isinstance(raw, str) or not raw.strip():
        return []
    return [raw.strip()]


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_date(raw: object) -> str | None:
    """gpuopen dates are ISO datetimes; truncate to ``YYYY-MM-DD``.

    Guards against malformed upstream input (Phase B #152 review):
    ``"2022/08/01"[:10]`` used to pass through verbatim as
    ``"2022/08/01"``, which would fail any downstream ISO parser. We
    slice then re-validate with ``^\\d{4}-\\d{2}-\\d{2}$``; on a mismatch
    return ``None`` so the record just carries no date rather than a
    malformed one.
    """
    if not isinstance(raw, str) or not raw:
        return None
    candidate = raw[:10]
    if not _ISO_DATE_RE.match(candidate):
        return None
    return candidate


def _max_resolution_px(tier: str) -> list[int] | None:
    """Derive ``[w, h]`` in pixels from the baked tier (gpuopen packages
    are labeled ``"1k 8b"``, ``"2k 8b"``, ...; the tier prefix is the px
    we extract)."""
    px = TIER_TO_PX.get(tier)
    if px is None:
        return None
    return [px, px]


def _fetch_one(
    mat: dict,
    tier: str,
    output_dir: Path,
    mtlx_dir: Path | None,
) -> MaterialRecord:
    """Download + extract a single gpuopen material for the given tier.

    Works from the enriched material dict produced by :func:`discover`:
    picks the package whose ``label`` matches the tier, downloads it, then
    writes textures under ``output_dir / material_id``. Called from the
    thread pool in :func:`fetch`.
    """
    mid = mat.get("id", "")
    name = mat.get("title") or mid
    tags = list(mat.get("_tag_titles", []))
    category = normalize_category(mat.get("_category_title", ""), tags)
    description = mat.get("description") or None
    source_url = f"https://matlib.gpuopen.com/main/materials/all?material={mid}"
    upstream = UpstreamBlock(
        source="gpuopen",
        schema_version=1,
        fetched_at=utc_now_iso(),
        raw=_filter_upstream(mat, UPSTREAM_ALLOWLIST),
    )

    def _mat_vis(
        maps: list[str] | None = None,
        pbr: PBRBlock | None = None,
    ) -> MatVisBlock:
        return MatVisBlock(
            name=name,
            category=category,
            tags=tags,
            description=description,
            upstream_id=mid,
            physical=PhysicalBlock(max_resolution_px=_max_resolution_px(tier)),
            # PBR scalars parsed from the .mtlx <standard_surface> shader
            # at fetch time (mat-vis#290). Texture-bound inputs leave the
            # corresponding PBRBlock field as None — adapters apply their
            # own neutral defaults (e.g. baseColorFactor [1,1,1] when a
            # colorMap is bound). On the failed-fetch path pbr=None and
            # we fall back to the dataclass default (an empty PBRBlock).
            pbr=pbr if pbr is not None else PBRBlock(),
            attribution=AttributionBlock(
                authors=_authors(mat),
                # Upstream ``license`` is a freeform string (the current
                # live value is ``"MIT Public Domain"`` — not a valid
                # SPDX id). normalize_spdx maps it to ``"MIT"`` and
                # falls back to ``"NOASSERTION"`` for unknown strings
                # so a drifted upstream value doesn't fail the bake
                # (mat-vis#168).
                license_spdx=normalize_spdx(mat.get("license")),
                source_url=source_url,
            ),
            dates=DatesBlock(
                published=_iso_date(mat.get("published_date")),
                updated=_iso_date(mat.get("updated_date")),
            ),
        )

    failed = lambda: MaterialRecord(  # noqa: E731 — local shorthand
        id=mid,
        source="gpuopen",
        mat_vis=_mat_vis(pbr=None),
        upstream=upstream,
        status="failed",
    )

    pkg = _pick_package_for_tier(mat, tier)
    if pkg is None:
        log.warning("%s (%s): no package matching tier %s", mid, name, tier)
        return failed()

    try:
        dl_url = pkg.get("file_url")
        if not dl_url:
            log.warning("%s: package %s has no file_url", mid, pkg.get("id"))
            return failed()

        resp = retry_request(dl_url)
        mtlx_path, textures = _extract_from_zip(resp.content, mid, output_dir, mtlx_dir=mtlx_dir)

        # If no flat textures but we have mtlx, flag for baking
        needs_bake = bool(mtlx_path) and not textures

        if not textures and not mtlx_path:
            log.warning("%s: no textures or mtlx in ZIP", mid)
            return failed()

        # Parse <standard_surface> scalars from the .mtlx so PBRBlock is
        # populated alongside texture_paths (mat-vis#290). Texture-bound
        # inputs leave the corresponding field as None.
        parsed_pbr: PBRBlock | None = None
        if mtlx_path is not None:
            try:
                parsed_pbr = parse_standard_surface_scalars(
                    mtlx_path.read_text(encoding="utf-8"),
                    material_id=mid,
                )
            except Exception:
                # Contract: scalar parsing must NEVER break the fetch
                # path. Catch broadly (e.g. UnicodeDecodeError on non-utf8
                # mtlx, or any future parser failure) and continue without
                # the parsed PBRBlock — texture_paths still flow through.
                log.exception("%s: could not read mtlx for scalar parse", mid)

        texture_paths = dict(textures)
        if mtlx_path:
            texture_paths["_mtlx"] = mtlx_path

        return MaterialRecord(
            id=mid,
            source="gpuopen",
            mat_vis=_mat_vis(pbr=parsed_pbr),
            upstream=upstream,
            available_tiers=[tier] if textures else [],
            maps=sorted(textures.keys()),
            texture_paths=texture_paths,
            needs_mtlx_bake=needs_bake,
        )

    except Exception:
        log.exception("%s: fetch failed", mid)
        return failed()


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
    materials = _cached_materials(s)
    if offset:
        materials = materials[offset:]
    if limit:
        materials = materials[:limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[MaterialRecord] = []
    ok = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one, mat, tier, output_dir, mtlx_dir): mat for mat in materials
        }
        for i, future in enumerate(as_completed(futures), 1):
            rec = future.result()
            records.append(rec)
            if rec.status == "failed":
                failed += 1
            elif rec.needs_mtlx_bake:
                log.info("%s: mtlx only (needs bake) [%d/%d]", rec.id, i, len(materials))
            else:
                ok += 1
                log.info("%s: ok (%d textures) [%d/%d]", rec.id, len(rec.maps), i, len(materials))

    log.info("gpuopen: %d ok, %d failed / %d total", ok, failed, len(materials))
    return records
