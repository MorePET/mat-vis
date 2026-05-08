"""Build ``<source>.json`` from MaterialRecords.

Each entry is a v3 envelope (ADR-0011 / mat-vis#152):

    {
      "id": ...,
      "source": ...,
      "mat_vis": { ... },          # Layer 1: stable curated contract
      "upstream": { ... },          # Layer 2: allowlisted verbatim mirror
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

Layer 2 (``upstream``) is the per-source allowlisted mirror of the
upstream JSON — added in Phase C of ADR-0011. When present, the key set
is ``{source, schema_version, fetched_at, raw}``, always all four. Pre-v3
records don't carry ``upstream`` at all; the client strips the key from
``index()`` / ``search()`` results and exposes it only via
``client.upstream(source, material_id)``.
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

    ``upstream`` is serialized only when ``rec.upstream`` is set. When
    present, the block's four keys (``source`` / ``schema_version`` /
    ``fetched_at`` / ``raw``) are always all four so the stable-key-set
    contract holds per-block.
    """
    entries: list[dict] = []
    for rec in records:
        entry: dict = {
            "id": rec.id,
            "source": source,
            "mat_vis": asdict(rec.mat_vis),
            "maps": rec.maps,
            # mat-vis#369: ``available_tiers`` is always present, always a
            # non-empty list. Sources are responsible for emitting
            # ``["scalar"]`` (not ``[]``) when a record has no texture
            # tiers — physicallybased always does, gpuopen now does for
            # the scalar-only subset (#369). Pre-#369 the key was omitted
            # when empty, forcing consumers into ``entry.get(..., [])``
            # gymnastics and diverging silently from the physicallybased
            # ``["scalar"]`` convention.
            "available_tiers": list(rec.available_tiers),
        }
        if rec.upstream is not None:
            entry["upstream"] = asdict(rec.upstream)
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
