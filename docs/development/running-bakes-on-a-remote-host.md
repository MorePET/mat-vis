# Running the bake pipeline on a remote host

The Dagger module (`.dagger/`) gives runner parity: the same
`dagger call bake …` executes byte-identically on a laptop, on any
remote Linux host with a container runtime, and on GitHub Actions
(ADR-0010). This doc covers the "run it on a remote dev host"
path — useful when you want to sidestep GitHub's 6 h per-job cap
or the 20-slot org concurrency budget and bake directly on a box
you control.

Any Linux host with podman (or Docker) + a shell account works.
Substitute `<your-remote-host>` throughout.

## One-time bootstrap

```bash
ssh <your-remote-host>

# Enable the rootless podman socket so Dagger has a daemon to talk to.
systemctl --user enable --now podman.socket

# Clone the repo.
git clone git@github.com:MorePET/mat-vis.git
cd mat-vis
```

### Get `dagger` on PATH — preferred: nix flake devShell

`flake.nix` provides `dagger` (plus `podman`, `uv`, `ruff`, …) as a
devShell package. When nix is available on the host, this is the
canonical path — same pinned version as CI, no out-of-band installs:

```bash
nix develop --extra-experimental-features "nix-command flakes"
# dagger, uv, podman, ruff all on PATH for the duration of the shell
```

### Fallback: direct binary install

If the host doesn't have nix (or the multi-user daemon is wedged —
symptom: `error: opening lock file "/nix/var/nix/db/big-lock":
Permission denied`), fall back to the official Dagger installer.
It pulls a signed binary over HTTPS from `dl.dagger.io`.

```bash
curl -fsSL https://dl.dagger.io/dagger/install.sh \
  | BIN_DIR="$HOME/.local/bin" sh
# ~/.local/bin/dagger --version
```

Either way, point Dagger at podman's rootless socket once:

```bash
echo 'export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock' \
  >> ~/.bashrc
exec bash
```

Export your HF write token for the baker session:

```bash
export HF_TOKEN=...   # from your vault / password manager
```

## Running a smoke bake (scratch dataset)

Default target is `gerchowl/mat-vis-tst` — no `--allow-prod` needed:

```bash
cd ~/mat-vis
git pull

dagger -m .dagger call bake \
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

Drop `--dry-run` when you want to actually land the commit on the
scratch dataset. The atomic-commit guarantees of ADR-0007 apply —
retries are safe.

## Running a production bake

Production (`gerchowl/mat-vis`) requires `--allow-prod=true`:

```bash
dagger -m .dagger call bake \
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
accidentally write to the canonical catalog (ADR-0010 safety rail,
`_guard_prod_target` in `.dagger/src/mat_vis_ci/main.py`).

## Full-matrix bake (long-running)

Each source × tier is one `dagger call bake` invocation. Run them
sequentially or in parallel — the host's resources are the only
cap. Monitor via OTLP if the observability sidecar is up:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<your-otel-host>:4318

for src in polyhaven ambientcg gpuopen; do
  for tier in 1k 2k; do
    dagger -m .dagger call bake \
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

## Why not just GitHub Actions?

GH Actions remains the committed / reproducible path in
`.github/workflows/{bake,derive}.yml` — anyone without dedicated
hardware can re-run a historical bake. For daily bakes a dev host
is typically faster (no cold-start install chain, no 6 h cap
driving matrix fan-out). Both paths call the same
`dagger call …` — that's the whole point of Dagger parity.
