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

from huggingface_hub import CommitOperationAdd, HfApi

from mat_vis_baker.hf_bake_per_file import (
    _fetch_manifest_with_parent,
    _merge_manifest_for_source,
)
from mat_vis_baker.hf_push import push_to_hf
from mat_vis_baker.hf_retry import _create_commit_with_backoff
from mat_vis_baker.index_builder import build_index

log = logging.getLogger("mat-vis-baker.hf_bake")

DEFAULT_REPO_ID = "gerchowl/mat-vis"
# #228: count default raised to 300 to mirror per-file driver. Bytes
# becomes the binding constraint for typical-sized content.
DEFAULT_BATCH_SIZE = 300
DEFAULT_BATCH_MAX_BYTES = 700 * 1024 * 1024  # 700 MiB


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
    """Bake a scalar-only source (physicallybased): write catalog + manifest.

    The catalog is overwritten wholesale — a scalar source is baked
    as one unit. The release-manifest.json is then merged in a second
    commit using the same CAS retry pattern as ``bake_one_per_file``
    (#251) so concurrent writers against the same release tag converge
    on a manifest that lists every baked source. Pre-#239 the client
    reconstructed the manifest from a tree listing, but the per-file
    substrate broke tree pagination at scale, so the static manifest
    is now the source of truth.

    The scalar manifest entry shape mirrors the textured one
    (``source_tiers.SUPPORTED_TIERS`` declares ``physicallybased:
    {"scalar"}``)::

        {"catalog": "<source>.json",
         "tiers": {"scalar": {"complete": True}}}
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    fetch = _get_fetcher(source)
    records = fetch()
    log.info("%s: %d records", source, len(records))

    index = build_index(records, source)
    catalog_path = work_dir / f"{source}.json"
    catalog_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")

    if dry_run:
        log.info(
            "dry-run: would push catalog + manifest (%s.json + release-manifest.json) to %s@%s",
            source,
            repo_id,
            release_tag,
        )
        return {"dry_run": True, "materials": len(index)}

    # 1) Catalog commit — single file, same wholesale-overwrite shape
    # as before. push_to_hf takes care of branch creation if missing.
    catalog_sha = push_to_hf(
        repo_id=repo_id,
        files=[(catalog_path, f"{source}.json")],
        revision=release_tag,
        commit_message=f"feat(data): {release_tag} — bake {source} (scalar)",
        token=hf_token,
    )

    # 2) Manifest commit — CAS retry loop merges the (source, "scalar")
    # entry into whatever the current manifest already holds. Mirrors
    # bake_one_per_file's pattern: fetch + parent SHA, merge, commit
    # with parent_commit=<sha>, retry on 412/409. Reuses the same
    # helpers so a single contract evolves across both bakers.
    manifest_path = work_dir / "release-manifest.json"
    api = HfApi(token=hf_token)
    retry_counter: dict[str, int] = {}
    cas_retries = 0
    max_retries = 6
    manifest_sha = ""
    for attempt in range(max_retries):
        existing_manifest, parent_sha = _fetch_manifest_with_parent(api, repo_id, release_tag)
        merged = _merge_manifest_for_source(existing_manifest, source, "scalar", release_tag)
        manifest_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        try:
            manifest_commit = _create_commit_with_backoff(
                api,
                source=source,
                repo_id=repo_id,
                repo_type="dataset",
                operations=[
                    CommitOperationAdd(
                        path_in_repo="release-manifest.json",
                        path_or_fileobj=str(manifest_path),
                    ),
                ],
                commit_message=f"feat(data): {release_tag} — {source} catalog + manifest",
                revision=release_tag,
                parent_commit=parent_sha,
                _retry_counter=retry_counter,
            )
            manifest_sha = (
                getattr(manifest_commit, "oid", "")
                or getattr(manifest_commit, "commit_oid", "")
                or ""
            )
            break
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            is_cas_conflict = (
                "412" in msg
                or "precondition" in msg
                or "parent_commit" in msg
                or "409" in msg
                or "another commit operation" in msg
            )
            if is_cas_conflict:
                if attempt + 1 == max_retries:
                    log.error(
                        "scalar manifest CAS exhausted after %d retries — concurrent writers?",
                        max_retries,
                    )
                    raise
                log.warning(
                    "scalar manifest CAS retry %d/%d — concurrent writer detected",
                    attempt + 1,
                    max_retries,
                )
                cas_retries += 1
                continue
            raise

    return {
        "commit": manifest_sha or catalog_sha,
        "catalog_commit": catalog_sha,
        "manifest_commit": manifest_sha,
        "materials": len(index),
        "cas_retries": cas_retries,
        "lock_409_retries": retry_counter.get("lock_409", 0),
    }


def bake_one(
    source: str,
    tier: str,
    release_tag: str,
    work_dir: Path,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    limit: int | None = None,
    offset: int = 0,
    filter_ids: list[str] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_max_bytes: int = DEFAULT_BATCH_MAX_BYTES,
    hf_token: str | None = None,
    dry_run: bool = False,
    allow_prod: bool = False,
    storage_tier: str | None = None,
    metrics_path: Path | None = None,
    _pre_manifest_hook=None,
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
    detail.

    ``storage_tier`` (#230): test-only escape hatch — keeps ``tier``
    as the upstream-fetch label while writing under a different path
    key. Used by the #210 concurrency E2E to park N parallel writers
    at distinct (source, tier) paths under the same release tag. Not
    exposed on the CLI; production callers leave it ``None`` and get
    today's behavior unchanged."""
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
        filter_ids=filter_ids,
        batch_size=batch_size,
        batch_max_bytes=batch_max_bytes,
        dry_run=dry_run,
        storage_tier=storage_tier,
        metrics_path=metrics_path,
        _pre_manifest_hook=_pre_manifest_hook,
    )
