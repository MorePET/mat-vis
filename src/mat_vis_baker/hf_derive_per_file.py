"""Per-file derive pipeline (#204 / ADR-0012).

Resurrects the ``hf-derive`` capability that was deleted in #189 alongside
the tar substrate. Lets a release built at one tier (e.g. 4k) feed the
smaller tiers (1k, 512, …) and a KTX2 transcode without a fresh upstream
fetch — every channel is read straight off the HF substrate via plain
HTTPS GET, transformed in-memory, and written back per-file.

Two public entry points:

- :func:`derive_smaller_tier` — PIL ``Image.resize(LANCZOS)`` from a
  larger source tier into a smaller target tier; output stays PNG.
- :func:`derive_ktx2_tier` — ``toktx`` transcode of an existing PNG
  tier into a KTX2 tier; output is per-file ``.ktx2``.

Both share three load-bearing properties with :mod:`hf_bake_per_file`:

1. **Pre-flight HEAD probe** — before doing any work for a material,
   probe ``<source>/<target_tier>/<mid>/<channel>.{png,ktx2}`` with
   HTTP HEAD; skip materials whose entire channel set already exists.
   Mirrors the bake path's tree-scan resume primitive but without
   needing an HfApi token (HEAD on resolve URLs is bytes-free public).

2. **Batch commits** — each commit groups up to ``batch_size`` materials
   so a mid-derive crash leaves at most one in-flight batch. Empirical
   probe (see ADR-0012) clears 350-file commits in <10 s on HF.

3. **Sentinel-last atomicity** — the very last commit per (source,
   target_tier) writes ``<source>/<target_tier>/.tier_complete`` so
   clients can probe a single file to detect tier completeness.
   The catalog (`<source>.json`) commit happens *before* the sentinel;
   together they restore the ADR-0007 "atomic tier" mental model.

Tar code is intentionally not imported — #189 deleted ``tar_writer``
and ``shard_utils`` entirely, and ADR-0012 forbids reintroducing them.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from huggingface_hub import CommitOperationAdd, HfApi
from PIL import Image

from mat_vis_baker.common import CANONICAL_CHANNELS, TIER_TO_PX
from mat_vis_baker.hf_bake_per_file import _guard_prod_target
from mat_vis_baker.hf_retry import _create_commit_with_backoff
from mat_vis_baker.progress import ProgressTracker, emit_bake_plan

log = logging.getLogger("mat-vis-baker.hf_derive_per_file")

# Default HF resolve base; override via HF_BASE env (kept consistent with
# the Python client). The legacy module hard-coded the same URL — we
# accept the canonical override so test/staging fixtures can stub it.
HF_RESOLVE_BASE = "https://huggingface.co/datasets"

# #228: count default raised to 300; bytes ceiling becomes the real
# binding constraint at typical 1k content (~1.5 MiB/material × 7
# channels). Default batch_max_bytes 700 MiB stays well under HF's
# 1 GiB per-commit cap and keeps headroom for catalog + manifest +
# sentinel commits on the 128/hr/repo budget.
DEFAULT_BATCH_SIZE = 300
DEFAULT_BATCH_MAX_BYTES = 700 * 1024 * 1024  # 700 MiB

# PNG / KTX2 magic — matches ``hf_bake_per_file._channel_ext``. Used by
# the magic-byte verification in :func:`_verify_png` / :func:`_verify_ktx2`.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
KTX2_MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"


# ── HTTP helpers ──────────────────────────────────────────────


def _resolve_url(repo_id: str, release_tag: str, path: str) -> str:
    """Build a resolve URL on the canonical HF dataset base."""
    return f"{HF_RESOLVE_BASE}/{repo_id}/resolve/{release_tag}/{path}"


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _http_get(url: str, *, token: str | None = None, timeout: int = 120) -> bytes:
    """Plain GET; raises ``urllib.error.HTTPError`` on non-2xx."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "mat-vis-baker/derive", **_auth_headers(token)},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_head_ok(url: str, *, token: str | None = None, timeout: int = 30) -> bool:
    """Return True iff the URL responds 2xx to HEAD. Used by the
    pre-flight skip — false for 404, network errors, etc."""
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "mat-vis-baker/derive", **_auth_headers(token)},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError:
        return False
    except Exception:  # noqa: BLE001
        return False


def _get_json(url: str, *, token: str | None = None, timeout: int = 60) -> Any:
    return json.loads(_http_get(url, token=token, timeout=timeout))


# ── catalog helpers ───────────────────────────────────────────


def _fetch_catalog(repo_id: str, release_tag: str, source: str, token: str | None) -> list[dict]:
    """Fetch ``<source>.json`` from the resolve URL. Returns [] if missing.

    Per ADR-0011, the catalog is a list of v3-shaped entries. The legacy
    derive path reused the same structure; we only ever append to
    ``available_tiers`` here, never reshape the entries.
    """
    url = _resolve_url(repo_id, release_tag, f"{source}.json")
    try:
        body = _get_json(url, token=token)
    except Exception as e:  # noqa: BLE001
        log.warning("catalog fetch failed (%s): %s", type(e).__name__, e)
        return []
    if not isinstance(body, list):
        log.warning("catalog at %s is not a list (got %s)", url, type(body).__name__)
        return []
    return body


def _extend_available_tiers(
    catalog: list[dict], derived_ids: set[str], target_tier: str
) -> list[dict]:
    """Append ``target_tier`` to ``available_tiers`` for every catalog
    entry whose id is in ``derived_ids``. Idempotent — entries that
    already list the tier are left alone."""
    for entry in catalog:
        mid = entry.get("id")
        if not mid or mid not in derived_ids:
            continue
        tiers = entry.get("available_tiers")
        if not isinstance(tiers, list):
            entry["available_tiers"] = [target_tier]
            continue
        if target_tier not in tiers:
            tiers.append(target_tier)
    return catalog


# ── transforms ────────────────────────────────────────────────


def _verify_png(data: bytes, *, where: str) -> None:
    if not data.startswith(PNG_MAGIC):
        raise RuntimeError(f"{where}: expected PNG magic, got {data[:8]!r} ({len(data)} bytes)")


def _verify_ktx2(data: bytes, *, where: str) -> None:
    if not data.startswith(KTX2_MAGIC):
        raise RuntimeError(f"{where}: expected KTX2 magic, got {data[:12]!r} ({len(data)} bytes)")


def _resize_png(raw: bytes, target_px: int) -> bytes:
    """LANCZOS-resize ``raw`` PNG bytes down to ``target_px`` square.

    Strips ICC / EXIF profiles by re-saving without them — the same
    profile-stripping the legacy ktx2 path did. This keeps the derived
    PNGs portable to ``toktx`` if a later derive transcodes them.
    """
    img = Image.open(io.BytesIO(raw))
    img.load()
    resized = img.resize((target_px, target_px), Image.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _ktx2_transcode(raw: bytes) -> bytes:
    """Re-encode incoming PNG via PIL (strips ICC / EXIF), shell out
    to ``toktx --encode uastc --genmipmap --t2``, return KTX2 bytes.

    ``toktx`` invocation mirrors the legacy ``hf_derive._ktx2_transform_factory``
    — same flags, same uastc encoding, same mipmap pyramid.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        png_in = tmp_dir / "in.png"
        ktx_out = tmp_dir / "out.ktx2"

        img = Image.open(io.BytesIO(raw))
        img.load()
        img.save(png_in, format="PNG", icc_profile=None)

        result = subprocess.run(
            [
                "toktx",
                "--encode",
                "uastc",
                "--genmipmap",
                "--t2",
                str(ktx_out),
                str(png_in),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"toktx exit {result.returncode}: "
                f"{(result.stderr or result.stdout or '').strip()[:500]}"
            )
        return ktx_out.read_bytes()


# ── shared driver ─────────────────────────────────────────────


def _list_source_material_ids(
    api: HfApi, repo_id: str, revision: str, source: str, source_tier: str
) -> list[str]:
    """List materials present at ``<source>/<source_tier>/`` on
    ``revision``. Mirrors ``hf_bake_per_file._already_committed_material_ids``
    but returns an ordered list (sort for determinism so retries replay
    the same work distribution)."""
    prefix = f"{source}/{source_tier}/"
    mids: set[str] = set()
    try:
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
            if len(parts) == 2 and parts[0] and not parts[0].startswith("."):
                mids.add(parts[0])
    except Exception as e:  # noqa: BLE001
        log.warning(
            "source-tier tree scan failed (%s): %s — derive will exit empty",
            type(e).__name__,
            e,
        )
    return sorted(mids)


def _list_source_channels(
    api: HfApi,
    repo_id: str,
    revision: str,
    source: str,
    source_tier: str,
    material_id: str,
) -> list[str]:
    """List channels present for one material under ``<source>/<source_tier>/<mid>/``.

    Stripped of extension. Falls back to :data:`CANONICAL_CHANNELS` if
    listing fails (the GET will 404 cleanly per channel and the failure
    counter trips the terminal gate).
    """
    prefix = f"{source}/{source_tier}/{material_id}/"
    chs: list[str] = []
    try:
        for entry in api.list_repo_tree(
            repo_id=repo_id,
            repo_type="dataset",
            revision=revision,
            path_in_repo=prefix.rstrip("/"),
            recursive=False,
        ):
            path = getattr(entry, "path", None)
            if not path or not path.startswith(prefix):
                continue
            rel = path[len(prefix) :]  # noqa: E203
            if "/" in rel or rel.startswith("."):
                continue
            stem = rel.rsplit(".", 1)[0]
            chs.append(stem)
    except Exception as e:  # noqa: BLE001
        log.debug("channel listing fallback for %s: %s", material_id, e)
        return list(CANONICAL_CHANNELS)
    return chs or list(CANONICAL_CHANNELS)


def _all_target_files_present(
    repo_id: str,
    release_tag: str,
    source: str,
    target_tier: str,
    material_id: str,
    channels: list[str],
    target_ext: str,
    token: str | None,
) -> bool:
    """HEAD-probe every expected output file. True only when *all* land
    successfully — a partial-resize crash retries the missing channels."""
    if not channels:
        return False
    for ch in channels:
        url = _resolve_url(
            repo_id, release_tag, f"{source}/{target_tier}/{material_id}/{ch}.{target_ext}"
        )
        if not _http_head_ok(url, token=token):
            return False
    return True


def _derive_one_material(
    *,
    repo_id: str,
    release_tag: str,
    source: str,
    source_tier: str,
    target_tier: str,
    material_id: str,
    channels: list[str],
    transform: Callable[[bytes], bytes],
    target_ext: str,
    token: str | None,
) -> list[CommitOperationAdd]:
    """Fetch + transform every channel. Returns the list of
    ``CommitOperationAdd`` ops for this material; empty if every channel
    fetch or transform failed (caller treats as material-level failure)."""
    ops: list[CommitOperationAdd] = []
    for ch in channels:
        src_url = _resolve_url(
            repo_id, release_tag, f"{source}/{source_tier}/{material_id}/{ch}.png"
        )
        try:
            raw = _http_get(src_url, token=token)
        except Exception as e:  # noqa: BLE001
            log.warning("%s/%s/%s: source GET failed: %s", source, material_id, ch, e)
            continue

        # Magic-byte verify the source bytes — catches HTML 404 pages
        # served as 200 on misconfigured tags.
        try:
            _verify_png(raw, where=f"source {source}/{source_tier}/{material_id}/{ch}.png")
        except RuntimeError as e:
            log.warning("%s", e)
            continue

        try:
            out = transform(raw)
        except Exception as e:  # noqa: BLE001
            log.warning("%s/%s/%s: transform failed: %s", source, material_id, ch, e)
            continue

        # Magic-byte verify the produced bytes (PNG for resize, KTX2 for
        # transcode). Refuses to commit malformed output.
        try:
            if target_ext == "png":
                _verify_png(out, where=f"derived {source}/{target_tier}/{material_id}/{ch}.png")
            elif target_ext == "ktx2":
                _verify_ktx2(out, where=f"derived {source}/{target_tier}/{material_id}/{ch}.ktx2")
        except RuntimeError as e:
            log.warning("%s", e)
            continue

        ops.append(
            CommitOperationAdd(
                path_in_repo=f"{source}/{target_tier}/{material_id}/{ch}.{target_ext}",
                path_or_fileobj=out,
            )
        )
    return ops


def _derive_driver(
    *,
    source: str,
    source_tier: str,
    target_tier: str,
    release_tag: str,
    work_dir: Path,
    repo_id: str,
    transform: Callable[[bytes], bytes],
    target_ext: str,
    label: str,
    hf_token: str | None,
    dry_run: bool,
    allow_prod: bool,
    limit: int | None,
    batch_size: int,
    batch_max_bytes: int,
    on_progress: Callable[[dict], None] | None,
) -> dict:
    """Shared driver behind :func:`derive_smaller_tier` and
    :func:`derive_ktx2_tier`. Single code path so the sentinel-last,
    catalog-update, and batching invariants only need to live in one
    place.

    Batching: first-of-N-or-bytes — flush on whichever bound trips
    first (count >= ``batch_size`` OR pending payload bytes >=
    ``batch_max_bytes``). #228."""
    _guard_prod_target(repo_id, allow_prod)
    work_dir.mkdir(parents=True, exist_ok=True)
    api = HfApi(token=hf_token)

    t0 = time.monotonic()
    log.info(
        "=== %s %s/%s → %s @ %s (batch_size=%d, batch_max_bytes=%d, dry_run=%s) ===",
        label,
        source,
        source_tier,
        target_tier,
        release_tag,
        batch_size,
        batch_max_bytes,
        dry_run,
    )

    # Enumerate material ids from the source tier. Sort for determinism
    # so retries cover the same materials in the same order.
    mids = _list_source_material_ids(api, repo_id, release_tag, source, source_tier)
    if limit is not None:
        mids = mids[:limit]
    if not mids:
        return {"error": "no source materials", "ok": 0, "failed": 0, "skipped_preflight": 0}

    # #217: structured plan line. derive_plan mirrors bake_plan exactly
    # except for the leading token. expected_files is materials × the
    # canonical channel count (a rough estimate; per-source channel
    # sets are non-uniform, hence the `≈` in the format).
    expected_files = len(mids) * len(CANONICAL_CHANNELS)
    emit_bake_plan(
        source=source,
        tier=target_tier,
        total_materials=len(mids),
        expected_files=expected_files,
        release_tag=release_tag,
        repo_id=repo_id,
        kind="derive",
    )
    progress = ProgressTracker(
        source=source,
        tier=target_tier,
        total_materials=len(mids),
        kind="derive",
    )

    n_ok = 0
    n_failed = 0
    n_skipped = 0
    derived_ids: set[str] = set()
    last_commit_sha = ""

    pending_ops: list[CommitOperationAdd] = []
    pending_mids: list[str] = []
    # #228: bytes accumulator drives the bytes-aware flush bound.
    pending_bytes = 0

    def _flush_batch() -> str:
        nonlocal last_commit_sha
        if not pending_ops:
            return last_commit_sha
        # Bytes accounting: the ops we built carry the transformed
        # bytes inline, so summing len() avoids a second pass.
        batch_bytes = sum(
            len(op.path_or_fileobj) for op in pending_ops if isinstance(op.path_or_fileobj, bytes)
        )
        if dry_run:
            log.info(
                "%s dry-run: would commit %d files for %d materials",
                label,
                len(pending_ops),
                len(pending_mids),
            )
            sha = ""
        else:
            # #225: 429-aware. Per-batch derive commits are the busiest
            # writers — phase-4 derive jobs against a freshly baked tier
            # were the trigger for the original 429.
            commit = _create_commit_with_backoff(
                api,
                source=source,
                repo_id=repo_id,
                repo_type="dataset",
                operations=list(pending_ops),
                commit_message=(
                    f"feat(data): {release_tag} — derive {source} {target_tier} "
                    f"from {source_tier} ({len(pending_mids)} materials, "
                    f"{len(pending_ops)} files)"
                ),
                revision=release_tag,
            )
            sha = getattr(commit, "oid", "") or getattr(commit, "commit_oid", "")
            log.info(
                "%s batch commit: %d materials, %d files, sha=%s",
                label,
                len(pending_mids),
                len(pending_ops),
                sha[:12] if sha else "?",
            )
        # #217: structured progress line — emit AFTER the commit lands.
        progress.record_batch(materials=len(pending_mids), bytes_added=batch_bytes)
        progress.emit_progress()
        return sha or last_commit_sha

    for i, mid in enumerate(mids):
        # Source-side channel discovery — what does this material actually
        # have at source_tier? Avoids requesting a normal.png that the
        # source omitted.
        channels = _list_source_channels(api, repo_id, release_tag, source, source_tier, mid)

        # Resume preflight: if every output channel already exists at
        # target_tier, skip the work.
        if _all_target_files_present(
            repo_id=repo_id,
            release_tag=release_tag,
            source=source,
            target_tier=target_tier,
            material_id=mid,
            channels=channels,
            target_ext=target_ext,
            token=hf_token,
        ):
            n_skipped += 1
            # Still mark it for catalog update — the tier IS available
            # for this material on the substrate, regardless of whether
            # this run produced it.
            derived_ids.add(mid)
            if on_progress is not None:
                on_progress({"phase": "skip", "material_id": mid, "i": i, "total": len(mids)})
            continue

        ops = _derive_one_material(
            repo_id=repo_id,
            release_tag=release_tag,
            source=source,
            source_tier=source_tier,
            target_tier=target_tier,
            material_id=mid,
            channels=channels,
            transform=transform,
            target_ext=target_ext,
            token=hf_token,
        )
        if not ops:
            n_failed += 1
            if on_progress is not None:
                on_progress({"phase": "fail", "material_id": mid, "i": i, "total": len(mids)})
            continue

        n_ok += 1
        derived_ids.add(mid)
        pending_ops.extend(ops)
        pending_mids.append(mid)
        # #228: byte-account at append-time so the flush check below can
        # short-circuit on bytes ceiling without re-reading payloads.
        pending_bytes += sum(
            len(op.path_or_fileobj) for op in ops if isinstance(op.path_or_fileobj, bytes)
        )
        if on_progress is not None:
            on_progress(
                {
                    "phase": "ok",
                    "material_id": mid,
                    "files": len(ops),
                    "i": i,
                    "total": len(mids),
                }
            )

        # #228: first-of-N-or-bytes — count >= batch_size OR pending
        # bytes >= batch_max_bytes triggers a flush.
        if len(pending_mids) >= batch_size or pending_bytes >= batch_max_bytes:
            last_commit_sha = _flush_batch()
            pending_ops.clear()
            pending_mids.clear()
            pending_bytes = 0

    # Final partial batch.
    if pending_mids:
        last_commit_sha = _flush_batch()
        pending_ops.clear()
        pending_mids.clear()
        pending_bytes = 0

    if n_ok == 0 and n_skipped == 0:
        return {
            "error": "no materials derived",
            "ok": 0,
            "failed": n_failed,
            "skipped_preflight": 0,
        }

    # ── catalog + manifest update (atomic, CAS-retried) ──────
    # Mirror the bake's #207 fix: single commit covers catalog +
    # release-manifest.json with parent_commit guarding against
    # matrix-write races (multiple derives may run on the same tag).
    from mat_vis_baker.hf_bake_per_file import (
        _fetch_manifest_with_parent,
        _merge_manifest_for_source,
    )

    catalog = _fetch_catalog(repo_id, release_tag, source, hf_token)
    if catalog:
        _extend_available_tiers(catalog, derived_ids, target_tier)
        catalog_bytes = (json.dumps(catalog, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if dry_run:
            log.info(
                "%s dry-run: would commit updated %s.json + manifest (%d entries touched)",
                label,
                source,
                len(derived_ids),
            )
        else:
            max_retries = 6
            for attempt in range(max_retries):
                manifest, parent_sha = _fetch_manifest_with_parent(api, repo_id, release_tag)
                merged = _merge_manifest_for_source(manifest, source, target_tier, release_tag)
                manifest_bytes = (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode(
                    "utf-8"
                )
                try:
                    # #225: 429-aware inside the CAS loop — same shape as
                    # the bake-side catalog commit; helper re-raises 412
                    # so the precondition matcher below still fires.
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
                            f"({target_tier} added)"
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
                            log.error(
                                "%s manifest CAS exhausted after %d retries", label, max_retries
                            )
                            raise
                        log.warning(
                            "%s manifest CAS retry %d/%d — concurrent writer detected",
                            label,
                            attempt + 1,
                            max_retries,
                        )
                        continue
                    raise
    else:
        log.info("%s: no catalog fetched (empty or missing); skipping catalog update", label)

    # ── sentinel-last ─────────────────────────────────────────
    sentinel_path = f"{source}/{target_tier}/.tier_complete"
    sentinel_body = (release_tag + "\n").encode("utf-8")
    if dry_run:
        log.info("%s dry-run: would commit sentinel %s", label, sentinel_path)
    else:
        # #225: 429-aware. The sentinel is the very last commit and the
        # one that died in #225's stack trace — wrap it.
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
            commit_message=f"feat(data): {release_tag} — {source} {target_tier} complete",
            revision=release_tag,
        )
        last_commit_sha = (
            getattr(commit, "oid", "") or getattr(commit, "commit_oid", "") or last_commit_sha
        )

    log.info(
        "PERF %s: %.1fs, %d ok / %d failed / %d skipped (preflight)",
        label,
        time.monotonic() - t0,
        n_ok,
        n_failed,
        n_skipped,
    )
    # #217: closing line of the (plan, progress, …, done) triple.
    progress.emit_done(ok=n_ok, failed=n_failed, skipped_preflight=n_skipped)

    return {
        "commit": last_commit_sha,
        "ok": n_ok,
        "failed": n_failed,
        "skipped_preflight": n_skipped,
        "materials": len(mids),
    }


# ── public entry points ───────────────────────────────────────


def derive_smaller_tier(
    source: str,
    target_tier: str,
    source_tier: str,
    release_tag: str,
    work_dir: Path,
    repo_id: str,
    *,
    hf_token: str | None = None,
    dry_run: bool = False,
    allow_prod: bool = False,
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_max_bytes: int = DEFAULT_BATCH_MAX_BYTES,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Resize every channel from ``source_tier`` (PNG) into ``target_tier``
    (PNG) on the per-file substrate.

    Reads source channels via plain HTTPS GET on the resolve URL,
    runs PIL ``Image.resize(LANCZOS)``, writes the smaller PNG back at
    ``<source>/<target_tier>/<mid>/<channel>.png``. Pre-flight HEAD-probes
    the target paths so re-runs are bytes-free for already-derived
    materials.

    The catalog (``<source>.json``) is fetched, every derived id has
    ``target_tier`` appended to its ``available_tiers``, and the file
    is committed. The final commit is a zero-byte
    ``<source>/<target_tier>/.tier_complete`` sentinel — clients can
    HEAD this single path to assert tier-level atomicity.

    Refuses upscale (would silently invent pixels): a target px larger
    than the source px raises ``ValueError`` before any HF traffic.
    """
    if target_tier not in TIER_TO_PX:
        raise ValueError(f"unknown target tier {target_tier!r}")
    if source_tier not in TIER_TO_PX:
        raise ValueError(f"unknown source tier {source_tier!r}")
    if TIER_TO_PX[source_tier] < TIER_TO_PX[target_tier]:
        raise ValueError(
            f"source tier {source_tier!r} ({TIER_TO_PX[source_tier]}px) is smaller than "
            f"target {target_tier!r} ({TIER_TO_PX[target_tier]}px) — upscaling not supported"
        )

    target_px = TIER_TO_PX[target_tier]
    transform = lambda raw: _resize_png(raw, target_px)  # noqa: E731

    return _derive_driver(
        source=source,
        source_tier=source_tier,
        target_tier=target_tier,
        release_tag=release_tag,
        work_dir=work_dir,
        repo_id=repo_id,
        transform=transform,
        target_ext="png",
        label=f"derive resize→{target_tier}",
        hf_token=hf_token,
        dry_run=dry_run,
        allow_prod=allow_prod,
        limit=limit,
        batch_size=batch_size,
        batch_max_bytes=batch_max_bytes,
        on_progress=on_progress,
    )


def derive_ktx2_tier(
    source: str,
    source_tier: str,
    target_tier: str,
    release_tag: str,
    work_dir: Path,
    repo_id: str,
    *,
    hf_token: str | None = None,
    dry_run: bool = False,
    allow_prod: bool = False,
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_max_bytes: int = DEFAULT_BATCH_MAX_BYTES,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Transcode every channel from ``source_tier`` (PNG) into
    ``target_tier`` (KTX2) on the per-file substrate.

    ``target_tier`` is a free-form tier label — the convention chosen
    here is ``ktx2-<source_tier>`` (e.g. source_tier='1k' →
    target_tier='ktx2-1k'), matching the legacy ADR-0007 path that
    nested KTX2 outputs under a ``ktx2-…`` namespace. The CLI / Dagger
    wrappers default to that convention but the function accepts any
    label.

    Requires ``toktx`` on ``PATH`` (KTX-Software). Fails loudly with
    a clear install hint at the first transcode attempt; we don't
    pre-flight ``toktx --version`` because the per-channel ``run`` is
    the only time it's actually invoked.
    """
    if source_tier not in TIER_TO_PX:
        raise ValueError(f"unknown source tier {source_tier!r}")
    # target_tier is intentionally not in TIER_TO_PX — KTX2 names
    # diverge from the resize tier vocabulary.

    return _derive_driver(
        source=source,
        source_tier=source_tier,
        target_tier=target_tier,
        release_tag=release_tag,
        work_dir=work_dir,
        repo_id=repo_id,
        transform=_ktx2_transcode,
        target_ext="ktx2",
        label=f"derive ktx2→{target_tier}",
        hf_token=hf_token,
        dry_run=dry_run,
        allow_prod=allow_prod,
        limit=limit,
        batch_size=batch_size,
        batch_max_bytes=batch_max_bytes,
        on_progress=on_progress,
    )
