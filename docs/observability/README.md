# Self-hosted OTLP + Grafana on a tailnet

One-command bring-up of an observability stack for the mat-vis baker.
Runs on any Docker/Podman host — laptop, `anvil`, a VPS — and joins
the operator's tailnet as `mat-vis-otel` with `tag:observability`.

## One-time setup

1. **Tailscale ACL** — add `tag:observability` to `tagOwners` with
   owner `["autogroup:admin"]`, grant
   `tag:ci → tag:observability:4318` and
   `autogroup:member → tag:observability:443` (or `:3000` for plain
   HTTP). See `docs/observability.md`.

   > **Known issue #176**: tagged auth-keys are rejected despite
   > correct `tagOwners`. The compose ships with `--advertise-tags`
   > commented out — the sidecar joins untagged, then apply the tag
   > manually at **Machines → mat-vis-otel → Edit machine tags**.
   > That uses a different validation path and works cleanly.

2. **Auth** — stash a Tailscale OAuth client secret or a reusable
   auth-key into the macOS login Keychain:

   ```bash
   security add-generic-password -U \
     -a tailscale-observability \
     -s TS_OBS_AUTHKEY \
     -w "$(bw get password 'tailscale_mat-vis_token')"
   ```

## Bring it up

```bash
just observability-up
```

What it does: reads `TS_OBS_AUTHKEY` from Keychain, exports it to the
compose env, runs `docker compose -f docs/observability/docker-compose.yml up -d`.

## Access

After ~10 s:

- **Grafana UI**: `http://mat-vis-otel.<your-tailnet>.ts.net:3000`
  (admin / admin on first login — change immediately).
- **OTLP/HTTP receiver**: `http://mat-vis-otel.<your-tailnet>.ts.net:4318`
  — wire this into `OTEL_EXPORTER_OTLP_ENDPOINT` for local bakes,
  or into the `otlp-tailnet` composite action for CI.

Find `<your-tailnet>` with `tailscale status --json | jq -r .Self.DNSName`.

## Import the dashboard

The repo ships with a committed Grafana dashboard:

```bash
curl -sf -u admin:admin \
  -H 'Content-Type: application/json' \
  -d "$(jq '{dashboard:., overwrite:true}' docs/observability/dashboard.json)" \
  http://mat-vis-otel.<your-tailnet>.ts.net:3000/api/dashboards/db
```

Or use the Grafana UI → Dashboards → Import → upload
`docs/observability/dashboard.json`.

## Teardown

```bash
just observability-down
```

Because the tailnet node is ephemeral (`TS_AUTHKEY=…?ephemeral=true`),
it deregisters automatically — no stale peer entries.

## Moving the stack to another host

The compose is host-agnostic. To move from your laptop to `anvil`:

```bash
# laptop
just observability-down

# anvil
ssh anvil 'security find-generic-password ... # or pull from Bitwarden'
scp docs/observability/docker-compose.yml anvil:/srv/otel/
ssh anvil 'cd /srv/otel && docker compose up -d'
```

No state persists between hosts by design — the `otel-lgtm-data`
volume is for the collector's recent-traces window only (72 h default).
