"""Build ``<source>.json`` from MaterialRecords.

Each entry is a v3 envelope (ADR-0011 / mat-vis#152):

    {
      "id": ...,
      "source": ...,
      "mat_vis": { ... },          # Layer 1: stable curated contract
      "available_tiers": [...],
      "maps": [...],
      "texture_hashes": { ... },   # when present
      "status": "failed"            # only when the bake failed
    }

``available_tiers`` is omitted by callers that derive it from the
dataset tree at read time (ADR-0007) — the write-side list was the root
of the manifest-merge race class (#99). It's present here when the
record carries it (e.g. single-tier bakes) and omitted otherwise.

Each source's catalog is written exactly once per bake. Concurrent
bakes of different sources touch disjoint files, so no race is possible.

Layer 2 (``upstream``) is added in Phase C of ADR-0011. It is NOT part
of the v3 envelope emitted here — Phase A is the Layer-1 clean break.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from mat_vis_baker.common import MaterialRecord

log = logging.getLogger("mat-vis-baker.index")


def build_index(records: list[MaterialRecord], source: str) -> list[dict]:
    """Convert MaterialRecords to catalog JSON entries (v3 shape).

    ``mat_vis`` is serialized via ``dataclasses.asdict`` so every nested
    key is present (with ``null`` where the extractor has no data). The
    stable key set is half of the Layer-1 contract.
    """
    entries: list[dict] = []
    for rec in records:
        entry: dict = {
            "id": rec.id,
            "source": source,
            "mat_vis": asdict(rec.mat_vis),
            "maps": rec.maps,
        }
        if rec.available_tiers:
            entry["available_tiers"] = rec.available_tiers
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
