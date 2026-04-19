"""Build index/<source>.json from MaterialRecords.

The per-source catalog is a flat list of material entries (see
``docs/specs/index-schema.json``). Every bake of ``(source, tier)``
produces the **same** catalog shape — tier-specific info lives in
``available_tiers`` on each entry, not in a separate file per tier.

When a partial bake writes the catalog, it must **merge** with any
entries already published under the same release revision on HF —
otherwise a second bake for the same source (e.g. running each tier
independently) clobbers what the first bake produced. That was the
substrate-level root of #99 on the old GH-Releases substrate; ADR-0007
fixes it by construction via the remote merge here + atomic commits.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mat_vis_baker.common import MaterialRecord

log = logging.getLogger("mat-vis-baker.index")


def build_index(records: list[MaterialRecord], source: str) -> list[dict]:
    """Convert MaterialRecords to index JSON entries (both ok and failed)."""
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
            "available_tiers": rec.available_tiers,
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


def merge_index(
    existing: list[dict] | None,
    new: list[dict],
) -> list[dict]:
    """Merge ``new`` into ``existing``, keyed by ``id``. New wins on conflict.

    ``available_tiers`` and ``maps`` are merged (union, sorted) so a
    second bake that only adds tier "2k" to a material that previously
    had "1k" leaves both tiers exposed on the entry.
    """
    by_id: dict[str, dict] = {}
    for e in existing or []:
        by_id[e["id"]] = dict(e)
    for e in new:
        mid = e["id"]
        if mid in by_id:
            merged = dict(by_id[mid])
            tiers = sorted(
                set(merged.get("available_tiers", [])) | set(e.get("available_tiers", []))
            )
            maps = sorted(set(merged.get("maps", [])) | set(e.get("maps", [])))
            merged.update(e)
            merged["available_tiers"] = tiers
            merged["maps"] = maps
            by_id[mid] = merged
        else:
            by_id[mid] = dict(e)
    return sorted(by_id.values(), key=lambda e: e["id"])


def merge_remote_index(
    *,
    repo_id: str,
    revision: str,
    source: str,
    local_entries: list[dict],
    hf_token: str | None = None,
) -> list[dict]:
    """Fetch the remote catalog for ``source`` at ``revision`` and merge.

    On first bake (file absent on remote), returns ``local_entries``
    unchanged (already sorted by ``build_index``).
    """
    from mat_vis_baker.manifest import _download_json

    remote = _download_json(
        repo_id=repo_id, revision=revision, path=f"{source}.json", hf_token=hf_token
    )
    if remote is None:
        log.info("no remote %s.json at %s — fresh catalog", source, revision)
        return local_entries
    merged = merge_index(remote, local_entries)
    log.info(
        "merged catalog %s: %d remote + %d local → %d entries",
        source,
        len(remote),
        len(local_entries),
        len(merged),
    )
    return merged


def write_index(index_data: list[dict], output_path: Path) -> Path:
    """Write index JSON to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(index_data, indent=2, ensure_ascii=False) + "\n")
    log.info("wrote %s (%d entries)", output_path, len(index_data))
    return output_path
