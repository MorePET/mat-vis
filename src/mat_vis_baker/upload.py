"""Atomic chunk upload helpers.

Addresses #60: the bake pipeline needs to upload parquet + rowmap assets to
a GitHub release without silently losing data. The previous implementation
called ``gh release upload`` with ``check=False`` and then immediately
deleted the local file, so any transient failure destroyed the only copy.

This module provides:

 * :func:`atomic_write` — context manager that writes to ``.part`` and
   ``os.replace``s into place only on successful close+fsync.
 * :func:`gh_upload` — ``gh release upload`` wrapped with exponential
   backoff and strict error handling.
 * :func:`verify_upload_size` — read back the asset size via ``gh api`` and
   compare against the local file size; catches truncated/partial uploads.
 * :func:`upload_with_verify` — upload, verify, retry on mismatch.
 * :func:`load_progress`/:func:`save_progress` — resume-marker helpers
   persisted to ``/tmp/out/.bake-progress.json``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("mat-vis-baker.upload")

_TRANSIENT_MARKERS = (
    "rate limit",
    "rate-limit",
    "429",
    "503",
    "502",
    "504",
    "timeout",
    "timed out",
    "connection reset",
    "could not resolve",
    "temporary failure",
)


class UploadError(RuntimeError):
    """Raised when an asset upload cannot be completed."""


# ── atomic file writes ──────────────────────────────────────────


@contextmanager
def atomic_write_path(final_path: Path):
    """Yield a ``.part`` path; ``os.replace`` into ``final_path`` on success.

    On exception, the partial file is removed. Caller is responsible for
    closing any file handles BEFORE the context exits — we do our own
    fsync on the path just before the rename for extra safety.
    """
    final_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = final_path.with_name(final_path.name + ".part")
    if part_path.exists():
        part_path.unlink()
    try:
        yield part_path
    except Exception:
        part_path.unlink(missing_ok=True)
        raise

    if not part_path.exists():
        raise UploadError(f"atomic_write_path: {part_path} was not created")

    # fsync the file contents before the rename.
    fd = os.open(str(part_path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(part_path, final_path)


# ── gh upload with retry ────────────────────────────────────────


def _is_transient(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _TRANSIENT_MARKERS)


def gh_upload(
    path: Path,
    release_tag: str,
    *,
    max_retries: int = 5,
    backoff_base: float = 2.0,
    _sleep=time.sleep,
    _run=subprocess.run,
) -> None:
    """Upload one file to a release, retrying on transient errors.

    Raises :class:`UploadError` if all retries are exhausted or if the
    failure is clearly non-transient (e.g. auth error).
    """
    if not path.exists():
        raise UploadError(f"gh_upload: source {path} does not exist")

    last_stderr = ""
    for attempt in range(max_retries):
        result = _run(
            ["gh", "release", "upload", release_tag, str(path), "--clobber"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            if attempt > 0:
                log.info("gh upload succeeded on retry %d for %s", attempt, path.name)
            return

        last_stderr = (result.stderr or "").strip()
        transient = _is_transient(last_stderr)
        if not transient:
            # Non-transient (auth, bad args, etc.) — fail fast, no retry.
            raise UploadError(
                f"gh upload failed for {path.name} (exit {result.returncode}): {last_stderr[:500]}"
            )

        # Transient: back off and retry, unless this was the final attempt.
        if attempt + 1 >= max_retries:
            break
        wait = backoff_base**attempt
        log.warning(
            "gh upload transient failure for %s (attempt %d/%d): %s — retry in %.1fs",
            path.name,
            attempt + 1,
            max_retries,
            last_stderr[:200],
            wait,
        )
        _sleep(wait)

    raise UploadError(
        f"gh upload exhausted {max_retries} retries for {path.name}: {last_stderr[:500]}"
    )


# ── upload verification ─────────────────────────────────────────


def verify_upload_size(
    release_tag: str,
    asset_name: str,
    expected_size: int,
    *,
    _run=subprocess.run,
) -> bool:
    """Query the release and confirm the asset exists at the expected size.

    Returns True on match, False otherwise. Logs a warning on mismatch.
    """
    result = _run(
        [
            "gh",
            "release",
            "view",
            release_tag,
            "--json",
            "assets",
            "--jq",
            f'.assets[] | select(.name == "{asset_name}") | .size',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log.warning(
            "verify_upload_size: gh release view failed for %s: %s",
            asset_name,
            (result.stderr or "").strip()[:200],
        )
        return False

    sizes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not sizes:
        log.warning("verify_upload_size: asset %s not found on release", asset_name)
        return False

    try:
        remote_size = int(sizes[0])
    except ValueError:
        log.warning("verify_upload_size: unparsable size %r for %s", sizes[0], asset_name)
        return False

    if remote_size != expected_size:
        log.warning(
            "verify_upload_size: %s size mismatch — local=%d remote=%d",
            asset_name,
            expected_size,
            remote_size,
        )
        return False
    return True


def upload_with_verify(
    path: Path,
    release_tag: str,
    *,
    max_verify_retries: int = 3,
    _sleep=time.sleep,
    _run=subprocess.run,
) -> None:
    """Upload + size-verify; retry the whole dance if the remote size
    doesn't match the local size.

    Raises :class:`UploadError` if verification fails after retries.
    """
    expected_size = path.stat().st_size
    for attempt in range(max_verify_retries):
        gh_upload(path, release_tag, _run=_run, _sleep=_sleep)
        if verify_upload_size(release_tag, path.name, expected_size, _run=_run):
            return
        log.warning(
            "upload_with_verify: size mismatch for %s, re-uploading (attempt %d/%d)",
            path.name,
            attempt + 1,
            max_verify_retries,
        )
        _sleep(2.0)

    raise UploadError(
        f"upload_with_verify: {path.name} failed verification after {max_verify_retries} attempts"
    )


# ── resume markers ──────────────────────────────────────────────


PROGRESS_FILENAME = ".bake-progress.json"


def progress_path(output_dir: Path) -> Path:
    return output_dir / PROGRESS_FILENAME


def save_progress(
    output_dir: Path,
    *,
    source: str,
    tier: str,
    offset_done: int,
    chunk_nums: dict[str, int],
    completed_categories: list[str],
    release_tag: str | None = None,
) -> Path:
    """Persist current bake offset so a crashed run can resume.

    Written to ``<output_dir>/.bake-progress.json``. If ``release_tag`` is
    set and uploads are enabled elsewhere, callers may additionally upload
    this file as a release asset so a fresh runner can fetch it.
    """
    data = {
        "source": source,
        "tier": tier,
        "offset_done": offset_done,
        "chunk_nums": dict(chunk_nums),
        "completed_categories": list(completed_categories),
        "release_tag": release_tag,
        "saved_at": time.time(),
    }
    path = progress_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def load_progress(output_dir: Path) -> dict | None:
    """Read the progress marker if it exists and looks valid."""
    path = progress_path(output_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.warning("load_progress: failed to read %s: %s", path, e)
        return None


def clear_progress(output_dir: Path) -> None:
    progress_path(output_dir).unlink(missing_ok=True)
