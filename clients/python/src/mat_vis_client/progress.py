"""Observability events + ready-made reporters for ``MatVisClient``.

Per mat-vis#312 + #355: ``MatVisClient`` emits :class:`ClientEvent`
instances through an optional ``on_event=`` callback. This module
defines the event taxonomy + four ready-made reporters consumers can
pass directly:

- :func:`silent_reporter` — drops every event (the default if
  ``on_event=`` is omitted; provided as a named explicit choice)
- :func:`log_reporter` — emits via :mod:`logging` at a configurable
  level. The original mat-vis#287 fix (silent ``log.info``) lives
  here as a one-liner consumers opt into instead of fighting.
- :func:`tty_reporter` — pretty progress on stderr when stdout is a
  TTY; degrades to silent in pipes / CI / non-interactive contexts.
- :func:`mcp_reporter` — emits structured dicts via a caller-supplied
  ``emit`` callable; designed for JSON-RPC-shaped surfaces like
  pymat-mcp where the model should receive structured items, not log
  lines.

Why one event channel + ready-made reporters: the joint #312/#355
spike landed on a single ``Callable[[ClientEvent], None]`` signature
to compose download events (#312) and cache events (#355) without
specialised hooks proliferating. Consumers compose: ``on_event=
mcp_reporter(emit)``, ``on_event=tty_reporter()``, etc. — same
``__init__`` surface; renderer is the consumer's choice.

The module is **stdlib-only** — same constraint as the rest of
``mat_vis_client``. ``tty_reporter`` soft-imports ``tqdm``; absent,
falls back to plain stderr lines.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

# Event kinds. Keep the literal explicit so type-checkers narrow the
# string values and the documentation is at the type. The "_event"
# vs "_start"/"_end" naming convention: discrete events have one kind
# (e.g. cache_hit); pairs have _start/_end (download_start/_end).
EventKind = Literal[
    # Download lifecycle (mat-vis#312).
    "download_start",
    "download_end",
    # Cache lifecycle (mat-vis#355).
    "cache_hit",
    "etag_not_modified",
    "cache_check_start",
    "cache_check_end",
    "cache_stale_detected",
    "cache_cleared",
]


@dataclass(frozen=True, slots=True)
class ClientEvent:
    """One observability event from ``MatVisClient``.

    Field semantics (every field optional except ``kind`` so the same
    dataclass spans the lifecycle from cache check → manifest fetch →
    index fetch → texture fetch):

    - ``source``/``material``/``channel``/``tier`` identify the artifact
      the event is about, when applicable.
    - ``url`` is the HF URL involved; useful for log-line debugging.
    - ``bytes_total`` is the ``Content-Length`` for downloads when
      known (HF serves it for PNG fetches).
    - ``bytes_done`` is the running total for in-progress downloads;
      today only emitted at completion (no chunked progress yet).
    - ``tag`` is the pinned release tag; lets per-tag dashboards
      filter cleanly.
    - ``detail`` is the escape hatch for kind-specific data — e.g.
      ``cache_stale_detected`` carries ``{"layouts": [...], "bytes": N}``.

    Frozen + slotted so the per-event dispatch overhead stays sub-µs
    even at high fetch volume. ``__str__`` gives a compact one-liner
    suited for log lines without forcing consumers to render the
    dataclass fields manually.
    """

    kind: EventKind
    source: str | None = None
    material: str | None = None
    channel: str | None = None
    tier: str | None = None
    url: str | None = None
    bytes_total: int | None = None
    bytes_done: int | None = None
    tag: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        """Compact one-line repr suited for log lines."""
        parts = [self.kind]
        if self.source:
            ident = "/".join(p for p in (self.source, self.material, self.channel) if p)
            parts.append(ident)
        if self.tier:
            parts.append(f"tier={self.tier}")
        if self.bytes_total is not None:
            parts.append(f"size={self.bytes_total}")
        return " ".join(parts)


# Type alias used by MatVisClient.__init__'s on_event= kwarg.
OnEvent = Callable[[ClientEvent], None]


def silent_reporter() -> OnEvent:
    """No-op reporter. Default behavior of ``MatVisClient`` when
    ``on_event=`` is omitted. Provided as a named choice so ``client =
    MatVisClient(on_event=silent_reporter())`` reads as deliberate
    rather than accidentally-omitted."""
    return lambda _e: None


def log_reporter(
    *,
    logger: str | logging.Logger = "mat-vis-client",
    level: int = logging.INFO,
) -> OnEvent:
    """Emit each event as a log line at ``level`` on ``logger``.

    Default level is ``INFO``: matches the mat-vis#287 fix's intent
    but routed through an explicit reporter consumers pass in, so
    "log lines are silent unless the consumer configures the logger"
    becomes a deliberate choice rather than an accidental no-op.
    """
    log = logging.getLogger(logger) if isinstance(logger, str) else logger

    def report(event: ClientEvent) -> None:
        log.log(level, "mat-vis %s", event)

    return report


def tty_reporter(
    *,
    stream=None,
    fallback_log_level: int = logging.INFO,
) -> OnEvent:
    """Pretty progress on ``stream`` (default ``sys.stderr``) when
    ``stream`` is a TTY; degrades to :func:`log_reporter` otherwise.

    Renders one line per download with ``\\r``-cleared on completion::

        gpuopen/Aluminum Brushed/color  3.1MB · 1.0s

    Soft-imports ``tqdm`` for richer display when available; falls
    back to plain stderr writes otherwise. The TTY check is
    ``stream.isatty()`` — same heuristic the standard library uses.

    Use case: REPL / Jupyter / interactive CLI. In CI logs the
    fallback log-reporter shape ensures lines aren't ANSI-escaped
    progress bars that ruin the log viewer.
    """
    s = stream or sys.stderr
    if not (hasattr(s, "isatty") and s.isatty()):
        return log_reporter(level=fallback_log_level)

    starts: dict[tuple[str, str | None, str | None], float] = {}

    def render(event: ClientEvent) -> None:
        key = (event.source or "", event.material, event.channel)
        if event.kind == "download_start":
            starts[key] = time.monotonic()
            ident = "/".join(p for p in key if p)
            s.write(f"\r↓ {ident}                    ")
            s.flush()
        elif event.kind == "download_end":
            t0 = starts.pop(key, time.monotonic())
            elapsed = time.monotonic() - t0
            ident = "/".join(p for p in key if p)
            size = event.bytes_total or event.bytes_done or 0
            mb = size / (1024 * 1024)
            s.write(f"\r✓ {ident}  {mb:.1f}MB · {elapsed:.1f}s\n")
            s.flush()
        elif event.kind == "cache_stale_detected":
            layouts = event.detail.get("layouts", [])
            byts = event.detail.get("bytes", 0)
            mb = byts / (1024 * 1024)
            s.write(
                f"⚠ mat-vis cache: legacy layouts found "
                f"({', '.join(layouts)}; ~{mb:.0f}MB). "
                "Run `python -m mat_vis_client cache clear --stale-only` to reclaim.\n"
            )
            s.flush()

    return render


def mcp_reporter(emit: Callable[[dict[str, Any]], None]) -> OnEvent:
    """Emit each event as a JSON-serializable dict via the supplied
    ``emit`` callable. Designed for downstream tools (pymat-mcp) that
    forward events into a structured stream the model can read.

    Output shape mirrors the dataclass fields plus ``"event"``:

        {"event": "download_start", "source": "gpuopen", "material": ...,
         "url": ..., "bytes_total": 3277401, ...}

    No HTTP, no I/O — pure transform. The consumer owns serialization
    + transport (e.g. wrapping in a tool result item).
    """

    def report(event: ClientEvent) -> None:
        payload: dict[str, Any] = {"event": event.kind}
        for f in (
            "source",
            "material",
            "channel",
            "tier",
            "url",
            "bytes_total",
            "bytes_done",
            "tag",
        ):
            v = getattr(event, f)
            if v is not None:
                payload[f] = v
        if event.detail:
            payload["detail"] = event.detail
        emit(payload)

    return report


__all__ = [
    "ClientEvent",
    "EventKind",
    "OnEvent",
    "log_reporter",
    "mcp_reporter",
    "silent_reporter",
    "tty_reporter",
]
