# Running the bake pipeline on anvil-dev

The Dagger module (`.dagger/`) gives us runner parity: the same
`dagger call bake …` executes byte-identically on a laptop, on
`anvil-dev`, and on GitHub Actions. This doc covers the anvil-dev
path — the daily-driver baker that sidesteps the 6 h GH runner cap
and the 20-slot org concurrency budget by simply running locally.

## One-time bootstrap

```bash
# From your laptop, inside your tailnet.
ssh anvil-dev

# (Inside anvil-dev) install dagger to ~/.local/bin — avoids the
# multi-user nix daemon wedge that sometimes trips `nix develop` on
# this VM. Revisit once the system nix is fixed (follow-up issue TBD).
curl -fsSL https://dl.dagger.io/dagger/install.sh \
  | BIN_DIR="$HOME/.local/bin" sh

# Enable the rootless podman socket so Dagger has a daemon to talk to.
systemctl --user enable --now podman.socket

# Clone the repo.
git clone git@github.com:MorePET/mat-vis.git
cd mat-vis

# Point Dagger at podman's rootless socket.
echo 'export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock' \
  >> ~/.bashrc
exec bash
```

Stash your HF write token in the ssh-agent-forwarded keychain or
export it inline (one-shot only):

```bash
export HF_TOKEN=...                 # from bitwarden / vault
```

## Running a smoke bake (scratch dataset)

Default target is `gerchowl/mat-vis-tst` — no `--allow-prod` needed:

```bash
cd ~/mat-vis
git pull

dagger call bake \
  --context=. \
  --source=polyhaven \
  --tier=1k \
  --release-tag=v0.0.1-smoke \
  --hf-token=env:HF_TOKEN \
  --limit=3 \
  --dry-run=true
```

`--dry-run=true` builds the tar locally without pushing, so it's
the fastest way to verify the end-to-end wiring.

Drop `--dry-run` when you want to actually land the commit on
`mat-vis-tst`. The atomic-commit guarantees of ADR-0007 apply —
retries are safe.

## Running a production bake

Production (`gerchowl/mat-vis`) requires `--allow-prod=true`:

```bash
dagger call bake \
  --context=. \
  --source=polyhaven \
  --tier=1k \
  --release-tag=v2026.04.1 \
  --hf-token=env:HF_TOKEN \
  --repo-id=gerchowl/mat-vis \
  --allow-prod=true
```

Without `--allow-prod=true`, the Dagger fn raises before any HF
call is made. Feature branches and smoke-tests therefore can't
accidentally write to the canonical catalog (ADR-0010 safety rail).

## Full-matrix bake (long-running)

Each source × tier is one `dagger call bake` invocation. Run them
sequentially or in parallel GNU-parallel-style — anvil-dev has
enough headroom that a 20 h single-runner run is also fine if you
don't need the matrix shape.

Monitor via OTel: if `OTEL_EXPORTER_OTLP_ENDPOINT` is set (see
`docs/observability/README.md`), Dagger + the baker both emit spans
to Grafana over the tailnet.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://mat-vis-otel.<tailnet>.ts.net:4318

for src in polyhaven ambientcg gpuopen; do
  for tier in 1k 2k; do
    dagger call bake \
      --context=. \
      --source="$src" \
      --tier="$tier" \
      --release-tag=v2026.04.1 \
      --hf-token=env:HF_TOKEN \
      --repo-id=gerchowl/mat-vis \
      --allow-prod=true \
      2>&1 | tee "logs/bake-${src}-${tier}.log"
  done
done
```

## Why not GH Actions?

GH Actions is still the committed / reproducible path in
`.github/workflows/{bake,derive}.yml` — anyone without anvil-dev
access can re-run a historical bake. For daily bakes it's slower
(cold-start, install chain, 6 h cap driving matrix fan-out),
so anvil-dev wins on latency and total wall-clock. Both paths
call the same `dagger call …` — that's the whole point of Dagger
parity (ADR-0010).
