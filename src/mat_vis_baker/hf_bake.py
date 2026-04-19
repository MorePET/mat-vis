"""v0.5.0 HF-substrate baker orchestrator.

One `(source, tier)` → one atomic HF commit carrying:

- `<source>-<tier>.tar`            — all texture channels
- `<source>-<tier>-rowmap.json`    — offset/length sidecar
- `<source>.json`                  — per-source catalog (merged with existing)
- `release-manifest.json`          — v2 shape; merged with existing

Everything lands in one `HfApi.create_commit` — the substrate-level
guarantee ADR-0007 relies on to retire the release-validator and
rebuild-manifest workflows. There is no per-category partitioning;
category is a column on the catalog entry, nothing else.

Call shape:

    bake_one(
        source="polyhaven",
        tier="1k",
        release_tag="v2026.04.1-rc1",
        work_dir=Path("/tmp/bake"),
        limit=5,
    )
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

from mat_vis_baker.bake import bake_material
from mat_vis_baker.common import (
    CANONICAL_CHANNELS,
    MaterialRecord,
    hash_textures,
)
from mat_vis_baker.hf_push import push_to_hf
from mat_vis_baker.index_builder import build_index, merge_remote_index
from mat_vis_baker.manifest import merge_remote_manifest
from mat_vis_baker.tar_writer import TarWriter

log = logging.getLogger("mat-vis-baker.hf_bake")

DEFAULT_REPO_ID = "gerchowl/mat-vis"
DEFAULT_BATCH_SIZE = 50


def _get_fetcher(source: str):
    if source == "ambientcg":
        from mat_vis_baker.sources.ambientcg import fetch
    elif source == "polyhaven":
        from mat_vis_baker.sources.polyhaven import fetch
    elif source == "gpuopen":
        from mat_vis_baker.sources.gpuopen import fetch
    elif source == "physicallybased":
        from mat_vis_baker.sources.physicallybased import fetch
    else:
        raise NotImplementedError(f"Source {source!r} not yet implemented")
    return fetch


def _pack_record(tw: TarWriter, rec: MaterialRecord) -> int:
    """Copy a baked record's textures into the tar. Returns #channels packed."""
    packed = 0
    for ch in CANONICAL_CHANNELS:
        path = rec.texture_paths.get(ch)
        if path is None or not path.exists():
            continue
        tw.add_channel(rec.id, ch, path.read_bytes())
        packed += 1
    return packed


def bake_scalar_source(
    source: str,
    release_tag: str,
    work_dir: Path,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    hf_token: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Bake a scalar-only source (physicallybased): catalog + manifest, no tar."""
    work_dir.mkdir(parents=True, exist_ok=True)
    fetch = _get_fetcher(source)
    records = fetch()
    log.info("%s: %d records", source, len(records))

    index = build_index(records, source)
    merged_index = merge_remote_index(
        repo_id=repo_id,
        revision=release_tag,
        source=source,
        local_entries=index,
        hf_token=hf_token,
    )
    catalog_path = work_dir / f"{source}.json"
    catalog_path.write_text(json.dumps(merged_index, indent=2, ensure_ascii=False) + "\n")

    manifest = merge_remote_manifest(
        repo_id=repo_id,
        revision=release_tag,
        release_tag=release_tag,
        patch={
            "sources": {
                source: {
                    "catalog": f"{source}.json",
                    "materials_count": len(merged_index),
                }
            }
        },
        hf_token=hf_token,
    )
    manifest_path = work_dir / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    if dry_run:
        log.info("dry-run: would push 2 files to %s@%s", repo_id, release_tag)
        return {"dry_run": True, "materials": len(merged_index)}

    sha = push_to_hf(
        repo_id=repo_id,
        files=[
            (manifest_path, "release-manifest.json"),
            (catalog_path, f"{source}.json"),
        ],
        revision=release_tag,
        commit_message=f"feat(data): {release_tag} — bake {source} (scalar)",
        token=hf_token,
    )
    return {"commit": sha, "materials": len(merged_index)}


def bake_one(
    source: str,
    tier: str,
    release_tag: str,
    work_dir: Path,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    limit: int | None = None,
    offset: int = 0,
    batch_size: int = DEFAULT_BATCH_SIZE,
    hf_token: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Bake one ``(source, tier)`` into a tar and atomic-commit to HF.

    Streaming: fetch in batches, bake in-place, pack into the tar,
    delete the batch's raw textures, loop. Constant disk usage — the
    tar grows but raw downloads do not accumulate.

    Scalar sources route through ``bake_scalar_source`` automatically.
    """
    if source == "physicallybased":
        return bake_scalar_source(
            source,
            release_tag,
            work_dir,
            repo_id=repo_id,
            hf_token=hf_token,
            dry_run=dry_run,
        )

    from mat_vis_baker.common import TIER_TO_PX

    if tier not in TIER_TO_PX:
        raise ValueError(f"unknown tier {tier!r}")

    work_dir.mkdir(parents=True, exist_ok=True)
    textures_dir = work_dir / "textures"
    baked_dir = work_dir / "baked"
    mtlx_dir = work_dir / "mtlx"
    tar_path = work_dir / f"{source}-{tier}.tar"
    rowmap_path = work_dir / f"{source}-{tier}-rowmap.json"
    catalog_path = work_dir / f"{source}.json"
    manifest_path = work_dir / "release-manifest.json"

    fetch = _get_fetcher(source)

    all_records: list[MaterialRecord] = []
    n_ok = 0
    n_failed = 0
    t0 = time.monotonic()
    fetched = 0
    cursor = offset

    log.info(
        "=== hf-bake %s %s → %s@%s (batch=%d, limit=%s) ===",
        source,
        tier,
        repo_id,
        release_tag,
        batch_size,
        limit if limit is not None else "all",
    )

    with TarWriter(tar_path) as tw:
        while True:
            batch_limit = batch_size
            if limit is not None:
                remaining = limit - fetched
                if remaining <= 0:
                    break
                batch_limit = min(batch_limit, remaining)

            t_b = time.monotonic()
            batch = fetch(tier, textures_dir, limit=batch_limit, offset=cursor, mtlx_dir=mtlx_dir)
            if not batch:
                break

            for rec in batch:
                if rec.status == "ok":
                    bake_material(rec, baked_dir, mtlx_dir, tier)
                    if rec.status == "ok":
                        hash_textures(rec)

            for rec in batch:
                if rec.status != "ok":
                    n_failed += 1
                    continue
                packed = _pack_record(tw, rec)
                if packed == 0:
                    # bake_material left no texture files on disk — treat
                    # as failed rather than silently landing an empty row.
                    rec.status = "failed"
                    n_failed += 1
                    continue
                n_ok += 1

            all_records.extend(batch)
            fetched += len(batch)
            cursor += len(batch)

            # Free this batch's raw textures — the tar already holds
            # what we need.
            shutil.rmtree(textures_dir, ignore_errors=True)
            shutil.rmtree(baked_dir, ignore_errors=True)

            log.info(
                "batch done: %d materials (%.1fs), totals: %d ok / %d failed",
                len(batch),
                time.monotonic() - t_b,
                n_ok,
                n_failed,
            )

            if len(batch) < batch_limit:
                break

        rowmap_materials = tw.finalize()

    if n_ok == 0:
        log.error("no successful materials — aborting push")
        return {"error": "no materials", "ok": 0, "failed": n_failed}

    rowmap = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": tier,
        "tar_file": tar_path.name,
        "materials": rowmap_materials,
    }
    rowmap_path.write_text(json.dumps(rowmap, indent=2) + "\n")

    index = build_index(all_records, source)
    merged_index = merge_remote_index(
        repo_id=repo_id,
        revision=release_tag,
        source=source,
        local_entries=index,
        hf_token=hf_token,
    )
    catalog_path.write_text(json.dumps(merged_index, indent=2, ensure_ascii=False) + "\n")

    manifest = merge_remote_manifest(
        repo_id=repo_id,
        revision=release_tag,
        release_tag=release_tag,
        patch={
            "sources": {
                source: {
                    "catalog": f"{source}.json",
                    "materials_count": len(merged_index),
                    "tiers": {
                        tier: {
                            "tar": tar_path.name,
                            "rowmap": rowmap_path.name,
                        }
                    },
                }
            }
        },
        hf_token=hf_token,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    log.info(
        "PERF hf-bake: %.1fs total, %d ok / %d failed, tar=%.1f MB",
        time.monotonic() - t0,
        n_ok,
        n_failed,
        tar_path.stat().st_size / 1e6,
    )

    if dry_run:
        log.info(
            "dry-run: would push 4 files to %s@%s (manifest, catalog, tar, rowmap)",
            repo_id,
            release_tag,
        )
        return {
            "dry_run": True,
            "ok": n_ok,
            "failed": n_failed,
            "tar_bytes": tar_path.stat().st_size,
        }

    sha = push_to_hf(
        repo_id=repo_id,
        files=[
            (manifest_path, "release-manifest.json"),
            (catalog_path, f"{source}.json"),
            (tar_path, tar_path.name),
            (rowmap_path, rowmap_path.name),
        ],
        revision=release_tag,
        commit_message=f"feat(data): {release_tag} — bake {source} {tier}",
        token=hf_token,
    )
    return {
        "commit": sha,
        "ok": n_ok,
        "failed": n_failed,
        "tar_bytes": tar_path.stat().st_size,
        "tar_url": (
            f"https://huggingface.co/datasets/{repo_id}/resolve/{release_tag}/{tar_path.name}"
        ),
    }
