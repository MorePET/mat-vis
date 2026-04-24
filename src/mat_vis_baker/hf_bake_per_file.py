"""v0.6.0 per-file HF substrate baker (ADR-0012 / #182).

Replacement for the tar-based ``hf_bake.bake_one`` path for textured
sources. For each material × channel, writes one file directly to HF
at ``<source>/<tier>/<material_id>/<channel>.{png,ktx2}``. No tar,
no rowmap. Metadata stays in the per-source catalog JSON at the repo
root.

Three load-bearing properties:

1. **Pre-flight tree scan** — before fetching, list the target
   revision's tree under ``<source>/<tier>/`` and skip any material
   whose files are already committed. Bake resumes across crashes
   without a separate state file.

2. **Batch commits** — commit every ``batch_size`` materials (default
   50 → ~350 files/commit at 7 channels per material). Each batch
   is a durable checkpoint on HF; mid-bake crash loses at most one
   in-flight batch.

3. **`.tier_complete` sentinel** — final commit per tier writes a
   zero-byte ``<source>/<tier>/.tier_complete`` so clients can
   detect tier-level atomicity via a single-file probe rather than
   counting tree entries. Restores ADR-0007's "atomic tier" mental
   model on top of the new per-file substrate.

Rate-limit behaviour: delegate to ``huggingface_hub >= 1.2.0``, which
parses the ``RateLimit`` response header and sleeps precisely per
``Retry-After`` when HF answers 429. No baker-side backoff loop.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi

from mat_vis_baker.bake import bake_material
from mat_vis_baker.common import (
    CANONICAL_CHANNELS,
    MaterialRecord,
    hash_textures,
)
from mat_vis_baker.index_builder import build_index
from mat_vis_baker.source_tiers import is_supported, unsupported_tier_message

log = logging.getLogger("mat-vis-baker.hf_bake_per_file")

DEFAULT_BATCH_SIZE = 50


def _guard_prod_target(repo_id: str, allow_prod: bool) -> None:
    """Match .dagger/src/mat_vis_ci/main.py::_guard_prod_target — the
    baker-level guard exists so CLI callers without Dagger also get
    the safety rail."""
    if "/" in repo_id:
        _owner, name = repo_id.rsplit("/", 1)
        if name == "mat-vis-tst" or (name.endswith("-tst") and name.startswith("mat-vis")):
            return
    if allow_prod:
        return
    raise ValueError(
        f"Refusing to write to non-scratch repo {repo_id!r} without "
        "allow_prod=True. Scratch repos are named .../mat-vis-tst "
        "(or .../mat-vis-*-tst); anything else requires explicit "
        "allow_prod=True."
    )


def _get_fetcher(source: str):
    """Return the per-source ``fetch`` function."""
    if source == "ambientcg":
        from mat_vis_baker.sources.ambientcg import fetch
    elif source == "polyhaven":
        from mat_vis_baker.sources.polyhaven import fetch
    elif source == "gpuopen":
        from mat_vis_baker.sources.gpuopen import fetch
    elif source == "physicallybased":
        raise ValueError(
            "physicallybased is scalar-only; use bake_scalar_source, not bake_one_per_file"
        )
    else:
        raise NotImplementedError(f"Source {source!r} not yet implemented")
    return fetch


def _already_committed_material_ids(
    api, repo_id: str, revision: str, source: str, tier: str
) -> set[str]:
    """Return the set of ``material_id``s whose directory already
    exists under ``<source>/<tier>/`` on ``revision``.

    Conservative: a material_id is considered "already committed" only
    if AT LEAST ONE of its channel files is present. Partial uploads
    from a crashed batch are rare (uploads + commit are atomic per HF)
    but if they do happen, the baker re-uploads the missing channels
    and Xet chunk-dedup makes duplicate uploads bytes-free."""
    prefix = f"{source}/{tier}/"
    mids: set[str] = set()
    try:
        # list_repo_tree returns a lazy paginator; the 404 for a missing
        # subfolder fires on iteration, not on construction.
        for entry in api.list_repo_tree(
            repo_id=repo_id,
            repo_type="dataset",
            revision=revision,
            path_in_repo=prefix.rstrip("/"),
            recursive=True,
        ):
            path = getattr(entry, "path", None)
            if not path or not path.startswith(prefix):
                continue
            rel = path[len(prefix) :]  # noqa: E203
            parts = rel.split("/", 1)
            if len(parts) == 2 and parts[0]:
                mids.add(parts[0])
    except Exception as e:
        # Folder / revision doesn't exist yet → empty set (first bake).
        # Any other error also short-circuits to empty: preflight is an
        # optimisation, not a correctness gate — re-upload is bytes-free
        # via Xet dedup if a material happens to already exist.
        log.info("preflight tree scan empty or unavailable (%s): %s", type(e).__name__, e)
    return mids


def _channel_ext(data: bytes) -> str:
    """Inspect magic bytes; mirror TarWriter's detection so URLs stay
    predictable for clients."""
    if data.startswith(b"\xabKTX 20\xbb\r\n\x1a\n"):
        return "ktx2"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return "bin"


def _build_commit_ops_for_record(rec: MaterialRecord, source: str, tier: str) -> list:
    """Convert a baked record's texture_paths into
    ``CommitOperationAdd`` entries keyed by the canonical HF path."""
    ops = []
    for ch in CANONICAL_CHANNELS:
        p = rec.texture_paths.get(ch)
        if p is None or not p.exists():
            continue
        data = p.read_bytes()
        ext = _channel_ext(data)
        repo_path = f"{source}/{tier}/{rec.id}/{ch}.{ext}"
        ops.append(CommitOperationAdd(path_in_repo=repo_path, path_or_fileobj=data))
    return ops


def bake_one_per_file(
    source: str,
    tier: str,
    release_tag: str,
    work_dir: Path,
    *,
    repo_id: str,
    hf_token: str | None = None,
    allow_prod: bool = False,
    limit: int | None = None,
    offset: int = 0,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> dict:
    """Bake one (source, tier) into per-file HF commits.

    Returns a dict with ``ok``, ``failed``, ``skipped`` counts plus
    the last commit SHA on success. Pre-flight tree scan populates
    ``skipped``; actual bake work populates ``ok`` + ``failed``.

    Caller responsibility: batch_size and the overall wall-clock
    should stay within HF's commit-rate budget (~10-20/hr/user).
    Default batch_size=50 across ~2000 materials = 40 commits =
    over-budget for one user in one hour. Use shards if needed, or
    increase batch_size (trades commit count for per-commit wall
    time — a 10k-file commit takes ~90 s by empirical probe)."""
    if not is_supported(source, tier):
        raise ValueError(unsupported_tier_message(source, tier))
    _guard_prod_target(repo_id, allow_prod)

    work_dir.mkdir(parents=True, exist_ok=True)
    textures_dir = work_dir / "textures"
    baked_dir = work_dir / "baked"
    mtlx_dir = work_dir / "mtlx"

    api = HfApi(token=hf_token)

    # Make sure the target revision exists as a branch (the push target).
    try:
        api.list_repo_commits(repo_id=repo_id, repo_type="dataset", revision=release_tag)
    except Exception:
        log.info("creating branch %s on %s", release_tag, repo_id)
        try:
            api.create_branch(
                repo_id=repo_id,
                repo_type="dataset",
                branch=release_tag,
                revision="main",
                exist_ok=True,
            )
        except Exception as e:
            log.warning("branch create failed (may already exist): %s", e)

    # Pre-flight: which materials are already committed on this tag?
    already = _already_committed_material_ids(api, repo_id, release_tag, source, tier)
    if already:
        log.info(
            "preflight: %d materials already on %s@%s — skipping",
            len(already),
            repo_id,
            release_tag,
        )

    fetch = _get_fetcher(source)

    all_records: list[MaterialRecord] = []
    n_ok = 0
    n_failed = 0
    n_skipped_preflight = 0
    t0 = time.monotonic()
    cursor = offset
    fetched = 0
    last_commit_sha = ""

    log.info(
        "=== hf-bake-per-file %s %s → %s@%s (batch_size=%d, already_committed=%d) ===",
        source,
        tier,
        repo_id,
        release_tag,
        batch_size,
        len(already),
    )

    pending_batch: list[MaterialRecord] = []

    def _flush_batch(batch: list[MaterialRecord]) -> str:
        """Upload every baked record's files as one atomic commit, then
        free the local texture bytes for that batch. Keeping the wipe
        coupled to the flush bounds peak disk at one batch — a crash
        mid-commit just orphans LFS blobs; HF's Xet dedup makes the
        re-upload free on the next run."""
        nonlocal last_commit_sha, n_ok
        ops = []
        for rec in batch:
            ops.extend(_build_commit_ops_for_record(rec, source, tier))
        if not ops:
            return last_commit_sha
        if dry_run:
            log.info("dry-run: would commit %d files for %d materials", len(ops), len(batch))
        else:
            commit = api.create_commit(
                repo_id=repo_id,
                repo_type="dataset",
                operations=ops,
                commit_message=(
                    f"feat(data): {release_tag} — bake {source} {tier} "
                    f"batch ({len(batch)} materials, {len(ops)} files)"
                ),
                revision=release_tag,
            )
            sha = getattr(commit, "oid", "") or getattr(commit, "commit_oid", "")
            log.info(
                "batch commit %s: %d materials, %d files, sha=%s",
                source,
                len(batch),
                len(ops),
                sha[:12] if sha else "?",
            )
        # Free per-rec files now the commit is durable.
        for rec in batch:
            for p in list(rec.texture_paths.values()):
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
        return sha if not dry_run else ""

    while True:
        batch_limit = batch_size
        if limit is not None:
            remaining = limit - fetched
            if remaining <= 0:
                break
            batch_limit = min(batch_limit, remaining)

        batch = fetch(tier, textures_dir, limit=batch_limit, offset=cursor, mtlx_dir=mtlx_dir)
        if not batch:
            break

        # Skip materials already on HF (pre-flight). Track for accounting.
        to_bake = [r for r in batch if r.id not in already]
        n_skipped_preflight += len(batch) - len(to_bake)

        # Bake + hash in place.
        for rec in to_bake:
            if rec.status == "ok":
                bake_material(rec, baked_dir, mtlx_dir, tier)
                if rec.status == "ok":
                    hash_textures(rec)

        # Commit: add every successfully-baked record to the pending batch;
        # when it reaches batch_size, flush.
        for rec in to_bake:
            if rec.status != "ok":
                n_failed += 1
                continue
            pending_batch.append(rec)
            n_ok += 1
            if len(pending_batch) >= batch_size:
                last_commit_sha = _flush_batch(pending_batch)
                pending_batch.clear()
                # Local cleanup: textures / baked dirs get wiped every
                # cursor advance below, bounding peak disk.

        all_records.extend(batch)
        fetched += len(batch)
        cursor += len(batch)

        if len(batch) < batch_limit:
            break

    # Flush the final partial batch.
    if pending_batch:
        last_commit_sha = _flush_batch(pending_batch)
        pending_batch.clear()

    if n_ok == 0 and n_skipped_preflight == 0:
        return {"error": "no materials", "ok": 0, "failed": n_failed}

    # Catalog commit (only write it — same skip-if-remote logic as the
    # tar path, so re-bakes don't churn the catalog).
    catalog_path = work_dir / f"{source}.json"
    index = build_index(all_records, source)
    catalog_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")

    # Sentinel commit — marks tier as "atomically complete". Clients
    # can probe <source>/<tier>/.tier_complete in one HEAD request.
    sentinel_name = ".tier_complete"
    sentinel_path = work_dir / f"{source}-{tier}-{sentinel_name}"
    sentinel_path.write_text(release_tag + "\n")

    if dry_run:
        log.info("dry-run: would commit catalog + sentinel")
    else:
        from huggingface_hub import CommitOperationAdd

        # Catalog commit
        catalog_commit = api.create_commit(
            repo_id=repo_id,
            repo_type="dataset",
            operations=[
                CommitOperationAdd(
                    path_in_repo=f"{source}.json",
                    path_or_fileobj=str(catalog_path),
                )
            ],
            commit_message=f"feat(data): {release_tag} — {source} catalog",
            revision=release_tag,
        )
        last_commit_sha = getattr(catalog_commit, "oid", "") or last_commit_sha

        # Sentinel commit — final marker.
        sentinel_commit = api.create_commit(
            repo_id=repo_id,
            repo_type="dataset",
            operations=[
                CommitOperationAdd(
                    path_in_repo=f"{source}/{tier}/{sentinel_name}",
                    path_or_fileobj=str(sentinel_path),
                )
            ],
            commit_message=f"feat(data): {release_tag} — {source} {tier} complete",
            revision=release_tag,
        )
        last_commit_sha = getattr(sentinel_commit, "oid", "") or last_commit_sha

    log.info(
        "PERF per-file bake: %.1fs, %d ok / %d failed / %d skipped (preflight)",
        time.monotonic() - t0,
        n_ok,
        n_failed,
        n_skipped_preflight,
    )

    return {
        "commit": last_commit_sha,
        "ok": n_ok,
        "failed": n_failed,
        "skipped_preflight": n_skipped_preflight,
        "materials": len(all_records),
    }
