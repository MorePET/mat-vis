"""v0.6.0 HF-substrate baker orchestrator (ADR-0012).

One ``(source, tier)`` → one or more atomic HF commits laying out
per-file artifacts:

- ``<source>/<tier>/<material_id>/<channel>.{png,ktx2}`` — texture files
- ``<source>/<tier>/.tier_complete``                    — atomicity sentinel
- ``<source>.json``                                     — per-source catalog (v3)

Per-file substrate replaced the legacy tar+rowmap path in #182
(ADR-0012) and #184 made it the default; #189 deleted the tar code
entirely. The orchestration here is a thin pre-flight + routing
shell — heavy lifting lives in ``hf_bake_per_file``.

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
from pathlib import Path

from mat_vis_baker.hf_push import push_to_hf
from mat_vis_baker.index_builder import build_index

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
    allow_prod: bool = False,
) -> dict:
    """Bake one ``(source, tier)`` and commit to HF.

    Per-file substrate (ADR-0012): one HF file per
    ``(source, tier, material, channel)`` under
    ``<source>/<tier>/<mid>/<channel>.{png,ktx2}``. Routes to
    ``bake_one_per_file``.

    Scalar sources (``physicallybased``) route through
    ``bake_scalar_source`` — they have no textures, so the per-file
    layout is a no-op; the catalog JSON is the only artifact.

    Pre-flight tree scan + batch commits (size ``batch_size``) make
    bakes resumable across crashes — see ``hf_bake_per_file`` for
    detail."""
    # Pre-flight: refuse (source, tier) combos with no upstream data.
    # Earlier code let these proceed, then produced 454-materials-failed
    # bake artifacts when every fetch returned no matching package.
    # The manifest in mat_vis_baker.source_tiers names the combos the
    # upstream actually serves.
    from mat_vis_baker.source_tiers import (
        is_supported as _tier_is_supported,
    )
    from mat_vis_baker.source_tiers import (
        unsupported_tier_message as _tier_unsupported_msg,
    )

    if not _tier_is_supported(source, tier):
        raise ValueError(_tier_unsupported_msg(source, tier))

    if source == "physicallybased":
        return bake_scalar_source(
            source,
            release_tag,
            work_dir,
            repo_id=repo_id,
            hf_token=hf_token,
            dry_run=dry_run,
        )

    from mat_vis_baker.hf_bake_per_file import bake_one_per_file

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
