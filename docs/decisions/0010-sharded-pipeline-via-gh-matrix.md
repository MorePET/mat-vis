# ADR-0010: Sharded derive pipeline via GH Actions matrix

**Status:** Accepted (PR #148, /spike 148 consolidated 2026-04-20)
**Supersedes parts of:** ADR-0009 (derive pipeline decisions — `fail-fast`
logic and single-runner assumption)
**Related:** #134 (shard CLI), #138 (workflow rewrite), epic #140 (Dagger
migration), ADR-0007 (HF substrate), ADR-0008 (tree as SoT).

## Context

`ambientcg-ktx2-2k` needs ~22 h of single-runner work — far above
GitHub's 6 h runner hard cap. Upgrading to larger runners doesn't help
(the cap is per-job, not per-CPU). Two viable substrates:

1. **GitHub Actions matrix fan-out** — every shard is a separate job,
   each ≤ 6 h. Public repos have unlimited Actions minutes, so cost is
   wall-clock and org concurrency (20-slot budget), not dollars.
2. **HF Jobs** — near-data execution, no 6 h cap. Paid after free
   tier, org quotas lower than GH's, dispatch UX immature. Interesting
   for a follow-up but not the first move.

We pick (1). Shape, defaults, and failure modes crystallised through a
/spike 148 round with four adversarial reviewers (GH Actions mechanics,
distributed systems, operator UX, cost).

## Decision

The derive pipeline runs as a four-job DAG on `workflow_dispatch`:

```
setup → derive (matrix[shard-index]) → merge → report
                                           ↓
                                    notify on failure
```

- **`setup`** precomputes the shard-index JSON array + target-tier
  name, exposes them as job outputs. Validates `shard-total` is a
  positive integer before the matrix evaluates `fromJson`.
- **`derive`** matrix expands `shard-index: [0, 1, …, K-1]`. Each job
  invokes `mat-vis-baker hf-derive` or `hf-derive-ktx2` with
  `--shard-index N --shard-total K`. Each shard commits its own tar +
  rowmap to HF atomically (ADR-0007's substrate-level guarantee).
- **`merge`** runs `mat-vis-baker merge-shards` to reassemble shards
  into one canonical tar + rowmap and delete shard leftovers in one
  atomic commit. Skipped when `shard-total=1` (degenerate single-shard
  case).
- **`report`** runs `if: always() && (needs.{setup,derive,merge}.result
  == 'failure')` and fires the `notify-on-failure` composite action.

Defaults: `shard-total=4` (resize), `shard-total=8` (ktx2);
`max-parallel=8`; `fail-fast=false`; `timeout-minutes=350` (10 min
under the 360-min hard cap).

## Consequences

**Good:**
- Removes the 6 h single-runner ceiling. ktx2-2k fits in ~3 h per
  shard × 8 shards = ~3 h wall-clock total.
- Each shard's HF commit is atomic, so partial progress is durably
  stored on HF — a failed shard doesn't waste the others' work. GH's
  "Re-run failed jobs" UI button is shard-selective, cheap, and the
  recommended recovery path (per operator UX review).
- Substrate-agnostic: the matrix shape survives a future swap to
  `dagger call` (#140 epic) or HF Jobs — only the `run:` step changes.

**Bad / accepted tradeoffs:**
- **Non-deterministic tar bytes** (the old implementation). The
  distributed-systems reviewer showed that
  `ThreadPoolExecutor + as_completed` yielded results in completion
  order, so tar member offsets varied between runs of the same shard.
  Fixed in PR #148 by buffering transformed bytes in a dict keyed by
  `(mid, ch)` and flushing to the tar in sorted order. Memory cost
  ≤ ~600 MB for ktx2-2k, well under the 16 GB runner limit.
- **Concurrent-dispatch race.** Without a `concurrency:` group, two
  simultaneous dispatches could race on the same shard paths and
  produce a tar whose bytes come from one producer but whose rowmap
  came from another — silent corruption after merge. Fixed in PR #148
  with `concurrency: derive-${repo-id}-${release-tag}-${source}-${source-tier}-${kind}`
  and `cancel-in-progress=false` so retries queue rather than preempt.
- **Post-merge dirty state.** After a successful merge, a re-dispatch
  re-publishes shard artifacts alongside the canonical tar. Not fixed
  in PR #148; tracked as #155 (guard `hf-derive` against overwriting an
  existing canonical tar unless `--force`).
- **Stale `-of-K'` artifacts after K change** hard-fails merge but
  offers no cleanup path. Tracked as #156 (`--clean-stale`).
- **HF CDN-lag 404s on freshly committed shards** have no retry in
  `_range_read`. Tracked as #153.

**Neutral / deferred:**
- **Operator UX — error-class tagging, source URL logging, aggregated
  failed-channel list** (#157). Noise today; structural fix later.
- **Selective re-dispatch** via `shards-to-run` CSV input (#158). GH's
  "Re-run failed jobs" UI button covers the interactive case; this
  matters for headless/automated retry only.
- **max-parallel=8 vs 4.** Cost reviewer argued 4 (frees team
  throughput on the org's 20-slot budget). Kept at 8 because the
  typical release fires one kind at a time; revisit if the team
  finds other workflows queue-starved.

## Alternatives considered

**A. Keep single-runner + bigger runners.** Dead on arrival — the 6 h
cap is per-job, not per-CPU. `ubuntu-latest-32gb` doesn't help.

**B. Self-hosted runners.** Removes the cap but shifts the cost to
host maintenance and trust boundaries. Not pursued; can be revisited
if wall-clock becomes the bottleneck.

**C. HF Jobs.** Genuinely interesting — runs near the data, no cap —
but paid beyond the free tier, org quotas lower than GH's, dispatch
UX immature. The matrix shape decided here swaps in as the `run:`
step regardless, so this ADR doesn't close the door.

**D. Dagger first (epic #140, issues #136/#137).** Would give local↔CI
parity and OTel out of the box. Dagger engine bootstrap is 60–120 s on
a cold runner — negligible relative to shard length — so the cost is
implementation complexity, not runtime. Decision: Dagger is the
*next* step, layered on top of this matrix. The matrix shape chosen
here is Dagger-ready; swap `uv run mat-vis-baker …` for `dagger call
…` in one PR.

## Implementation notes

- PR #148 lands the workflow + 4 blocker fixes from the spike.
- PR #149 adds an `HF_INTEGRATION=1`-gated live test + cosmetic nits +
  the bake→derive ordering doc note on ADR-0009.
- Follow-up issues #153–#158 carry the non-blocker items to the
  **v0.6.0 — sharded pipeline + Dagger** milestone.
- Dagger migration tracked separately in epic #140.

## Review trail

- Independent review of #146 (shard CLI) — no blockers; 2 items patched
  (empty-shard terminal gate, null-byte hash separator).
- /spike 148 — four reviewers (GH Actions, distributed systems,
  operator UX, cost). Findings consolidated into PR #148 blockers +
  follow-up issues above.
