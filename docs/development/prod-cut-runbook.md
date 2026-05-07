# Production Substrate Cut Runbook

Mechanical procedure for cutting a CalVer release of the mat-vis substrate to
`gerchowl/mat-vis` (the production HF dataset).

## Pre-conditions (mat-vis#345 preflight, enforced at workflow level)

The `bake.yml` workflow refuses any `--allow-prod=true` dispatch unless three
gates pass. Each is independent and the operator gets a structured failure
message pointing at the specific blocker.

### 1. tst-prior gate

The release-tag must already exist on `gerchowl/mat-vis-tst`. The check is a
single HEAD on the manifest URL.

> "ALWAYS E2E on tst before prod" made mechanical at the workflow boundary,
> not human discipline.

If it fails: dispatch a tst bake at the same release-tag first, wait for green,
then re-dispatch the prod cut.

```sh
# Run the same line on tst (no --allow-prod needed):
gh workflow run bake.yml \
  -f line=v2026.04 \
  -f release-tag=v2026.04.4 \
  -f repo-id=gerchowl/mat-vis-tst
```

### 2. tier-coverage gate

The tst manifest's `(source, tier)` cells at the release-tag must be a
**superset** of the previous prod release's cells. Catches the matrix-coverage
class of bug — e.g. matrix declares only `1k` but prod was shipping
`128/256/512/1k/ktx2-512/ktx2-1k`.

If it fails: either bake the missing tier(s) onto tst, OR mark them as
deprecated (gate 3 below).

### 3. Deprecation escape hatch (agent-resistant)

When a tier is being intentionally retired, declare it explicitly:

```sh
gh workflow run bake.yml \
  -f line=v2026.04 \
  -f release-tag=v2026.04.4 \
  -f repo-id=gerchowl/mat-vis \
  -f allow-prod=true \
  -f previous-prod-tag=v2026.04.3 \
  -f deprecate-cells='[["gpuopen", "128"]]'
```

Each declared cell **must** have a corresponding open GH issue with the
**`tier-deprecation-approved` label**. The workflow runs
`scripts/verify_deprecation_issues.py` as the first step and fails the
preflight if any declared cell lacks an approved issue.

#### Why this is agent-resistant

The `tier-deprecation-approved` label can only be applied or removed by repo
maintainers (`triage` role or higher). Agents / contributor accounts can:

- ✅ open issues, comment, edit text
- ✅ open PRs that reference labels in code
- ❌ apply protected labels at the repo level — that's enforced by GitHub's
  permission model, not by code

So the gate's signal is a permission boundary GitHub itself enforces. An
agent literally cannot pass this check without the `triage` role, which bot
accounts shouldn't have.

#### Operator workflow for a tier deprecation

1. Open a GH issue per `(source, tier)` cell being retired. Title must
   contain both the source and tier strings (e.g. `Drop gpuopen 128 —
   superseded by ktx2-1k for v2026.05`).
2. A maintainer applies the `tier-deprecation-approved` label.
3. Re-dispatch the prod cut with `deprecate-cells='[[…]]'` listing the cells.

Step 2 is the human-in-the-loop signal that an automated agent (including
`Claude Code`, an unauthorized bot, or a contributor accidentally writing a
script) cannot fake.

## Tst-first dress rehearsal (always)

Even when the preflight gates pass, a tst bake at the release-tag is the
only proof the substrate is shippable. The tst dataset is at
`gerchowl/mat-vis-tst` and accepts unbounded bakes (`limit=0`).

```sh
gh workflow run bake.yml \
  -f line=v2026.04 \
  -f release-tag=v2026.04.4 \
  -f repo-id=gerchowl/mat-vis-tst
```

After tst goes green, verify on HF directly:

```sh
curl -sL https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve/v2026.04.4/release-manifest.json | jq '.sources | keys'
```

## Prod cut

Once the tst dispatch is green and verified:

```sh
gh workflow run bake.yml \
  -f line=v2026.04 \
  -f release-tag=v2026.04.4 \
  -f repo-id=gerchowl/mat-vis \
  -f allow-prod=true \
  -f previous-prod-tag=v2026.04.3
```

The preflight job runs all three gates; if green, the bake matrix kicks off.

## After the cut

- `release-validate.yml` cron (06:30 UTC daily) compares the new tag against
  the previous tag (#344 fix: catches `tier-missing-from-current` regressions
  too).
- Bump `clients/python/src/mat_vis_client/client.py:DEFAULT_TAG` to the new
  release in a follow-up PR so tag-less clients pick it up.

## References

- mat-vis#345 — this preflight design + agent-resistance reasoning.
- mat-vis#344 — `validate_release` tier-missing blind-spot fix; the post-cut
  daily cron now catches the inverse failure mode.
- mat-vis#306 — release-matrix as canonical (source × tier) source-of-truth.
- ADR-0012 — per-file substrate; per-cell atomic HF commits.
