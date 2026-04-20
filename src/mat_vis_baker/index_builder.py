"""Build ``<source>.json`` from MaterialRecords.

Each material entry carries upstream-derived metadata — id, name,
category, tags, scalars, source_url, source_license, maps. It does
**not** carry ``available_tiers``: that's derived by clients from the
set of tier-specific tars present in the dataset tree. Having a
write-side "tier list" field was the root of the manifest-merge race
class (#99) and is now a pure read-side computation.

Each source's catalog is written exactly once — on the initial bake
of the source. Derive pipelines do not touch it. Concurrent bakes of
different sources write disjoint files, so no race is possible.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mat_vis_baker.common import MaterialRecord

log = logging.getLogger("mat-vis-baker.index")


def build_index(records: list[MaterialRecord], source: str) -> list[dict]:
    """Convert MaterialRecords to catalog JSON entries.

    ``available_tiers`` is deliberately omitted — clients compute it
    from the dataset tree at read time.
    """
    entries = []
    for rec in records:
        entry: dict = {
            "id": rec.id,
            "source": source,
            "name": rec.name,
            "category": rec.category,
            "tags": rec.tags,
            "source_url": rec.source_url,
            "source_license": rec.source_license,
            "maps": rec.maps,
            "last_updated": rec.last_updated,
        }
        if rec.color_hex is not None:
            entry["color_hex"] = rec.color_hex
        if rec.roughness is not None:
            entry["roughness"] = rec.roughness
        if rec.metalness is not None:
            entry["metalness"] = rec.metalness
        if rec.ior is not None:
            entry["ior"] = rec.ior
        if rec.source_mtlx_url is not None:
            entry["source_mtlx_url"] = rec.source_mtlx_url
        if rec.texture_hashes:
            entry["texture_hashes"] = rec.texture_hashes
        if rec.status == "failed":
            entry["status"] = "failed"

        entries.append(entry)

    entries.sort(key=lambda e: e["id"])
    return entries


def write_index(index_data: list[dict], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(index_data, indent=2, ensure_ascii=False) + "\n")
    log.info("wrote %s (%d entries)", output_path, len(index_data))
    return output_path
