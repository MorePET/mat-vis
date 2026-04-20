# Observability — self-hosted, no subscription

The baker emits OpenTelemetry spans around bake/derive pipelines.
Out-of-the-box: no-op. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to an OTLP
receiver and spans flow.

## Local dashboard in 60 seconds

```bash
podman run -d --name otel-lgtm \
  --restart=always \
  -p 4318:4318 -p 3000:3000 \
  -v otel-lgtm-data:/data \
  grafana/otel-lgtm:latest
```

One image: Grafana + Tempo (traces) + Loki (logs) + Mimir/Prometheus
(metrics). Open <http://localhost:3000>, default login
`admin` / `admin`.

## Point the baker at it

Install the extra:

```bash
uv sync --all-extras
# or: pip install 'mat-vis[observability]'
```

Run with the endpoint set:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  mat-vis-baker hf-derive polyhaven 512 /tmp/d \
  --source-tier 1k --release-tag v2026.04.1 --repo-id gerchowl/mat-vis-tst
```

Spans land in Grafana Tempo under the `mat-vis-baker` service.
Look for `stream.transform` root spans with `n_ok`, `n_failed`,
`outcome` (`ok`/`fail_fast`), and per-30-s `progress` events.

## CI via Tailscale (no public endpoint)

Host machine (laptop, Pi, home server) on your tailnet:

```bash
# one-shot: expose Grafana + OTLP over tailnet
tailscale serve --bg --https=3000 http://localhost:3000
```

Grab the tailnet hostname (`otel.tail-xxx.ts.net`). In the workflow:

```yaml
- uses: tailscale/github-action@v2
  with:
    authkey: ${{ secrets.TS_AUTHKEY }}
    tags: tag:ci
- run: |
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel.tail-xxx.ts.net:4318
    mat-vis-baker hf-derive ...
```

The runner joins your tailnet briefly, emits spans, leaves. No
public endpoint, no inbound firewall holes.

## What gets emitted

- `stream.transform` root span per derive: `label`, `n_total`,
  `max_workers`, plus `outcome`, `n_ok`, `n_failed` on close.
- `progress` events every ~30 s with rolling counters + throughput.
- Fail-fast path sets `outcome=fail_fast` and attaches the first
  error as a span event.

## Cost

Zero subscription. Self-hosted container runs anywhere with an OCI
runtime. Tempo defaults to 72 h trace retention (enough for
debugging; not an audit trail).

## References

- ADR-0009: derive pipeline decisions (fail-fast, sliding window,
  telemetry hooks).
- Issue #131: full self-host spec.
- Issue #129: Dagger pipeline (optional, emits to the same OTLP
  receiver).
