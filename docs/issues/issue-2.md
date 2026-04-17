---
type: issue
state: open
created: 2026-04-16T14:58:32Z
updated: 2026-04-16T14:58:32Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/2
comments: 0
labels: none
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:31.797Z
---

# [Issue 2]: [Cache hygiene: soft cap + warning, stale-tag GC on upgrade, CLI affordances](https://github.com/MorePET/mat-vis/issues/2)

## Context

ADR-0004 introduced the default lazy local cache at
`~/.cache/mat-vis/`, and explicitly punted on invalidation:

> Cache invalidation is the user's problem. Old release tags
> accumulate over time. Mitigation: ship
> `mat_vis.cache.prune(keep_tags=[\"current\", \"previous\"])` and
> document it.

That stance is weak for a cache that could realistically reach
tens of GB at 4K or 8K. The \"unbounded + manual prune\"
pattern is what pip, Hugging Face, Docker, etc. do, and it has
worn out its welcome — users report 100+ GB cache surprises
regularly.

We can do better without being user-hostile (silent eviction
of user-fetched data is its own class of bug).

## Proposal

Layered policy — warn first, GC only with explicit consent,
expose plumbing for power users.

### 1. Soft cap with visible warning (no silent delete)

Default cap: **5 GB** (configurable). On cache write, check
total size; if over cap, emit a visible log warning:

```
mat-vis: cache is 7.2 GB (soft cap 5 GB).
         Run `mat-vis cache prune` to clean up stale entries.
```

Never auto-delete without consent — respects the principle
that you don't silently discard data the user paid bandwidth
for.

### 2. Stale-tag GC on client version upgrade

When the installed `mat-vis` version increases, first cache
access prompts (or logs prominently, in non-interactive
contexts):

```
mat-vis: found 4.1 GB of cache entries for release tags older
         than v2026.04.0 (current). Delete? [y/N]
```

Tags that don't match the currently-installed client are by
definition regenerable — losing them costs a re-download, not
authored work. Safe to ask.

Non-interactive contexts (CI, `pty=False`) default to
**no delete + warn**. Opt in via env var
`MAT_VIS_AUTO_PRUNE_STALE=1`.

### 3. CLI affordances

```
mat-vis cache status                         # bytes by (tag, source, tier)
mat-vis cache prune --older-than 90d
mat-vis cache prune --keep-tags current,previous
mat-vis cache prune --tag v2026.04.0
mat-vis cache clear --all
mat-vis cache set-cap 10GB
```

Matches patterns from `docker`, `pip`, `uv` cache subcommands.

### 4. Respect XDG / platform conventions

- Linux: `$XDG_CACHE_HOME/mat-vis/` (fallback `~/.cache/mat-vis/`)
- macOS: `~/Library/Caches/mat-vis/` **or** `~/.cache/mat-vis/`
  — document which; most scientific Python uses the XDG path
  even on macOS.
- Windows: `%LOCALAPPDATA%\\mat-vis\\Cache\\`

Plus `MAT_VIS_CACHE_DIR` env var for explicit redirect (HPC
scratch disks, shared research volumes).

### 5. Configurable cap via env var

- `MAT_VIS_CACHE_MAX_SIZE=50GB` — raise the warning threshold
  (CI, HPC, disk-rich laptops).
- `MAT_VIS_CACHE_MAX_SIZE=0` — disable size checks entirely.

### What this explicitly does NOT do

- **No silent LRU eviction.** Once a user fetched a material,
  silently deleting it between notebook runs breaks
  reproducibility in a way that's painful to diagnose. Warn
  loudly, let the user decide.
- **No automatic background GC daemon.** Stays in-process,
  explicit, predictable.
- **No telemetry.** Cache size never leaves the user's
  machine.

## Acceptance

- [ ] Soft cap warning fires on writes past threshold, once per
      process session (not spammy).
- [ ] `mat-vis cache status` prints a breakdown by `(tag,
      source, tier)` in human-readable bytes.
- [ ] `mat-vis cache prune` subcommands work and are
      idempotent.
- [ ] Client version bump triggers the stale-tag prompt
      (interactive) or warning (non-interactive).
- [ ] `MAT_VIS_CACHE_DIR`, `MAT_VIS_CACHE_MAX_SIZE`,
      `MAT_VIS_AUTO_PRUNE_STALE` env vars documented in README.
- [ ] Documented platform-specific default paths (Linux/macOS/
      Windows).
- [ ] Tests cover: soft-cap warning, prune filters, env var
      overrides.

## Amends

ADR-0004 — specifically the \"cache invalidation is the user's
problem\" clause. Worth writing this up as either a supersede-
style revision or an amendment note in the existing ADR once
the implementation lands.

## Related

- [MorePET/mat-vis#1](https://github.com/MorePET/mat-vis/issues/1)
  — schema for appearance ↔ physical material (separate axis,
  but both touch the first release schema).
- [MorePET/mat#34](https://github.com/MorePET/mat/issues/34)
  — extras refactor; clarified that `[viz]` is about feature
  surface, not offline mode. Cache hygiene lives entirely in
  mat-vis.
