"""Bounded retry/backoff for HF Hub commit throttling (#225, #230).

The HF Hub enforces two rate caps that surface as ``429 Too Many
Requests`` on ``api.create_commit``:

1. **Commit-rate cap** — ~128 commits per hour per repo. Phase-3 matrix
   bakes plus phase-4 derives running in the same hour against the same
   repo can saturate this, killing the in-flight job at its final
   manifest commit (see #225 stack trace).

2. **API-rate cap** — ~1000 requests per 300s rolling window. Not
   biting yet, but surfaces as the same 429 status, so the same retry
   path handles it.

3. **Per-repo write lock** (#230) — HF serializes commits at the
   repository granularity. Two writers racing the same repo see a
   ``409 Conflict`` with body ``"Another commit operation is in
   progress for this repository. Please try again later."``. Same
   retry envelope as 429 — bounded backoff, then re-raise — but with
   a shorter floor since the lock typically clears in single-digit
   seconds, not the 30s 429 cooldown.

This module wraps every ``api.create_commit`` call site (bake +
derive) with :func:`_create_commit_with_backoff`, which:

- Detects 429 via ``response.status_code``.
- Parses ``Retry-After`` (HTTP-date or integer seconds) from headers.
- Falls back to parsing ``"Retry after N seconds"`` from the response
  body.
- Falls back to exponential backoff with jitter, starting at 30s and
  capped at 120s.
- Caps total attempts at ``max_retries`` (default 5).
- Emits one structured ``bake_throttle`` log line per retry, mirroring
  the format conventions in :mod:`mat_vis_baker.progress` (single line,
  ``key=value`` pairs, ``flush=True`` for live-tail visibility).
- Re-raises every other exception unchanged so the existing 412 (CAS
  precondition) retry loop in :mod:`hf_bake_per_file` keeps working.

No new runtime deps — just stdlib ``time`` + a small regex on the body.
"""

from __future__ import annotations

import email.utils
import random
import re
import time
from typing import Any

from huggingface_hub.errors import HfHubHTTPError

# Backoff envelope. The 30s floor matches the smallest ``Retry-After``
# HF observed in #225's stack trace; the 120s cap keeps a runaway repo
# from stalling a workflow for the full GH-Actions step budget.
_BACKOFF_FLOOR_S = 30.0
_BACKOFF_CEIL_S = 120.0
_DEFAULT_MAX_RETRIES = 5

# #230: 409 (per-repo write-lock contention) clears much faster than
# 429 (rate-cap cooldown). A 2-8s backoff matches the observed clear
# time and keeps test wallclock reasonable; we still cap retries so
# a runaway concurrent writer can't stall forever.
_LOCK_BACKOFF_FLOOR_S = 2.0
_LOCK_BACKOFF_CEIL_S = 8.0

# Body fallback: HF's API includes a human-readable "Retry after N
# seconds" phrase in the JSON error body when the header is absent on
# some edge cases. Match it case-insensitively.
_BODY_RETRY_RE = re.compile(r"retry after\s+(\d+)\s*seconds?", re.IGNORECASE)


def _parse_retry_after_header(value: str | None) -> float | None:
    """Parse ``Retry-After``: integer seconds or HTTP-date.

    Returns ``None`` if absent or unparsable so the caller can fall
    back to body parsing or exponential backoff.
    """
    if not value:
        return None
    value = value.strip()
    # Integer seconds form (e.g. "30").
    try:
        return float(int(value))
    except ValueError:
        pass
    # HTTP-date form (e.g. "Wed, 21 Oct 2026 07:28:00 GMT").
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt is None:
            return None
        delta = dt.timestamp() - time.time()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _parse_retry_after_body(body: str | None) -> float | None:
    """Parse ``"Retry after N seconds"`` from the response body."""
    if not body:
        return None
    m = _BODY_RETRY_RE.search(body)
    if m is None:
        return None
    try:
        return float(int(m.group(1)))
    except ValueError:
        return None


def _exponential_backoff(attempt: int) -> float:
    """Exponential backoff with jitter, clamped to [floor, ceil].

    ``attempt`` is 1-indexed. attempt=1 → ~30s, attempt=2 → ~60s,
    attempt=3+ → 120s ceiling. Adds ±10% jitter to desync concurrent
    retriers (matrix-bake N writers all woken by the same 429 would
    otherwise re-burst together).
    """
    base = _BACKOFF_FLOOR_S * (2 ** (attempt - 1))
    base = min(base, _BACKOFF_CEIL_S)
    jitter = base * 0.1 * (random.random() * 2 - 1)  # ±10%
    return max(_BACKOFF_FLOOR_S, base + jitter)


def _extract_response_pieces(exc: HfHubHTTPError) -> tuple[int | None, dict, str]:
    """Pull (status_code, headers, body_text) from an HfHubHTTPError.

    Defensive: every attribute lookup is guarded because the response
    object's exact shape varies between huggingface_hub versions and
    test fixtures (httpx.Response in real life, SimpleNamespace in
    unit tests).
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", None) or {}
    # httpx.Headers behaves dict-like; SimpleNamespace test fixtures use
    # a plain dict. Both support ``.get``.
    body = ""
    try:
        body = getattr(response, "text", "") or ""
    except Exception:  # noqa: BLE001 — body access can raise on detached responses
        body = ""
    return status, headers, body


def _format_elapsed(seconds: float) -> str:
    """Mirror :func:`mat_vis_baker.progress._format_elapsed` so the
    ``bake_throttle`` line speaks the same time format as the
    ``bake_progress`` lines around it."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs}s"


def _emit_throttle_line(
    *,
    source: str,
    attempt: int,
    max_retries: int,
    wait_s: float,
    elapsed_s: float,
    reason: str = "429",
) -> None:
    """Single-line, structured throttle log — one per retry.

    Format mirrors the contract in :mod:`mat_vis_baker.progress`:
    leading prefix token, ``key=value`` pairs, ``flush=True`` so the
    line streams to GH Actions' live tail rather than buffering until
    process exit. ``reason`` is the HTTP status that triggered the
    retry — ``429`` for rate-throttle, ``409`` for per-repo write-lock
    contention (#230).
    """
    print(
        f"bake_throttle source={source} retry={attempt}/{max_retries} "
        f"reason={reason} wait_s={wait_s:.1f} elapsed={_format_elapsed(elapsed_s)}",
        flush=True,
    )


def _lock_backoff(attempt: int) -> float:
    """Short backoff for 409 per-repo lock contention. Exponential
    with jitter, clamped to ``[_LOCK_BACKOFF_FLOOR_S,
    _LOCK_BACKOFF_CEIL_S]``. Smaller envelope than 429 because the
    lock clears in seconds, not minutes."""
    base = _LOCK_BACKOFF_FLOOR_S * (2 ** (attempt - 1))
    base = min(base, _LOCK_BACKOFF_CEIL_S)
    jitter = base * 0.2 * (random.random() * 2 - 1)  # ±20% — desync writers
    return max(_LOCK_BACKOFF_FLOOR_S * 0.5, base + jitter)


def _is_repo_lock_409(status: int | None, body: str) -> bool:
    """True iff the response is HF's per-repo write-lock 409 (#230).

    The status alone is too broad — 409 also fires on revision
    conflicts and other states we shouldn't silently retry. Match the
    body phrase too so we only auto-retry the case we mean.
    """
    if status != 409:
        return False
    return "another commit operation is in progress" in (body or "").lower()


def _create_commit_with_backoff(
    api: Any,
    *,
    source: str = "unknown",
    max_retries: int = _DEFAULT_MAX_RETRIES,
    _sleep: Any = time.sleep,
    _retry_counter: dict | None = None,
    **kwargs: Any,
) -> Any:
    """Wrap ``api.create_commit(**kwargs)`` with bounded 429 retry.

    On ``HfHubHTTPError`` with ``response.status_code == 429``:

    1. Parse ``Retry-After`` header (integer seconds or HTTP-date).
    2. Fall back to parsing the body for ``"Retry after N seconds"``.
    3. Fall back to exponential backoff with jitter, in
       ``[30s, 120s]``.
    4. Sleep that long, emit a structured ``bake_throttle`` log line,
       and retry.

    Bounded at ``max_retries`` (default 5). Every other exception
    (412 CAS, network, auth, etc.) re-raises unchanged so existing
    handlers — notably the 412 retry loop in
    :mod:`hf_bake_per_file.bake_one_per_file` — keep working.

    ``source`` is plumbed through for log-line attribution; both bake
    and derive call sites pass it. ``_sleep`` is injectable so unit
    tests can run instantly.
    """
    started = time.monotonic()
    last_exc: HfHubHTTPError | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return api.create_commit(**kwargs)
        except HfHubHTTPError as e:
            status, headers, body = _extract_response_pieces(e)
            is_lock_409 = _is_repo_lock_409(status, body)
            if status != 429 and not is_lock_409:
                # Not a throttle and not the per-repo lock — re-raise
                # so 412 / auth / network propagate to the caller's
                # handlers untouched.
                raise
            last_exc = e
            if attempt == max_retries:
                # Out of retries — surface the original error so the
                # caller sees the actual HF response, not a wrapper.
                raise
            if is_lock_409:
                # #230: short backoff for write-lock contention.
                wait_s = _lock_backoff(attempt)
                reason = "409"
                if _retry_counter is not None:
                    _retry_counter["lock_409"] = _retry_counter.get("lock_409", 0) + 1
            else:
                wait_s = _parse_retry_after_header(headers.get("Retry-After"))
                if wait_s is None:
                    wait_s = _parse_retry_after_body(body)
                if wait_s is None:
                    wait_s = _exponential_backoff(attempt)
                wait_s = max(_BACKOFF_FLOOR_S, min(wait_s, _BACKOFF_CEIL_S))
                reason = "429"
            _emit_throttle_line(
                source=source,
                attempt=attempt,
                max_retries=max_retries,
                wait_s=wait_s,
                elapsed_s=time.monotonic() - started,
                reason=reason,
            )
            _sleep(wait_s)
    # Unreachable — the loop either returns or re-raises — but keep a
    # defensive raise so type-checkers don't infer ``None``.
    if last_exc is not None:  # pragma: no cover
        raise last_exc
    raise RuntimeError("create_commit retry loop exited without result")  # pragma: no cover
