"""Streaming progress lines for ``hf-bake`` / ``hf-derive`` (#217).

Surface live "should-vs-is" progress as single-line, structured stdout
records that GitHub Actions can render in the live tail of a step (not
just after step exit). The format is a public contract — downstream
consumers grep / parse these lines, so the field order and key names
must stay stable.

Three line shapes:

- ``bake_plan`` / ``derive_plan`` — emitted once, at the top of a run,
  after the catalog has been resolved. Tells the operator how big the
  job is up front: ``total_materials``, ``expected_files``, the target
  repo, the release tag.

- ``bake_progress`` / ``derive_progress`` — emitted at the end of every
  successful batch commit. Contains a rolling 3-batch rate to smooth
  out first-batch warm-up bias, plus an ETA derived from the same
  rolling rate.

- ``bake_done`` / ``derive_done`` — final summary line.

All emit through ``print(..., flush=True)`` so the line reaches the
parent process / GitHub Actions live log within the OS pipe-flush
window (~milliseconds), not buffered until the Python process exits.
The Dagger baker container additionally sets ``PYTHONUNBUFFERED=1``;
this module's flush is the belt to that suspenders.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from dataclasses import dataclass, field

# Rolling-rate window: smooth over the last N batches. Three is enough
# to dampen first-batch warm-up (cold caches, branch creation, etc.)
# without lagging meaningfully behind real throughput changes.
_RATE_WINDOW = 3


def _format_elapsed(seconds: float) -> str:
    """Render seconds as ``<H>m<S>s`` — minutes always shown, seconds
    rounded to int. Matches the format the issue specifies."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs}s"


def emit_bake_plan(
    *,
    source: str,
    tier: str,
    total_materials: int,
    expected_files: int,
    release_tag: str,
    repo_id: str,
    kind: str = "bake",
) -> None:
    """Emit the one-shot ``<kind>_plan`` line. ``kind`` is ``bake`` or
    ``derive``; the line shape is otherwise identical."""
    print(
        f"{kind}_plan source={source} tier={tier} "
        f"total_materials={total_materials} expected_files≈{expected_files} "
        f"release_tag={release_tag} repo={repo_id}",
        flush=True,
    )


@dataclass
class ProgressTracker:
    """Rolling-window progress accountant.

    One instance per run. ``record_batch`` is called at the end of every
    successful batch commit; ``emit_progress`` writes a single structured
    line to stdout using the rolling rate from the last ``_RATE_WINDOW``
    batches. ``emit_done`` writes the final summary.
    """

    source: str
    tier: str
    total_materials: int
    kind: str = "bake"  # "bake" | "derive"
    started_monotonic: float = field(default_factory=time.monotonic)

    done: int = 0
    batches: int = 0
    commits: int = 0
    bytes_pushed: int = 0
    # Rolling window: each entry is (monotonic_ts, materials_in_batch).
    _window: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=_RATE_WINDOW))

    def record_batch(self, *, materials: int, bytes_added: int, commits: int = 1) -> None:
        """Record one durable batch commit. ``commits`` defaults to 1
        (the common case — one HF commit per batch); callers that
        bundle multiple commits in a single batch can override."""
        self.done += materials
        self.batches += 1
        self.commits += commits
        self.bytes_pushed += bytes_added
        self._window.append((time.monotonic(), materials))

    def _rolling_rate_per_min(self) -> float:
        """Materials per minute over the rolling window. Returns 0.0
        until at least two samples are available — a single sample
        gives no time delta."""
        if len(self._window) < 2:
            return 0.0
        oldest_ts, _ = self._window[0]
        newest_ts, _ = self._window[-1]
        # Sum materials EXCLUDING the oldest sample's count: rate over
        # the interval [oldest_ts, newest_ts] is "what landed AFTER
        # the oldest tick" / "elapsed since then".
        materials_in_window = sum(m for _, m in list(self._window)[1:])
        elapsed = newest_ts - oldest_ts
        if elapsed <= 0:
            return 0.0
        return materials_in_window * 60.0 / elapsed

    def _eta_minutes(self) -> int:
        """Minutes remaining at the rolling rate. 0 if rate is 0 (avoid
        divide-by-zero) or the run is already complete."""
        rate = self._rolling_rate_per_min()
        remaining = max(0, self.total_materials - self.done)
        if rate <= 0 or remaining == 0:
            return 0
        return int(round(remaining / rate))

    def emit_progress(self) -> None:
        """Emit a single ``<kind>_progress`` line. Called at the end of
        every successful batch commit."""
        elapsed = time.monotonic() - self.started_monotonic
        pct = 0
        if self.total_materials > 0:
            pct = int(round(100.0 * self.done / self.total_materials))
        rate = self._rolling_rate_per_min()
        eta_m = self._eta_minutes()
        bytes_mib = self.bytes_pushed / (1024 * 1024)
        print(
            f"{self.kind}_progress source={self.source} "
            f"done={self.done}/{self.total_materials} ({pct}%) "
            f"batches={self.batches} commits={self.commits} "
            f"bytes_pushed={bytes_mib:.1f}MiB "
            f"elapsed={_format_elapsed(elapsed)} "
            f"rate={rate:.1f}mat/min eta={eta_m}m",
            flush=True,
        )

    def emit_done(
        self,
        *,
        ok: int,
        failed: int,
        skipped_preflight: int,
    ) -> None:
        """Emit the final ``<kind>_done`` summary line."""
        elapsed = time.monotonic() - self.started_monotonic
        print(
            f"{self.kind}_done source={self.source} tier={self.tier} "
            f"ok={ok} failed={failed} skipped_preflight={skipped_preflight} "
            f"elapsed={_format_elapsed(elapsed)}",
            flush=True,
        )


def enable_line_buffering() -> None:
    """Reconfigure ``sys.stdout`` for line buffering — defensive belt
    on top of ``PYTHONUNBUFFERED=1``. Safe to call multiple times; a
    no-op on streams that don't support ``reconfigure`` (e.g. some
    pytest captures)."""
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        # ValueError can fire on already-detached streams; AttributeError
        # on stdout objects that don't expose reconfigure (rare, but
        # captured stdouts in some test runners).
        pass
