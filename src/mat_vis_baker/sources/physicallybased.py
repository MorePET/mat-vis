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
    MaterialRecord,
    normalize_category,
    retry_request,
)

log = logging.getLogger("mat-vis-baker.physicallybased")

API_URL = "https://api.physicallybased.info/materials"


def _rgb_to_hex(rgb: list[float] | None) -> str | None:
    """Convert [r, g, b] floats (0-1) to #RRGGBB hex."""
    if not rgb or len(rgb) < 3:
        return None
    r, g, b = rgb[:3]
    return "#{:02X}{:02X}{:02X}".format(
        int(round(r * 255)), int(round(g * 255)), int(round(b * 255))
    )


def fetch(*, session: requests.Session | None = None) -> list[MaterialRecord]:
    """Fetch all physicallybased materials (scalar only, no tier needed)."""
    s = session or requests.Session()
    resp = retry_request(API_URL, session=s)
    materials = resp.json()
    log.info("fetched %d materials", len(materials))

    records: list[MaterialRecord] = []
    for mat in materials:
        name = mat.get("name", "")
        raw_cat = mat.get("category", "")
        if isinstance(raw_cat, list):
            raw_cat = raw_cat[0] if raw_cat else ""
        cat = normalize_category(raw_cat)
        color_hex = _rgb_to_hex(mat.get("color"))

        rec = MaterialRecord(
            id=name.lower().replace(" ", "_"),
            source="physicallybased",
            name=name,
            category=cat,
            source_url="https://physicallybased.info",
            source_license="CC0-1.0",
            color_hex=color_hex,
            roughness=mat.get("roughness"),
            metalness=mat.get("metalness"),
            ior=mat.get("ior"),
            available_tiers=[],
            maps=[],
        )
        records.append(rec)

    log.info("physicallybased: %d records", len(records))
    return records
