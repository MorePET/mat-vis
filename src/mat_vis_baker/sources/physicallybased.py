"""Physicallybased.info fetcher — scalar properties only, no textures.

API: https://api.physicallybased.info/materials
License: CC0-1.0
Format: JSON array of scalar material properties (IOR, color, roughness, etc.)
No textures, no parquet — index JSON only.
"""

from __future__ import annotations

import logging

import requests

from mat_vis_baker.common import (
    AttributionBlock,
    MaterialRecord,
    MatVisBlock,
    PBRBlock,
    UpstreamBlock,
    _filter_upstream,
    normalize_category,
    retry_request,
    utc_now_iso,
)

log = logging.getLogger("mat-vis-baker.physicallybased")

API_URL = "https://api.physicallybased.info/materials"


# ── upstream allowlist (Layer 2, ADR-0011 / mat-vis#152 phase-c) ─
#
# physicallybased.info is already a scientific dataset: almost every key
# is load-bearing (scalar PBR, dispersion, complex IOR, subsurface,
# acoustic). We mirror the lot — the allowlist exists to make NEW upstream
# keys show up in the schema-diff gate, not to prune good data.
UPSTREAM_ALLOWLIST: frozenset[str] = frozenset(
    {
        "name",
        "category",
        "group",
        "description",
        "tags",
        "reference",
        "sources",
        "color",
        "roughness",
        "metalness",
        "ior",
        "specularColor",
        "complexIor",
        "transmission",
        "transmissionDepth",
        "transmissionDispersion",
        "density",
        "densityRange",
        "viscosity",
        "surfaceTension",
        "subsurfaceRadius",
        "thinFilmIor",
        "thinFilmThickness",
        "acousticAbsorption",
    }
)


def _color_rgb(rgb: object) -> list[float] | None:
    """Extract a ``[r, g, b]`` float triple from upstream ``color`` if valid."""
    if not isinstance(rgb, list) or len(rgb) < 3:
        return None
    try:
        return [float(rgb[0]), float(rgb[1]), float(rgb[2])]
    except (TypeError, ValueError):
        return None


def _specular_f0(raw: object) -> list[float] | None:
    """Extract ``[r, g, b]`` from upstream ``specularColor`` if valid.

    physicallybased.info exposes ``specularColor`` as a float triple on
    dielectrics (absent on most metals — ``None`` passthrough). Same shape
    contract as ``_color_rgb``; decoupled so future per-field validation
    can diverge without churn.
    """
    if not isinstance(raw, list) or len(raw) < 3:
        return None
    try:
        return [float(raw[0]), float(raw[1]), float(raw[2])]
    except (TypeError, ValueError):
        return None


def _transmission(raw: object) -> float | None:
    """Upstream ``transmission`` is a single float (0..1) or missing."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _complex_ior(raw: object) -> list[float] | None:
    """Passthrough ``complexIor`` verbatim as a list of floats.

    physicallybased.info publishes 6-element wavelength-resolved complex
    IOR triples for metals (``[n_r, k_r, n_g, k_g, n_b, k_b]``). Some
    entries upstream diverge in length; we coerce to list and keep all
    data in Phase B — stripping / length validation is Phase C's
    allowlist + schema-diff gate.
    """
    if not isinstance(raw, list) or not raw:
        return None
    out: list[float] = []
    for v in raw:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            return None
    return out


def _normalize_tags(raw: object) -> list[str]:
    """Normalize upstream tags to lowercase, stripped, de-duplicated strings.

    physicallybased.info's ``tags`` is a list of strings, but a handful of
    entries contain a single empty string (``[""]``) and some values carry
    stray whitespace or mixed case. Drop empties, collapse casing, and
    preserve first-seen order so the output is stable across bakes.
    """
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        if not isinstance(t, str):
            continue
        norm = t.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def fetch(*, session: requests.Session | None = None) -> list[MaterialRecord]:
    """Fetch all physicallybased materials (scalar only, no tier needed)."""
    s = session or requests.Session()
    resp = retry_request(API_URL, session=s)
    materials = resp.json()
    log.info("fetched %d materials", len(materials))

    # One fetched_at for the whole batch — physicallybased is a single
    # API call, so every record legitimately shares the same timestamp.
    fetched_at = utc_now_iso()

    records: list[MaterialRecord] = []
    for mat in materials:
        name = mat.get("name", "")
        raw_cat = mat.get("category", "")
        if isinstance(raw_cat, list):
            raw_cat = raw_cat[0] if raw_cat else ""
        pb_tags = _normalize_tags(mat.get("tags"))
        cat = normalize_category(raw_cat, pb_tags)
        mid = name.lower().replace(" ", "_")

        rec = MaterialRecord(
            id=mid,
            source="physicallybased",
            mat_vis=MatVisBlock(
                name=name,
                category=cat,
                tags=_normalize_tags(mat.get("tags")),
                description=mat.get("description") or None,
                upstream_id=mid,
                pbr=PBRBlock(
                    color_rgb=_color_rgb(mat.get("color")),
                    roughness=mat.get("roughness"),
                    metalness=mat.get("metalness"),
                    ior=mat.get("ior"),
                    specular_f0=_specular_f0(mat.get("specularColor")),
                    transmission=_transmission(mat.get("transmission")),
                    complex_ior=_complex_ior(mat.get("complexIor")),
                ),
                attribution=AttributionBlock(
                    license_spdx="CC0-1.0",
                    source_url="https://physicallybased.info",
                ),
            ),
            upstream=UpstreamBlock(
                source="physicallybased",
                schema_version=1,
                fetched_at=fetched_at,
                raw=_filter_upstream(mat, UPSTREAM_ALLOWLIST),
            ),
            # mat-vis#331: scalar-only sources advertise the "scalar"
            # sentinel tier (the manifest already declares
            # tiers.scalar.complete=True for physicallybased). Was [];
            # client.materials("physicallybased", "scalar") returned
            # empty for every dispatcher because `tier in []` matches
            # nothing — see Bernhard's #313 cascade.
            available_tiers=["scalar"],
            maps=[],
        )
        records.append(rec)

    log.info("physicallybased: %d records", len(records))
    return records
