"""OpenTelemetry instrumentation for the baker (opt-in, self-hosted).

Wires OpenTelemetry SDK + OTLP HTTP exporter around bake / derive
operations. Active when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set
(e.g. ``http://grafana.tail-xxx.ts.net:4318``); a complete no-op
otherwise. Use with the free-tier stack documented in #131:

    podman run -d --name otel-lgtm \\
      -p 4318:4318 -p 3000:3000 \\
      grafana/otel-lgtm:latest

Emits:

- one root span per high-level op (``hf-bake`` is the only live op
  today; the tar-era ``hf-derive`` / ``hf-derive-ktx2`` were retired
  in #189) with source/tier/release attrs;
- a ``stream.transform`` child span with the ok/failed counters
  attached as attributes when the pool drains;
- counter events every 30 s with a ``progress`` marker.

The sdk / exporter deps live under the ``observability`` optional
extra so non-observing consumers don't pay for them.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger("mat-vis-baker.telemetry")

_tracer = None  # type: ignore[var-annotated]
_initialised = False


def _init_once() -> None:
    """Initialise the OTLP tracer on first use. No-op if the endpoint
    env var is unset or the SDK isn't installed."""
    global _tracer, _initialised
    if _initialised:
        return
    _initialised = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        log.debug("OTEL_EXPORTER_OTLP_ENDPOINT not set — telemetry disabled")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT set but opentelemetry SDK missing — "
            "install `mat-vis[observability]` to enable"
        )
        return

    resource = Resource.create(
        {
            "service.name": "mat-vis-baker",
            "service.version": os.environ.get("MAT_VIS_BAKER_VERSION", "dev"),
            "deployment.environment": (
                "ci" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"
            ),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("mat_vis_baker")
    log.info("telemetry enabled → %s", endpoint)


@contextmanager
def span(name: str, **attrs: object) -> Iterator[object]:
    """Context manager that opens an OTel span (no-op if telemetry off).

    Yields the span handle so callers can set additional attributes
    mid-operation (e.g. final ok/failed counters)."""
    _init_once()
    if _tracer is None:
        yield _NoopSpan()
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            s.set_attribute(k, _coerce(v))
        yield s


class _NoopSpan:
    def set_attribute(self, *_args, **_kw) -> None: ...
    def add_event(self, *_args, **_kw) -> None: ...


def _coerce(v: object) -> object:
    """OTel attrs want str/int/float/bool or sequences of same."""
    if isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, (list, tuple)):
        return [_coerce(x) for x in v]
    return str(v)
