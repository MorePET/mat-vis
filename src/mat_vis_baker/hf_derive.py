"""Derive smaller / transcoded tiers from an existing HF tar (ADR-0007).

Streams each channel via HTTP Range reads against the source tar's
HF resolve URL — never downloads the full tar to disk, never loads
it into RAM. Transform + fetch is parallelised via a thread pool
(I/O-bound for HTTP, CPU-bound for toktx; both benefit).

Two pipelines:

- ``derive_smaller_tier(..., target_tier="512")`` — PIL resize every
  channel, pack into ``<source>-<target>.tar``.
- ``derive_ktx2_tier(..., source_tier="1k")`` — ``toktx`` transcode,
  pack into ``ktx2/<source>-<target>.tar``. Requires ``toktx`` on PATH.

Both finish with an atomic HF commit carrying manifest + catalog +
tar + rowmap; the catalog's ``available_tiers`` is extended in place
and the manifest's per-source ``tiers`` map gains the new key.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests
from PIL import Image

from mat_vis_baker.common import TIER_TO_PX
from mat_vis_baker.hf_push import push_to_hf
from mat_vis_baker.tar_writer import TarWriter
from mat_vis_baker.telemetry import span

log = logging.getLogger("mat-vis-baker.hf_derive")

DEFAULT_REPO_ID = "gerchowl/mat-vis"
HF_RESOLVE = "https://huggingface.co/datasets"

# Workers for per-channel fetch+transform. Resize is I/O-heavy; ktx2
# is toktx-bound (CPU). Both benefit from modest parallelism without
# blowing up memory (only N in-flight channels resident at once).
DEFAULT_RESIZE_WORKERS = int(os.environ.get("MAT_VIS_DERIVE_WORKERS", "8"))
DEFAULT_KTX2_WORKERS = int(os.environ.get("MAT_VIS_KTX2_WORKERS", "4"))


# ── HTTP-range streaming source ────────────────────────────────


def _pin_commit(repo_id: str, revision: str, token: str | None) -> str:
    """Pin the revision to a concrete commit SHA.

    A branch tag can move during a long-running derive if another
    bake commits. Resolving once to the current HEAD sha and range-
    reading that sha's URL keeps the rowmap offsets consistent with
    the bytes we actually fetch.
    """
    from huggingface_hub import HfApi

    commits = HfApi(token=token).list_repo_commits(
        repo_id=repo_id, repo_type="dataset", revision=revision
    )
    return commits[0].commit_id


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _fetch_rowmap(*, resolve_base: str, source: str, source_tier: str, token: str | None) -> dict:
    r = requests.get(
        f"{resolve_base}/{source}-{source_tier}-rowmap.json",
        headers=_auth_headers(token),
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _range_read(*, session: requests.Session, tar_url: str, spec: dict, token: str | None) -> bytes:
    lo = int(spec["offset"])
    length = int(spec["length"])
    headers = {"Range": f"bytes={lo}-{lo + length - 1}", **_auth_headers(token)}
    r = session.get(tar_url, headers=headers, timeout=120)
    r.raise_for_status()
    data = r.content
    if len(data) != length:
        raise RuntimeError(f"range read short: asked {length} bytes, got {len(data)} @ {tar_url}")
    return data


# ── test shim: kept so existing unit tests that pass bytes still work ──


def _slice_channel(tar_bytes_or_fh, spec: dict) -> bytes:
    """Test helper — production path uses ``_range_read`` instead."""
    lo = int(spec["offset"])
    length = int(spec["length"])
    if isinstance(tar_bytes_or_fh, (bytes, bytearray, memoryview)):
        return bytes(tar_bytes_or_fh[lo : lo + length])
    tar_bytes_or_fh.seek(lo)
    return tar_bytes_or_fh.read(length)


# ── parallel pipeline ─────────────────────────────────────────


def _stream_transform_into_tar(
    *,
    materials: dict,
    tar_url: str,
    token: str | None,
    transform: Callable[[bytes], bytes],
    max_workers: int,
    out_tar_path: Path,
    label: str,
) -> dict[str, dict[str, dict[str, int]]]:
    """Fetch every channel via HTTP Range, run ``transform`` in a worker
    pool, write sequentially into ``out_tar_path``. Returns the rowmap
    materials dict produced by the writer."""
    session = requests.Session()
    work: list[tuple[str, str, dict]] = [
        (mid, ch, spec) for mid, channels in materials.items() for ch, spec in channels.items()
    ]
    n_total = len(work)

    def _one(item):
        mid, ch, spec = item
        raw = _range_read(session=session, tar_url=tar_url, spec=spec, token=token)
        return mid, ch, transform(raw)

    # Two-layer failure detection:
    #
    # (1) *Continuous* fail-fast — a sliding-window check that stays
    #     armed for the whole run, not a one-shot at the first-50
    #     boundary. If the last WINDOW completions exceed FAIL_RATIO
    #     failures and we've seen at least MIN_SAMPLES overall, abort.
    #     Catches mid-run regressions (rate-limit, disk-full, a
    #     position-correlated format bug) that earlier versions missed.
    #
    # (2) *Terminal* success-rate gate — after all work drains,
    #     reject a run whose overall success rate is below
    #     TERMINAL_MIN_OK_RATIO. Prevents publishing a near-empty tar
    #     with ``n_ok == 1`` as if it were a valid derive.
    WINDOW = 50
    MIN_SAMPLES = 50
    FAIL_RATIO = 0.20
    TERMINAL_MIN_OK_RATIO = 0.90

    from collections import deque

    recent = deque(maxlen=WINDOW)  # True=failure, False=success

    n_ok = 0
    n_failed = 0
    first_error: str | None = None
    t_last = time.monotonic()
    t_start = time.monotonic()
    with span("stream.transform", label=label, n_total=n_total, max_workers=max_workers) as outer:
        with ThreadPoolExecutor(max_workers=max_workers) as pool, TarWriter(out_tar_path) as tw:
            futures = {pool.submit(_one, item): item for item in work}
            for fut in as_completed(futures):
                mid, ch, spec = futures[fut]
                try:
                    r_mid, r_ch, out_bytes = fut.result()
                    tw.add_channel(r_mid, r_ch, out_bytes)
                    n_ok += 1
                    recent.append(False)
                except Exception as e:
                    if first_error is None:
                        first_error = f"{mid}/{ch}: {e}"
                    log.warning("%s/%s: %s failed: %s", mid, ch, label, e)
                    n_failed += 1
                    recent.append(True)

                completed = n_ok + n_failed
                if (
                    completed >= MIN_SAMPLES
                    and len(recent) == WINDOW
                    and sum(recent) / WINDOW > FAIL_RATIO
                ):
                    pool.shutdown(wait=False, cancel_futures=True)
                    outer.set_attribute("outcome", "fail_fast")
                    outer.set_attribute("n_ok", n_ok)
                    outer.set_attribute("n_failed", n_failed)
                    raise RuntimeError(
                        f"{label}: fail-fast — last {WINDOW} completions had "
                        f"{sum(recent)} failures (>{int(FAIL_RATIO * 100)}%) "
                        f"after {completed}/{n_total} total. "
                        f"First error: {first_error}"
                    )

                if time.monotonic() - t_last > 30:
                    elapsed = time.monotonic() - t_start
                    rate = completed / elapsed if elapsed else 0
                    eta = (n_total - completed) / rate if rate > 0 else 0
                    log.info(
                        "%s progress: %d/%d ok, %d failed (window=%d%%, rate=%.1f/s, eta=%ds)",
                        label,
                        n_ok,
                        n_total,
                        n_failed,
                        int(100 * sum(recent) / max(1, len(recent))),
                        rate,
                        int(eta),
                    )
                    outer.add_event(
                        "progress",
                        {"n_ok": n_ok, "n_failed": n_failed, "rate_per_s": rate},
                    )
                    t_last = time.monotonic()
            new_materials = tw.finalize()
        outer.set_attribute("outcome", "ok")
        outer.set_attribute("n_ok", n_ok)
        outer.set_attribute("n_failed", n_failed)

    # Terminal gate: refuse to ship a partial tar. A few dozen bad
    # textures in a 11k-channel bake is tolerable; sub-90% is not.
    ok_ratio = n_ok / n_total if n_total else 0.0
    if ok_ratio < TERMINAL_MIN_OK_RATIO:
        _write_step_summary(label, n_ok, n_failed, n_total, first_error)
        raise RuntimeError(
            f"{label}: terminal check — {n_ok}/{n_total} succeeded "
            f"({ok_ratio * 100:.1f}% < {int(TERMINAL_MIN_OK_RATIO * 100)}%). "
            f"Refusing to push a partial tar. First error: {first_error}"
        )

    log.info("%s done: %d ok / %d failed / %d total", label, n_ok, n_failed, n_total)
    _write_step_summary(label, n_ok, n_failed, n_total, first_error)
    return new_materials, n_ok, n_failed


def _write_step_summary(
    label: str, n_ok: int, n_failed: int, n_total: int, first_error: str | None
) -> None:
    """Append a markdown summary to $GITHUB_STEP_SUMMARY (no-op off-CI)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    pct = 100 * n_ok / n_total if n_total else 0.0
    status = "✅" if n_failed == 0 else ("⚠️" if n_ok > 0 else "❌")
    lines = [
        f"### {status} `{label}`",
        "",
        f"- **ok**: {n_ok} / {n_total} ({pct:.1f}%)",
        f"- **failed**: {n_failed}",
    ]
    if first_error:
        lines.append(f"- **first error**: `{first_error}`")
    lines.append("")
    try:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


# ── resize ────────────────────────────────────────────────────


def _resize_transform(target_px: int) -> Callable[[bytes], bytes]:
    def _t(raw: bytes) -> bytes:
        img = Image.open(io.BytesIO(raw))
        img.load()
        resized = img.resize((target_px, target_px), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG", optimize=False)
        return buf.getvalue()

    return _t


def derive_smaller_tier(
    source: str,
    target_tier: str,
    source_tier: str,
    release_tag: str,
    work_dir: Path,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    hf_token: str | None = None,
    dry_run: bool = False,
    workers: int = DEFAULT_RESIZE_WORKERS,
) -> dict:
    """Stream-resize every channel from ``<source>-<source_tier>.tar``
    to ``target_tier`` resolution. HTTP-range-reads the source tar; no
    local full-tar download, no full-tar in RAM."""
    if target_tier not in TIER_TO_PX:
        raise ValueError(f"unknown target tier {target_tier!r}")
    if source_tier not in TIER_TO_PX:
        raise ValueError(f"unknown source tier {source_tier!r}")
    target_px = TIER_TO_PX[target_tier]
    if TIER_TO_PX[source_tier] < target_px:
        raise ValueError(
            f"source tier {source_tier!r} ({TIER_TO_PX[source_tier]}px) is smaller "
            f"than target {target_tier!r} ({target_px}px) — upscaling not supported"
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    out_tar_name = f"{source}-{target_tier}.tar"
    out_rowmap_name = f"{source}-{target_tier}-rowmap.json"
    out_tar_path = work_dir / out_tar_name
    out_rowmap_path = work_dir / out_rowmap_name

    t0 = time.monotonic()
    log.info(
        "=== hf-derive %s %s → %s @ %s (streaming, workers=%d) ===",
        source,
        source_tier,
        target_tier,
        release_tag,
        workers,
    )

    sha = _pin_commit(repo_id, release_tag, hf_token)
    resolve_base = f"{HF_RESOLVE}/{repo_id}/resolve/{sha}"
    log.info("pinned %s@%s → %s", repo_id, release_tag, sha[:12])

    src_rowmap = _fetch_rowmap(
        resolve_base=resolve_base, source=source, source_tier=source_tier, token=hf_token
    )
    materials = src_rowmap.get("materials", {})
    tar_url = f"{resolve_base}/{source}-{source_tier}.tar"

    new_materials, n_ok, n_failed = _stream_transform_into_tar(
        materials=materials,
        tar_url=tar_url,
        token=hf_token,
        transform=_resize_transform(target_px),
        max_workers=workers,
        out_tar_path=out_tar_path,
        label=f"resize→{target_tier}",
    )
    if n_ok == 0:
        return {"error": "no channels resized", "ok": 0, "failed": n_failed}

    rowmap = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": target_tier,
        "tar_file": out_tar_name,
        "materials": new_materials,
    }
    out_rowmap_path.write_text(json.dumps(rowmap, indent=2) + "\n")

    log.info(
        "PERF derive: %.1fs, %d ok / %d failed, out_tar=%.1f MB",
        time.monotonic() - t0,
        n_ok,
        n_failed,
        out_tar_path.stat().st_size / 1e6,
    )
    if dry_run:
        return {"dry_run": True, "ok": n_ok, "failed": n_failed}

    # Only push the tar + its rowmap. No catalog/manifest touch —
    # tier presence is discovered from the tree listing by clients
    # (removes the merge-race class that bit the earlier matrix).
    sha = push_to_hf(
        repo_id=repo_id,
        files=[
            (out_tar_path, out_tar_name),
            (out_rowmap_path, out_rowmap_name),
        ],
        revision=release_tag,
        commit_message=f"feat(data): {release_tag} — derive {source} {target_tier} from {source_tier}",
        token=hf_token,
    )
    return {
        "commit": sha,
        "ok": n_ok,
        "failed": n_failed,
        "tar_bytes": out_tar_path.stat().st_size,
    }


# ── ktx2 transcode ────────────────────────────────────────────


def _ktx2_transform_factory() -> Callable[[bytes], bytes]:
    """Return a transform that writes PNG → tmp → toktx → reads KTX2 back.

    Re-encodes the incoming PNG via PIL first, which strips ICC color
    profiles and other metadata that ``toktx`` refuses (seen on
    polyhaven: "It has an ICC profile. These are not supported.").
    Pixel data is untouched — we never interpret color space here.

    toktx stderr is captured and propagated into the exception so
    real failures surface in logs instead of opaque exit-code-1.
    """

    def _t(raw: bytes) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            png_in = tmp_dir / "in.png"
            ktx_out = tmp_dir / "out.ktx2"

            # Strip ICC / EXIF / color metadata — keep pixels.
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

    return _t


def derive_ktx2_tier(
    source: str,
    source_tier: str,
    release_tag: str,
    work_dir: Path,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    hf_token: str | None = None,
    dry_run: bool = False,
    target_tier: str | None = None,
    workers: int = DEFAULT_KTX2_WORKERS,
) -> dict:
    """Stream-transcode every channel from ``<source>-<source_tier>.tar``
    to KTX2. Requires ``toktx`` on PATH."""
    target_tier = target_tier or f"ktx2-{source_tier}"

    try:
        subprocess.run(["toktx", "--version"], capture_output=True, check=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise RuntimeError(
            "toktx not on PATH — install KTX-Software "
            "(https://github.com/KhronosGroup/KTX-Software/releases)"
        ) from e

    work_dir.mkdir(parents=True, exist_ok=True)
    out_subdir_name = "ktx2"
    out_tar_name = f"{source}-{target_tier}.tar"
    out_tar_in_repo = f"{out_subdir_name}/{out_tar_name}"
    out_rowmap_name = f"{source}-{target_tier}-rowmap.json"
    out_rowmap_in_repo = f"{out_subdir_name}/{out_rowmap_name}"
    (work_dir / out_subdir_name).mkdir(exist_ok=True)
    out_tar_path = work_dir / out_subdir_name / out_tar_name
    out_rowmap_path = work_dir / out_subdir_name / out_rowmap_name

    t0 = time.monotonic()
    log.info(
        "=== hf-derive-ktx2 %s %s → %s @ %s (streaming, workers=%d) ===",
        source,
        source_tier,
        target_tier,
        release_tag,
        workers,
    )

    sha = _pin_commit(repo_id, release_tag, hf_token)
    resolve_base = f"{HF_RESOLVE}/{repo_id}/resolve/{sha}"
    log.info("pinned %s@%s → %s", repo_id, release_tag, sha[:12])

    src_rowmap = _fetch_rowmap(
        resolve_base=resolve_base, source=source, source_tier=source_tier, token=hf_token
    )
    materials = src_rowmap.get("materials", {})
    tar_url = f"{resolve_base}/{source}-{source_tier}.tar"

    new_materials, n_ok, n_failed = _stream_transform_into_tar(
        materials=materials,
        tar_url=tar_url,
        token=hf_token,
        transform=_ktx2_transform_factory(),
        max_workers=workers,
        out_tar_path=out_tar_path,
        label=f"ktx2→{target_tier}",
    )
    if n_ok == 0:
        return {"error": "no channels transcoded", "ok": 0, "failed": n_failed}

    rowmap = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": target_tier,
        "tar_file": out_tar_in_repo,
        "materials": new_materials,
    }
    out_rowmap_path.write_text(json.dumps(rowmap, indent=2) + "\n")

    log.info(
        "PERF ktx2: %.1fs, %d ok / %d failed, out_tar=%.1f MB",
        time.monotonic() - t0,
        n_ok,
        n_failed,
        out_tar_path.stat().st_size / 1e6,
    )
    if dry_run:
        return {"dry_run": True, "ok": n_ok, "failed": n_failed}

    # Only push the tar + rowmap. Tier presence is discovered by
    # clients from the tree listing (ADR-0007 race-free design).
    sha = push_to_hf(
        repo_id=repo_id,
        files=[
            (out_tar_path, out_tar_in_repo),
            (out_rowmap_path, out_rowmap_in_repo),
        ],
        revision=release_tag,
        commit_message=f"feat(data): {release_tag} — derive {source} {target_tier} from {source_tier}",
        token=hf_token,
    )
    return {
        "commit": sha,
        "ok": n_ok,
        "failed": n_failed,
        "tar_bytes": out_tar_path.stat().st_size,
    }
