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
from mat_vis_baker.index_builder import build_index
from mat_vis_baker.manifest import _download_json
from mat_vis_baker.shard_utils import material_in_shard, shard_suffix
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
    """Bake a scalar-only source (physicallybased): write catalog, no tar.

    The catalog is overwritten wholesale — a scalar source is baked
    as one unit. No manifest write; clients derive the manifest from
    the dataset tree.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    fetch = _get_fetcher(source)
    records = fetch()
    log.info("%s: %d records", source, len(records))

    index = build_index(records, source)
    catalog_path = work_dir / f"{source}.json"
    catalog_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")

    if dry_run:
        log.info("dry-run: would push 1 file (%s.json) to %s@%s", source, repo_id, release_tag)
        return {"dry_run": True, "materials": len(index)}

    sha = push_to_hf(
        repo_id=repo_id,
        files=[(catalog_path, f"{source}.json")],
        revision=release_tag,
        commit_message=f"feat(data): {release_tag} — bake {source} (scalar)",
        token=hf_token,
    )
    return {"commit": sha, "materials": len(index)}


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
    shard: tuple[int, int] | None = None,
    legacy_tar: bool = False,
    allow_prod: bool = False,
) -> dict:
    """Bake one ``(source, tier)`` and commit to HF.

    Default substrate (ADR-0012): per-file — one HF file per
    (source, tier, material, channel) under
    ``<source>/<tier>/<mid>/<channel>.{png,ktx2}``. Routes to
    ``bake_one_per_file``.

    Set ``legacy_tar=True`` to invoke the original tar+rowmap path.
    Kept as a one-release-cycle escape hatch; retired by #189.

    Scalar sources (``physicallybased``) always route through
    ``bake_scalar_source`` regardless of ``legacy_tar``.

    Sharding (``shard=(index, total)``) only applies under the legacy
    tar path. Per-file substrate makes per-shard tars redundant — pre-
    flight tree scan + batch commits are the resumability primitive."""
    # Pre-flight: refuse (source, tier) combos with no upstream data.
    # Earlier code let these proceed, then produced 454-materials-failed
    # bake artifacts when every fetch returned no matching package.
    # The manifest in mat_vis_baker.source_tiers names the combos the
    # upstream actually serves; everything else routes via hf-derive.
    from mat_vis_baker.source_tiers import (
        is_supported as _tier_is_supported,
    )
    from mat_vis_baker.source_tiers import (
        unsupported_tier_message as _tier_unsupported_msg,
    )

    if not _tier_is_supported(source, tier):
        raise ValueError(_tier_unsupported_msg(source, tier))

    if source == "physicallybased":
        # Scalar sources are one unit — sharding has no benefit, and
        # per-file vs tar substrates both no-op into a single catalog
        # JSON commit.
        return bake_scalar_source(
            source,
            release_tag,
            work_dir,
            repo_id=repo_id,
            hf_token=hf_token,
            dry_run=dry_run,
        )

    if not legacy_tar:
        # ADR-0012 per-file substrate (default).
        from mat_vis_baker.hf_bake_per_file import bake_one_per_file

        if shard is not None:
            log.warning(
                "shard=%r ignored under per-file substrate (ADR-0012); "
                "use --legacy-tar to retain shard semantics for one cycle.",
                shard,
            )
        return bake_one_per_file(
            source=source,
            tier=tier,
            release_tag=release_tag,
            work_dir=work_dir,
            repo_id=repo_id,
            hf_token=hf_token,
            allow_prod=allow_prod,
            limit=limit,
            offset=offset,
            batch_size=batch_size,
            dry_run=dry_run,
        )

    from mat_vis_baker.common import TIER_TO_PX

    if tier not in TIER_TO_PX:
        raise ValueError(f"unknown tier {tier!r}")

    work_dir.mkdir(parents=True, exist_ok=True)
    textures_dir = work_dir / "textures"
    baked_dir = work_dir / "baked"
    mtlx_dir = work_dir / "mtlx"
    suffix = shard_suffix(*shard) if shard else ""
    tar_path = work_dir / f"{source}-{tier}{suffix}.tar"
    rowmap_path = work_dir / f"{source}-{tier}{suffix}-rowmap.json"
    catalog_path = work_dir / f"{source}.json"

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

            # Shard filter: mark materials outside this shard as skipped
            # so they don't count toward n_ok/n_failed. Upstream cursor
            # still advances so other shards see the same iteration order.
            if shard is not None:
                shard_index, shard_total = shard
                for rec in batch:
                    if not material_in_shard(rec.id, shard_index, shard_total):
                        rec.status = "skipped"

            for rec in batch:
                if rec.status == "ok":
                    bake_material(rec, baked_dir, mtlx_dir, tier)
                    if rec.status == "ok":
                        hash_textures(rec)

            for rec in batch:
                if rec.status == "skipped":
                    # Not this shard's material — don't count as failure.
                    continue
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

    rowmap: dict = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": tier,
        "tar_file": tar_path.name,
        "materials": rowmap_materials,
    }
    if shard is not None:
        rowmap["shard_index"] = shard[0]
        rowmap["shard_total"] = shard[1]
    rowmap_path.write_text(json.dumps(rowmap, indent=2) + "\n")

    files_to_push: list[tuple[Path, str]] = [
        (tar_path, tar_path.name),
        (rowmap_path, rowmap_path.name),
    ]
    if shard is None:
        # Race-benign: two unsharded bakes of the same source would
        # write identical content (upstream metadata doesn't vary by
        # tier), so skipping the write when a remote exists keeps
        # commits minimal.
        remote_catalog = _download_json(
            repo_id=repo_id, revision=release_tag, path=f"{source}.json", hf_token=hf_token
        )
        if remote_catalog is None:
            index = build_index(all_records, source)
            catalog_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
            files_to_push.append((catalog_path, f"{source}.json"))
            log.info("catalog: fresh write (no remote %s.json)", source)
        else:
            log.info(
                "catalog: remote %s.json exists (%d entries), skipping write",
                source,
                len(remote_catalog),
            )
    else:
        # Sharded bake: write a partial catalog alongside the shard tar
        # containing only the records this shard actually baked —
        # skipped records belong to other shards and would bloat every
        # shard's upload by ~K× if included (merge-shards dedupes
        # regardless, but the extra bytes per push are pure waste).
        # merge-shards unions all partials into <source>.json. No
        # remote-catalog check — concurrent shards publish independently.
        partial_catalog_name = f"{source}-{tier}{suffix}.catalog.json"
        partial_catalog_path = work_dir / partial_catalog_name
        this_shard_records = [r for r in all_records if r.status != "skipped"]
        index = build_index(this_shard_records, source)
        partial_catalog_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
        files_to_push.append((partial_catalog_path, partial_catalog_name))
        log.info(
            "catalog: shard partial written (%d entries); merge-shards will union",
            len(index),
        )

    log.info(
        "PERF hf-bake: %.1fs total, %d ok / %d failed, tar=%.1f MB",
        time.monotonic() - t0,
        n_ok,
        n_failed,
        tar_path.stat().st_size / 1e6,
    )

    if dry_run:
        log.info(
            "dry-run: would push %d files to %s@%s",
            len(files_to_push),
            repo_id,
            release_tag,
        )
        return {
            "dry_run": True,
            "ok": n_ok,
            "failed": n_failed,
            "tar_bytes": tar_path.stat().st_size,
        }

    shard_note = f" (shard {shard[0]}/{shard[1]})" if shard else ""
    sha = push_to_hf(
        repo_id=repo_id,
        files=files_to_push,
        revision=release_tag,
        commit_message=f"feat(data): {release_tag} — bake {source} {tier}{shard_note}",
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
