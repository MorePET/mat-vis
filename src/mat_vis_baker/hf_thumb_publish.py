"""Per-material thumb publish (#402 / mat-vis#361).

Walks a local directory of pre-rendered thumb PNGs (produced by
``bake/preview/run.py``) and uploads them to HF as a new
``thumb`` tier on the per-file substrate (ADR-0012).

Layout on disk (input)::

    <thumbs_dir>/<source>/<material_id>/thumb.png

Layout on HF (output)::

    <source>/thumb/<material_id>/thumb.png

Why this layout — keep ``<source>/<tier>/<mid>/<channel>.<ext>``
consistent with every other tier so the existing
:func:`mat_vis_client.client.MatVisClient.fetch_texture` URL builder
(``_per_file_url``) round-trips for free. The "channel" name is the
literal ``thumb`` so it slots into the catalog's ``maps`` list and
``available_tiers`` extension lands naturally.

Three load-bearing invariants, mirrored from
:mod:`hf_bake_per_file` and :mod:`hf_derive_per_file`:

1. **Pre-flight HEAD probe** — skip materials whose
   ``<source>/thumb/<mid>/thumb.png`` already exists. Re-runs are
   bytes-free for completed materials.

2. **Batched commits** — first-of-N-or-bytes flush, same #228
   convention as bake / derive (``batch_size=300`` count ceiling,
   700 MiB byte ceiling).

3. **Sentinel-last + CAS-protected manifest** — the catalog +
   ``release-manifest.json`` commit (CAS-retried against concurrent
   resize / ktx2 dispatches) lands first, then the
   ``<source>/thumb/.tier_complete`` sentinel as the very last commit
   so clients can probe one path to assert tier-level atomicity.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi

from mat_vis_baker.hf_bake_per_file import (
    _fetch_manifest_with_parent,
    _guard_prod_target,
    _merge_manifest_for_source,
)
from mat_vis_baker.hf_derive_per_file import (
    PNG_MAGIC,
    _extend_available_tiers,
    _fetch_catalog,
    _http_head_ok,
    _resolve_url,
)
from mat_vis_baker.hf_retry import _create_commit_with_backoff

log = logging.getLogger("mat-vis-baker.hf_thumb_publish")

# #228: same ceilings as bake / derive — count is a safe overshoot,
# bytes is the binding constraint at typical 256² ~10 KB thumbs.
DEFAULT_BATCH_SIZE = 300
DEFAULT_BATCH_MAX_BYTES = 700 * 1024 * 1024  # 700 MiB

# The single channel name used for thumbs. One PNG per material.
THUMB_CHANNEL = "thumb"
THUMB_TIER = "thumb"


def _list_local_thumbs(thumbs_dir: Path, source: str) -> list[tuple[str, Path]]:
    """Return sorted ``[(material_id, path)]`` for every thumb PNG under
    ``<thumbs_dir>/<source>/``.

    Sorted for determinism so retries cover materials in the same
    order as the originating run. ``thumb.png`` is the conventional
    output of ``bake/preview/run.py`` — any other filename in the
    per-material dir is ignored.
    """
    src_dir = thumbs_dir / source
    if not src_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for entry in sorted(src_dir.iterdir()):
        if not entry.is_dir():
            continue
        thumb_png = entry / "thumb.png"
        if thumb_png.is_file():
            out.append((entry.name, thumb_png))
    return out


def _verify_png(data: bytes, *, where: str) -> None:
    if not data.startswith(PNG_MAGIC):
        raise RuntimeError(f"{where}: expected PNG magic, got {data[:8]!r} ({len(data)} bytes)")


def _extend_maps_for_thumb(catalog: list[dict], derived_ids: set[str]) -> list[dict]:
    """Add ``"thumb"`` to each entry's ``maps`` list for derived ids.

    Mirrors :func:`_extend_available_tiers`'s in-place + idempotent
    style. The client's :meth:`MatVisClient.channels` reads from
    ``maps``; without this entry, ``fetch_texture(..., channel="thumb",
    tier="thumb")`` raises a friendly "channel not found" error before
    we even hit HF.
    """
    for entry in catalog:
        mid = entry.get("id")
        if not mid or mid not in derived_ids:
            continue
        maps = entry.get("maps")
        if not isinstance(maps, list):
            entry["maps"] = [THUMB_CHANNEL]
            continue
        if THUMB_CHANNEL not in maps:
            maps.append(THUMB_CHANNEL)
    return catalog


def publish_thumb_tier(
    source: str,
    release_tag: str,
    thumbs_dir: Path,
    repo_id: str,
    *,
    hf_token: str | None = None,
    dry_run: bool = False,
    allow_prod: bool = False,
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_max_bytes: int = DEFAULT_BATCH_MAX_BYTES,
) -> dict:
    """Upload every locally-baked thumb PNG to HF as the ``thumb`` tier.

    Produces commits at::

        <source>/thumb/<material_id>/thumb.png       (per-file)
        <source>.json                                 (catalog +tier +map)
        release-manifest.json                         (sources.<src>.tiers.thumb.complete=true)
        <source>/thumb/.tier_complete                 (sentinel-last)

    Pre-flight HEAD probe per material — re-runs are bytes-free for
    already-published thumbs. The catalog + manifest commit is
    CAS-retried against concurrent writers (resize / ktx2 dispatches
    that touch ``release-manifest.json`` from sibling matrix cells).

    ``thumbs_dir`` must contain ``<source>/<mid>/thumb.png`` files —
    the layout produced by ``bake/preview/run.py --out <thumbs_dir>``.
    """
    _guard_prod_target(repo_id, allow_prod)
    api = HfApi(token=hf_token)

    t0 = time.monotonic()
    log.info(
        "=== thumb publish %s → %s @ %s (batch_size=%d, batch_max_bytes=%d, dry_run=%s) ===",
        source,
        THUMB_TIER,
        release_tag,
        batch_size,
        batch_max_bytes,
        dry_run,
    )

    locals_ = _list_local_thumbs(thumbs_dir, source)
    if limit is not None:
        locals_ = locals_[:limit]
    if not locals_:
        return {
            "error": f"no local thumbs found under {thumbs_dir}/{source}/",
            "ok": 0,
            "failed": 0,
            "skipped_preflight": 0,
        }

    log.info("found %d local thumbs for %s", len(locals_), source)

    n_ok = 0
    n_failed = 0
    n_skipped = 0
    derived_ids: set[str] = set()
    last_commit_sha = ""

    pending_ops: list[CommitOperationAdd] = []
    pending_mids: list[str] = []
    pending_bytes = 0

    def _flush_batch() -> str:
        nonlocal last_commit_sha
        if not pending_ops:
            return last_commit_sha
        batch_bytes = sum(
            len(op.path_or_fileobj) for op in pending_ops if isinstance(op.path_or_fileobj, bytes)
        )
        if dry_run:
            log.info(
                "thumb dry-run: would commit %d files for %d materials (%d bytes)",
                len(pending_ops),
                len(pending_mids),
                batch_bytes,
            )
            return last_commit_sha
        commit = _create_commit_with_backoff(
            api,
            source=source,
            repo_id=repo_id,
            repo_type="dataset",
            operations=list(pending_ops),
            commit_message=(
                f"feat(data): {release_tag} — publish {source} {THUMB_TIER} "
                f"({len(pending_mids)} materials)"
            ),
            revision=release_tag,
        )
        sha = getattr(commit, "oid", "") or getattr(commit, "commit_oid", "")
        log.info(
            "thumb batch commit: %d materials, %d files, %d bytes, sha=%s",
            len(pending_mids),
            len(pending_ops),
            batch_bytes,
            sha[:12] if sha else "?",
        )
        return sha or last_commit_sha

    for i, (mid, png_path) in enumerate(locals_):
        target_path = f"{source}/{THUMB_TIER}/{mid}/{THUMB_CHANNEL}.png"
        target_url = _resolve_url(repo_id, release_tag, target_path)

        # Resume preflight: HEAD probe — skip if already on HF.
        if _http_head_ok(target_url, token=hf_token):
            n_skipped += 1
            derived_ids.add(mid)
            continue

        try:
            data = png_path.read_bytes()
            _verify_png(data, where=f"local {png_path}")
        except Exception as e:  # noqa: BLE001
            log.warning("thumb read/verify failed for %s/%s: %s", source, mid, e)
            n_failed += 1
            continue

        pending_ops.append(CommitOperationAdd(path_in_repo=target_path, path_or_fileobj=data))
        pending_mids.append(mid)
        pending_bytes += len(data)
        derived_ids.add(mid)
        n_ok += 1

        if (i + 1) % 200 == 0:
            log.info(
                "thumb progress %d/%d (ok=%d, skipped=%d, failed=%d)",
                i + 1,
                len(locals_),
                n_ok,
                n_skipped,
                n_failed,
            )

        if len(pending_mids) >= batch_size or pending_bytes >= batch_max_bytes:
            last_commit_sha = _flush_batch()
            pending_ops.clear()
            pending_mids.clear()
            pending_bytes = 0

    if pending_mids:
        last_commit_sha = _flush_batch()
        pending_ops.clear()
        pending_mids.clear()
        pending_bytes = 0

    if n_ok == 0 and n_skipped == 0:
        return {
            "error": "no thumbs published",
            "ok": 0,
            "failed": n_failed,
            "skipped_preflight": 0,
        }

    # ── catalog + manifest update (CAS-retried) ──────────────────
    catalog = _fetch_catalog(repo_id, release_tag, source, hf_token)
    if catalog:
        _extend_available_tiers(catalog, derived_ids, THUMB_TIER)
        _extend_maps_for_thumb(catalog, derived_ids)
        catalog_bytes = (json.dumps(catalog, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if dry_run:
            log.info(
                "thumb dry-run: would commit updated %s.json + manifest (%d entries touched)",
                source,
                len(derived_ids),
            )
        else:
            max_retries = 6
            for attempt in range(max_retries):
                manifest, parent_sha = _fetch_manifest_with_parent(api, repo_id, release_tag)
                merged = _merge_manifest_for_source(manifest, source, THUMB_TIER, release_tag)
                manifest_bytes = (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode(
                    "utf-8"
                )
                try:
                    commit = _create_commit_with_backoff(
                        api,
                        source=source,
                        repo_id=repo_id,
                        repo_type="dataset",
                        operations=[
                            CommitOperationAdd(
                                path_in_repo=f"{source}.json",
                                path_or_fileobj=catalog_bytes,
                            ),
                            CommitOperationAdd(
                                path_in_repo="release-manifest.json",
                                path_or_fileobj=manifest_bytes,
                            ),
                        ],
                        commit_message=(
                            f"feat(data): {release_tag} — {source} catalog + manifest "
                            f"({THUMB_TIER} added)"
                        ),
                        revision=release_tag,
                        parent_commit=parent_sha,
                    )
                    last_commit_sha = (
                        getattr(commit, "oid", "")
                        or getattr(commit, "commit_oid", "")
                        or last_commit_sha
                    )
                    break
                except Exception as e:  # noqa: BLE001
                    msg = str(e).lower()
                    if "412" in msg or "precondition" in msg or "parent_commit" in msg:
                        if attempt + 1 == max_retries:
                            log.error("thumb manifest CAS exhausted after %d retries", max_retries)
                            raise
                        log.warning(
                            "thumb manifest CAS retry %d/%d — concurrent writer detected",
                            attempt + 1,
                            max_retries,
                        )
                        continue
                    raise
    else:
        log.info("thumb: no catalog fetched (empty or missing); skipping catalog update")

    # ── sentinel-last ─────────────────────────────────────────
    sentinel_path = f"{source}/{THUMB_TIER}/.tier_complete"
    sentinel_body = (release_tag + "\n").encode("utf-8")
    if dry_run:
        log.info("thumb dry-run: would commit sentinel %s", sentinel_path)
    else:
        commit = _create_commit_with_backoff(
            api,
            source=source,
            repo_id=repo_id,
            repo_type="dataset",
            operations=[
                CommitOperationAdd(
                    path_in_repo=sentinel_path,
                    path_or_fileobj=sentinel_body,
                )
            ],
            commit_message=f"feat(data): {release_tag} — {source} {THUMB_TIER} complete",
            revision=release_tag,
        )
        last_commit_sha = (
            getattr(commit, "oid", "") or getattr(commit, "commit_oid", "") or last_commit_sha
        )

    log.info(
        "PERF thumb publish: %.1fs, %d ok / %d failed / %d skipped (preflight)",
        time.monotonic() - t0,
        n_ok,
        n_failed,
        n_skipped,
    )

    return {
        "commit": last_commit_sha,
        "ok": n_ok,
        "failed": n_failed,
        "skipped_preflight": n_skipped,
        "materials": len(locals_),
    }
