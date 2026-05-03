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

### Import the committed dashboard

The canonical pipeline dashboard ships with the repo:

- [`docs/observability/dashboard.json`](./observability/dashboard.json)

Import via **Dashboards → New → Import → Upload JSON file**, or via
the API:

```bash
curl -s -u admin:admin -H 'Content-Type: application/json' \
  -X POST http://localhost:3000/api/dashboards/import \
  -d "$(jq '{dashboard: ., overwrite: true, inputs: [], folderUid: ""}' \
        docs/observability/dashboard.json)"
```

Panels:

- **Shard pipeline** — `stream.transform` root spans from
  `service.name=mat-vis-baker` with `label`, `n_total`, `max_workers`,
  `shard_index`, `shard_total`, `outcome`, `n_ok`, `n_failed` as
  columns; `n_failed` colour-coded, `outcome` mapped
  `ok`/`fail_fast`.
- **Progress events** — 30-s rolling `progress` span events
  (`n_ok` / `n_failed` / `rate_per_s`) as a table. Narrow the time
  picker to one trace window to watch a single run.
- **Dagger pipeline** — spans from `service.name~="dagger.*"` (Dagger
  engine emits these natively when `OTEL_EXPORTER_OTLP_ENDPOINT` is
  set). Shows container builds + function invocations surrounding
  the baker spans.

Tested against `grafana/otel-lgtm:latest` (Grafana 12.4, Tempo
default datasource `uid=tempo`). Dashboard pinned to schemaVersion
39 for backwards compatibility with Grafana 10.4+/11.x/12.x.

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

Grab the tailnet hostname (`otel.tail-xxx.ts.net`). Use the
repo-local composite action
[`./.github/actions/otlp-tailnet`](../.github/actions/otlp-tailnet/action.yml)
which wraps the tailnet join + endpoint export:

```yaml
- name: Join tailnet + export OTLP endpoint
  uses: ./.github/actions/otlp-tailnet
  with:
    hostname: otel.tail-xxx.ts.net
    port: "4318"
  env:
    TS_AUTHKEY: ${{ secrets.TS_AUTHKEY }}

- run: uv run mat-vis-baker hf-derive ...   # spans flow automatically
- run: dagger call integration-test ...     # Dagger engine spans too
```

After the step runs, `OTEL_EXPORTER_OTLP_ENDPOINT` is exported to
`$GITHUB_ENV` for the rest of the job — Dagger's engine picks it up
natively (one span per `dagger call` + per container op) and the
baker's OTel SDK picks it up via `src/mat_vis_baker/telemetry.py`.
The action also probes `/v1/traces` so a blocked tailnet ACL or
down collector surfaces as a step warning instead of silently
dropped spans.

The action is **not** wired into `derive.yml` / `bake.yml` — those
workflows stay operator-agnostic. Drop the step into a fork /
override workflow if you want CI spans.

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
- ADR-0010: full-pipeline observability (Dagger + baker → same
  OTLP collector).
- Issue #131: full self-host spec.
- Issue #129: Dagger pipeline (optional, emits to the same OTLP
  receiver).
- Issue #139: this wiring — committed Grafana dashboard + reusable
  Tailscale composite action.
