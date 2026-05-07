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
from mat_vis_baker.hf_retry import _create_commit_with_backoff
from mat_vis_baker.index_builder import build_index
from mat_vis_baker.per_file_metrics import record_batch
from mat_vis_baker.progress import ProgressTracker, emit_bake_plan
from mat_vis_baker.source_tiers import is_supported, unsupported_tier_message

log = logging.getLogger("mat-vis-baker.hf_bake_per_file")

# #228: bytes-aware batching. HF caps per-commit at 1 GiB and 25k files,
# rate-limits at 128 commits/hr/repo. Old default of 50 materials ran
# 4-70× under the per-commit caps, burning the rate budget on
# mostly-empty commits. Now: flush on whichever bound trips first
# (count >= batch_size OR pending_bytes >= batch_max_bytes), so commits
# self-tune to per-commit headroom and we use ~5× fewer commits. The
# count default rises to 300 — at typical 1k content (~1.5 MiB/material)
# 300 materials = ~450 MiB, comfortably under the 700 MiB byte cap.
DEFAULT_BATCH_SIZE = 300
DEFAULT_BATCH_MAX_BYTES = 700 * 1024 * 1024  # 700 MiB


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


def _discover_total_materials(source: str) -> int:
    """Count materials on the upstream catalog for ``bake_plan``.

    Best-effort: failures return 0 so we still emit the plan line (with
    ``total_materials=0``) rather than crashing the bake. The real count
    surfaces in ``bake_done`` either way.
    """
    try:
        if source == "ambientcg":
            from mat_vis_baker.sources.ambientcg import discover

            return len(discover())
        if source == "polyhaven":
            from mat_vis_baker.sources.polyhaven import discover

            return len(discover())
        if source == "gpuopen":
            from mat_vis_baker.sources.gpuopen import discover

            return len(discover())
    except Exception as e:  # noqa: BLE001
        log.warning("bake_plan total_materials lookup failed (%s): %s", type(e).__name__, e)
    return 0


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


def _fetch_manifest_with_parent(api: HfApi, repo_id: str, revision: str) -> tuple[dict, str | None]:
    """Return ``(manifest, parent_sha)`` for the current revision.

    Both halves matter: the manifest content for the merge, and the parent
    SHA so a subsequent ``create_commit(parent_commit=...)`` can detect a
    concurrent writer (matrix bakes against the same release tag write
    the manifest from N parallel containers — see #207 race fix).
    """
    parent_sha: str | None = None
    try:
        info = api.repo_info(repo_id=repo_id, repo_type="dataset", revision=revision)
        parent_sha = getattr(info, "sha", None)
    except Exception:  # noqa: BLE001 — branch may not exist yet
        pass

    manifest: dict = {}
    try:
        path = api.hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=revision,
            filename="release-manifest.json",
        )
        manifest = json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001 — 404 / not yet uploaded → {}
        manifest = {}
    return manifest, parent_sha


def _merge_manifest_for_source(
    manifest: dict,
    source: str,
    tier: str,
    release_tag: str,
    *,
    mtlx_filename: str | None = None,
) -> dict:
    """Layer this bake's (source, tier) into ``manifest`` and return it.

    ``mtlx_filename`` (#292): when provided, stamp ``sources.<src>.mtlx``
    so clients (`MatVisClient._fetch_mtlx_original_map`) read the bundled
    upstream MaterialX JSON instead of guessing ``{source}-mtlx.json`` and
    silently 404-caching an empty dict. Sources without upstream .mtlx
    (e.g. ambientcg) leave the field absent.
    """
    manifest["schema_version"] = 3
    manifest["release_tag"] = release_tag
    sources = manifest.setdefault("sources", {})
    src_entry = sources.setdefault(source, {})
    src_entry["catalog"] = f"{source}.json"
    tiers = src_entry.setdefault("tiers", {})
    tiers[tier] = {"complete": True}
    if mtlx_filename is not None:
        src_entry["mtlx"] = mtlx_filename
    return manifest


def _fetch_catalog_with_parent(
    api: HfApi, repo_id: str, revision: str, source: str
) -> tuple[list[dict], str | None]:
    """Return ``(existing_catalog, parent_sha)`` for the per-source catalog.

    Mirrors :func:`_fetch_manifest_with_parent` for the per-source
    ``<source>.json``. ``parent_sha`` is unused for the catalog write
    (the manifest write is the CAS anchor — both files commit together
    in one operation), but returned for symmetry / future use.

    Returns ``([], None)`` when the catalog doesn't exist yet
    (first cut on a release tag).
    """
    parent_sha: str | None = None
    try:
        info = api.repo_info(repo_id=repo_id, repo_type="dataset", revision=revision)
        parent_sha = getattr(info, "sha", None)
    except Exception:  # noqa: BLE001 — branch may not exist yet
        pass

    existing: list[dict] = []
    try:
        path = api.hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=revision,
            filename=f"{source}.json",
        )
        loaded = json.loads(Path(path).read_text())
        if isinstance(loaded, list):
            existing = loaded
    except Exception:  # noqa: BLE001 — 404 / not yet uploaded → []
        existing = []
    return existing, parent_sha


def _merge_catalog_with_existing(
    fresh: list[dict],
    existing: list[dict],
    fresh_tier: str,
    *,
    prune_missing: bool = True,
) -> list[dict]:
    """Merge a freshly-baked per-source catalog onto the existing one.

    Implements the cross-tier ``available_tiers`` preservation that
    mat-vis#301 needs:

    - For each material in BOTH existing and fresh: take the FRESH
      entry (newest data wins) but set
      ``available_tiers = sorted(union(existing.available_tiers, [fresh_tier]))``.
    - For each material ONLY in existing: behavior depends on
      ``prune_missing`` — see below.
    - For each material ONLY in fresh: KEEP as-is (new addition;
      ``available_tiers = [fresh_tier]`` already from index_builder).

    The ``prune_missing`` flag (mat-vis#329 follow-up after E2E on tst):

    - ``True`` (default; **unbounded bakes only**): the fresh set is
      treated as the AUTHORITATIVE upstream snapshot for ``fresh_tier``.
      Materials only in existing have their fresh_tier removed from
      available_tiers; if that empties the list (the material existed
      only at this tier and fresh didn't see it), the entry is DROPPED
      — upstream pruned it.
    - ``False`` (**bounded bakes** — ``limit > 0`` or ``offset > 0``):
      the fresh set is a SUBSET, not the upstream truth. Materials only
      in existing are preserved verbatim with their existing
      ``available_tiers`` untouched — we don't know whether they were
      pruned upstream or just outside our slice.

    Caller (``bake_one_per_file``) passes ``prune_missing = (limit == 0
    and offset == 0)``. Production cuts always run unbounded so they
    prune; dev spot-tests with ``--limit=10`` preserve the rest of the
    catalog instead of clobbering 99% of it.

    Order preserved: fresh entries first (in their input order — already
    sorted by name in :func:`build_index`), then any preserved-existing
    entries in their original order.

    First-cut case (no existing): returns ``fresh`` unchanged.
    """
    if not existing:
        return fresh

    fresh_by_id: dict[str, dict] = {e["id"]: e for e in fresh if "id" in e}
    out: list[dict] = []

    # Pass 1: walk fresh in order, merging available_tiers from existing.
    existing_by_id: dict[str, dict] = {e["id"]: e for e in existing if "id" in e}
    for entry in fresh:
        mid = entry.get("id")
        if mid is None:
            continue
        prev = existing_by_id.get(mid)
        if prev is not None:
            prev_tiers = set(prev.get("available_tiers") or [])
            cur_tiers = set(entry.get("available_tiers") or [])
            merged_tiers = sorted(prev_tiers | cur_tiers)
            entry = {**entry, "available_tiers": merged_tiers}
        out.append(entry)

    # Pass 2: append existing entries that are NOT in fresh.
    for prev in existing:
        mid = prev.get("id")
        if mid is None or mid in fresh_by_id:
            continue
        if not prune_missing:
            # Bounded bake (limit/offset): fresh is a subset, not truth.
            # Preserve verbatim — we don't know whether this material
            # was pruned upstream or just outside our slice.
            out.append(prev)
            continue
        # Unbounded bake: fresh is authoritative for fresh_tier. Drop
        # fresh_tier from this material's available_tiers; if that
        # empties the list, the entry is gone (upstream pruned).
        prev_tiers = set(prev.get("available_tiers") or [])
        new_tiers = sorted(prev_tiers - {fresh_tier})
        if not new_tiers:
            continue
        out.append({**prev, "available_tiers": new_tiers})

    return out


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
    filter_ids: list[str] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_max_bytes: int = DEFAULT_BATCH_MAX_BYTES,
    dry_run: bool = False,
    storage_tier: str | None = None,
    metrics_path: Path | None = None,
    _pre_manifest_hook=None,
) -> dict:
    """Bake one (source, tier) into per-file HF commits.

    Returns a dict with ``ok``, ``failed``, ``skipped`` counts plus
    the last commit SHA on success. Pre-flight tree scan populates
    ``skipped``; actual bake work populates ``ok`` + ``failed``.
    Also returns ``cas_retries``: the number of times the catalog +
    manifest commit hit a 412 precondition mismatch and re-merged
    (zero on a single-writer bake; non-zero proves the retry path
    fired under concurrent writers — see #210/#230).

    Batching: flushes on first-of-N-or-bytes — whichever bound trips
    first. ``batch_size`` (default 300) caps materials per commit;
    ``batch_max_bytes`` (default 700 MiB) caps payload size,
    well under HF's 1 GiB hard cap and leaves headroom for the
    catalog + manifest + sentinel commits on the same hour budget.
    The rate-cap binding constraint flips from count to bytes for
    typical 1k content (~1.5 MiB/material), giving ~5× fewer commits
    per source vs the old count-only batching (#228).

    ``storage_tier`` (#230): test-only escape hatch. When set, used
    as the path key (``<source>/<storage_tier>/...``) and manifest
    tier label, while ``tier`` continues to drive the upstream
    fetcher. Lets the concurrency E2E (#210) park N parallel writers
    at distinct (source, tier) paths under one release tag without
    weakening the production tier guard. Default ``None`` →
    ``storage_tier == tier`` exactly as before. Not exposed on the
    CLI."""
    if not is_supported(source, tier):
        raise ValueError(unsupported_tier_message(source, tier))
    _guard_prod_target(repo_id, allow_prod)
    # #230: storage_tier defaults to the fetch tier — preserves
    # pre-existing single-arg semantics for every production caller.
    storage_tier = storage_tier if storage_tier is not None else tier

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
    # Use storage_tier — that's where past runs of THIS bake wrote.
    already = _already_committed_material_ids(api, repo_id, release_tag, source, storage_tier)
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
        "=== hf-bake-per-file %s %s%s → %s@%s "
        "(batch_size=%d, batch_max_bytes=%d, already_committed=%d) ===",
        source,
        tier,
        f" (storage={storage_tier})" if storage_tier != tier else "",
        repo_id,
        release_tag,
        batch_size,
        batch_max_bytes,
        len(already),
    )

    # #217: structured plan line — first non-init log entry of the bake
    # step. Emitted even when total_materials lookup fails (best-effort
    # discover() falls back to 0). expected_files is a rough estimate:
    # total_materials × len(CANONICAL_CHANNELS). Per-source channel sets
    # are non-uniform (see ambientcg.py), so this is "≈" not exact —
    # which the line shape (`expected_files≈<M>`) reflects.
    total_materials = _discover_total_materials(source)
    expected_files = total_materials * len(CANONICAL_CHANNELS)
    emit_bake_plan(
        source=source,
        tier=tier,
        total_materials=total_materials,
        expected_files=expected_files,
        release_tag=release_tag,
        repo_id=repo_id,
        kind="bake",
    )
    progress = ProgressTracker(
        source=source,
        tier=tier,
        total_materials=total_materials,
        kind="bake",
    )

    # #230: cross-call retry counter — every _create_commit_with_backoff
    # invocation in this bake shares it so we observe the full picture
    # (texture-batch lock-409s + manifest CAS lock-409s + manifest 412s).
    retry_counter: dict[str, int] = {}

    # #228: pending_batch holds (rec, ops) — ops are computed at append
    # time so we can accumulate pending_bytes against batch_max_bytes
    # without a second disk read at flush. Caching ops also avoids the
    # `_build_commit_ops_for_record` recomputation that the old flush did.
    pending_batch: list[tuple[MaterialRecord, list]] = []
    pending_bytes = 0

    # #263 phase B: 1-indexed batch counter for the metrics row's
    # batch_seq column. Incremented each time _flush_batch actually
    # emits an upload (skipped on empty pending_batch / dry-run-with-no-ops).
    batch_seq_counter = 0

    def _flush_batch(batch: list[tuple[MaterialRecord, list]]) -> str:
        """Upload every baked record's files as one atomic commit, then
        free the local texture bytes for that batch. Keeping the wipe
        coupled to the flush bounds peak disk at one batch — a crash
        mid-commit just orphans LFS blobs; HF's Xet dedup makes the
        re-upload free on the next run."""
        nonlocal last_commit_sha, n_ok, batch_seq_counter
        ops = []
        for _rec, rec_ops in batch:
            ops.extend(rec_ops)
        if not ops:
            return last_commit_sha
        # Account bytes by reading the same in-memory payload the
        # CommitOperationAdd holds — avoids a second disk read and
        # matches the actual upload size.
        batch_bytes = sum(
            len(op.path_or_fileobj) for op in ops if isinstance(op.path_or_fileobj, bytes)
        )
        if dry_run:
            log.info("dry-run: would commit %d files for %d materials", len(ops), len(batch))
            sha = ""
        else:
            # #225: bounded 429 retry/backoff. Helper re-raises every other
            # exception so the existing CAS / auth / network handlers
            # keep working untouched.
            commit = _create_commit_with_backoff(
                api,
                source=source,
                repo_id=repo_id,
                repo_type="dataset",
                operations=ops,
                commit_message=(
                    f"feat(data): {release_tag} — bake {source} {storage_tier} "
                    f"batch ({len(batch)} materials, {len(ops)} files)"
                ),
                revision=release_tag,
                _retry_counter=retry_counter,
            )
            sha = getattr(commit, "oid", "") or getattr(commit, "commit_oid", "")
            log.info(
                "batch commit %s: %d materials, %d files, sha=%s",
                source,
                len(batch),
                len(ops),
                sha[:12] if sha else "?",
            )
        # #263 phase B: emit a per-file metrics row per successful batch
        # commit. Production bake.yml passes a metrics_path; tests omit
        # it (or pass tmp_path) and the metrics file is git-tracked at
        # the repo root. Skip on dry-run — the row would carry an empty
        # OID and confuse the validator into thinking a commit happened.
        batch_seq_counter += 1
        if metrics_path is not None and not dry_run:
            try:
                record_batch(
                    metrics_path,
                    release_tag=release_tag,
                    source=source,
                    tier=storage_tier,
                    operation="bake",
                    batch_seq=batch_seq_counter,
                    materials_committed=len(batch),
                    files_committed=len(ops),
                    bytes_committed=batch_bytes,
                    hf_commit_oid=sha,
                    repo_id=repo_id,
                )
            except Exception as e:  # noqa: BLE001 — metrics are observability, not gating
                log.warning(
                    "per-file metrics record failed (%s): %s — bake continues",
                    type(e).__name__,
                    e,
                )
        # #217: structured progress line — emit AFTER the commit lands so
        # a crash mid-commit doesn't credit the operator with progress
        # the substrate doesn't actually hold.
        progress.record_batch(materials=len(batch), bytes_added=batch_bytes)
        progress.emit_progress()
        # Free per-rec files now the commit is durable.
        for rec, _rec_ops in batch:
            for p in list(rec.texture_paths.values()):
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
        return sha

    while True:
        batch_limit = batch_size
        if limit is not None:
            remaining = limit - fetched
            if remaining <= 0:
                break
            batch_limit = min(batch_limit, remaining)

        batch = fetch(
            tier,
            textures_dir,
            limit=batch_limit,
            offset=cursor,
            filter_ids=filter_ids,
            mtlx_dir=mtlx_dir,
        )
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
        # flush on whichever bound trips first — count >= batch_size
        # OR pending_bytes >= batch_max_bytes (#228).
        for rec in to_bake:
            if rec.status != "ok":
                n_failed += 1
                continue
            rec_ops = _build_commit_ops_for_record(rec, source, storage_tier)
            rec_bytes = sum(
                len(op.path_or_fileobj) for op in rec_ops if isinstance(op.path_or_fileobj, bytes)
            )
            pending_batch.append((rec, rec_ops))
            pending_bytes += rec_bytes
            n_ok += 1
            if len(pending_batch) >= batch_size or pending_bytes >= batch_max_bytes:
                last_commit_sha = _flush_batch(pending_batch)
                pending_batch.clear()
                pending_bytes = 0
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
        pending_bytes = 0

    if n_ok == 0 and n_skipped_preflight == 0:
        return {"error": "no materials", "ok": 0, "failed": n_failed}

    # Catalog commit (only write it — same skip-if-remote logic as the
    # tar path, so re-bakes don't churn the catalog).
    catalog_path = work_dir / f"{source}.json"
    index = build_index(all_records, source)
    catalog_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")

    # #292: pack upstream .mtlx files into ``<source>-mtlx.json`` and ship
    # it alongside the catalog. Both gpuopen (writes
    # ``mtlx_dir/gpuopen/<mid>/material.mtlx``) and polyhaven (writes
    # ``mtlx_dir/polyhaven/<slug>.mtlx``) drop their upstream .mtlx into
    # ``mtlx_dir`` during fetch, so we reuse ``pack_original_mtlx_json``
    # to bundle them. Sources without upstream .mtlx (ambientcg) yield
    # zero files; we skip the commit + manifest stamp in that case so
    # the manifest doesn't advertise an empty file.
    from mat_vis_baker.mtlx_tier import pack_original_mtlx_json

    mtlx_json_path: Path | None = None
    mtlx_filename: str | None = None
    source_mtlx_dir = mtlx_dir / source
    if source_mtlx_dir.is_dir() and any(source_mtlx_dir.rglob("*.mtlx")):
        mtlx_json_path = pack_original_mtlx_json(
            mtlx_dir=mtlx_dir,
            source=source,
            output_dir=work_dir,
        )
        # Only stamp the manifest if pack actually produced a non-empty map.
        try:
            packed = json.loads(mtlx_json_path.read_text())
        except Exception:  # noqa: BLE001
            packed = {}
        if packed:
            mtlx_filename = f"{source}-mtlx.json"
        else:
            mtlx_json_path = None

    # release-manifest.json — the entry point that JS/shell/Rust clients
    # fetch first. The Python client has a tree-fallback, but the static
    # file is the source of truth.
    #
    # Concurrency: matrix bakes (bake.yml) write the manifest from N
    # parallel containers against the same release tag. We use HF's
    # `parent_commit` parameter for optimistic locking — the commit
    # fails if the revision moved since we read it. On conflict we
    # re-fetch, re-merge, retry. Bounded retries prevent infinite loops
    # on a runaway concurrent writer.
    manifest_path = work_dir / "release-manifest.json"

    # Sentinel commit — marks tier as "atomically complete". Clients
    # can probe <source>/<storage_tier>/.tier_complete in one HEAD
    # request. Path keyed by storage_tier so a non-default override
    # (#230) lands the sentinel under the same path the texture
    # commits used.
    sentinel_name = ".tier_complete"
    sentinel_path = work_dir / f"{source}-{storage_tier}-{sentinel_name}"
    sentinel_path.write_text(release_tag + "\n")

    # #230: observable CAS-retry counter — exposed in the return dict
    # so the concurrency E2E (#210) can prove the retry path actually
    # fired without scraping log strings across multiprocessing pipes.
    cas_retries = 0

    if dry_run:
        log.info("dry-run: would commit catalog + manifest + sentinel")
    else:
        from huggingface_hub import CommitOperationAdd

        # #230: optional sync barrier for the concurrency E2E. Production
        # callers leave _pre_manifest_hook=None and skip this entirely.
        # The hook is fired exactly once, just before the first CAS
        # attempt, so N parallel workers all start the manifest commit
        # together and HF actually serves contention at the precondition
        # check. Without it, fetch-time variance lets workers finish
        # serially and cas_retries stays at 0 even with N processes.
        if _pre_manifest_hook is not None:
            try:
                _pre_manifest_hook()
            except Exception as e:  # noqa: BLE001
                log.warning("pre_manifest_hook raised %s: %s", type(e).__name__, e)

        # CAS retry loop on the catalog + manifest commit.
        max_retries = 6  # >> realistic matrix concurrency (≤4 sources today)
        for attempt in range(max_retries):
            existing_manifest, parent_sha = _fetch_manifest_with_parent(api, repo_id, release_tag)
            merged = _merge_manifest_for_source(
                existing_manifest,
                source,
                storage_tier,
                release_tag,
                mtlx_filename=mtlx_filename,
            )
            manifest_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")

            # mat-vis#301: per-source catalog merge-on-write.
            # Fetch existing `<source>.json` from HF and merge with the
            # freshly-baked records (preserving cross-tier
            # `available_tiers` for materials baked at OTHER tiers in
            # earlier dispatches). Without this, baking 1k then 2k
            # without a derive in between would clobber the 1k tier
            # off every material's `available_tiers` even though the 1k
            # files are still committed under `<source>/1k/`.
            #
            # Fetched inside the CAS loop so a concurrent writer's
            # update is observed on retry — same shape as the manifest
            # merge above. The `<source>.json` and manifest commit
            # together in one operation, so a single 412 on the manifest
            # already covers catalog conflicts.
            existing_catalog, _ = _fetch_catalog_with_parent(api, repo_id, release_tag, source)
            # mat-vis#329 follow-up after E2E on tst: only prune
            # "missing from fresh" materials when the bake is UNBOUNDED.
            # A limit-bound or offset-bound bake produces a SUBSET of
            # the upstream catalog, not the truth — clobbering everything
            # outside the subset (the original wholesale-replace bug)
            # would still happen if we always pruned. Production cuts
            # use limit=0/offset=0 so they prune normally; dev spot-tests
            # with --limit=10 keep the rest of the catalog intact.
            unbounded = (limit is None or limit == 0) and (offset is None or offset == 0)
            merged_catalog = _merge_catalog_with_existing(
                fresh=index,
                existing=existing_catalog,
                fresh_tier=storage_tier,
                prune_missing=unbounded,
            )
            catalog_path.write_text(json.dumps(merged_catalog, indent=2, ensure_ascii=False) + "\n")
            # #292: ship the packed upstream-MTLX JSON in the same atomic
            # commit as catalog + manifest. The manifest entry that points
            # at it lands in the same commit, so a client that sees the
            # manifest field is guaranteed to find the file (no
            # write-before-advertise race).
            catalog_ops = [
                CommitOperationAdd(
                    path_in_repo=f"{source}.json",
                    path_or_fileobj=str(catalog_path),
                ),
                CommitOperationAdd(
                    path_in_repo="release-manifest.json",
                    path_or_fileobj=str(manifest_path),
                ),
            ]
            if mtlx_json_path is not None:
                catalog_ops.append(
                    CommitOperationAdd(
                        path_in_repo=f"{source}-mtlx.json",
                        path_or_fileobj=str(mtlx_json_path),
                    )
                )
            try:
                # #225: 429 retry sits *inside* the CAS loop. The helper
                # re-raises 412 unchanged so the precondition matcher
                # below still catches concurrent-writer conflicts; only
                # 429 throttles get the bounded backoff treatment.
                catalog_commit = _create_commit_with_backoff(
                    api,
                    source=source,
                    repo_id=repo_id,
                    repo_type="dataset",
                    operations=catalog_ops,
                    commit_message=f"feat(data): {release_tag} — {source} catalog + manifest",
                    revision=release_tag,
                    parent_commit=parent_sha,
                    _retry_counter=retry_counter,
                )
                last_commit_sha = getattr(catalog_commit, "oid", "") or last_commit_sha
                break
            except Exception as e:  # noqa: BLE001
                # HF returns 412 Precondition Failed on parent_commit mismatch
                # (the optimistic-lock path we designed for). It ALSO returns
                # 409 "Another commit operation is in progress" when two
                # commits race the server-side per-repo write lock — same
                # underlying cause (concurrent writer), different layer.
                # Treat both as a CAS retry: re-fetch + re-merge + retry.
                # Other exceptions (auth, network) re-raise after the loop.
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
                            "manifest CAS exhausted after %d retries — concurrent writers?",
                            max_retries,
                        )
                        raise
                    log.warning(
                        "manifest CAS retry %d/%d — concurrent writer detected",
                        attempt + 1,
                        max_retries,
                    )
                    cas_retries += 1
                    continue
                raise

        # Sentinel commit — final marker. #225: 429-aware. #230: 409-aware.
        sentinel_commit = _create_commit_with_backoff(
            api,
            source=source,
            repo_id=repo_id,
            repo_type="dataset",
            operations=[
                CommitOperationAdd(
                    path_in_repo=f"{source}/{storage_tier}/{sentinel_name}",
                    path_or_fileobj=str(sentinel_path),
                )
            ],
            commit_message=f"feat(data): {release_tag} — {source} {storage_tier} complete",
            revision=release_tag,
            _retry_counter=retry_counter,
        )
        last_commit_sha = getattr(sentinel_commit, "oid", "") or last_commit_sha

    log.info(
        "PERF per-file bake: %.1fs, %d ok / %d failed / %d skipped (preflight)",
        time.monotonic() - t0,
        n_ok,
        n_failed,
        n_skipped_preflight,
    )
    # #217: structured terminal line — single-line summary that closes
    # the (plan, progress, …, done) triple a parser can lock onto.
    progress.emit_done(ok=n_ok, failed=n_failed, skipped_preflight=n_skipped_preflight)

    return {
        "commit": last_commit_sha,
        "ok": n_ok,
        "failed": n_failed,
        "skipped_preflight": n_skipped_preflight,
        "materials": len(all_records),
        # #230: contention observability.
        # ``cas_retries`` counts 412 parent_commit mismatches on the
        # manifest commit (the optimistic-lock path). ``lock_409_retries``
        # counts 409 per-repo write-lock contention across every commit
        # this bake makes. Either one being non-zero proves the bake
        # raced another writer and recovered cleanly.
        "cas_retries": cas_retries,
        "lock_409_retries": retry_counter.get("lock_409", 0),
    }
