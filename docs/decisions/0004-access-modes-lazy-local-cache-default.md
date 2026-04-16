# 0004. Lazy local cache as the default access mode

- Status: Accepted
- Date: 2026-04-16
- Deciders: @gerchowl

## Context

Three access strategies for fetching a material's textures:

1. **Remote** — range-read per request. No disk. Network on every
   access.
2. **Full prefetch** — download entire tier upfront. 2–80 GB.
   Good for CI / air-gap. Bad first-time UX.
3. **Lazy cache** — fetch on first access, cache locally. Instant
   on reload. Pay bandwidth once per material.

Option 3 is the right default.

## Decision

**mat's texture client defaults to lazy local cache. Prefetch and
no-cache modes are opt-in.**

```python
from pymat import Material

# Default: lazy cache at ~/.cache/mat-vis/
steel = Material("ambientcg/Metal064", tier="2k")

# Explicit cache directory
steel = Material(..., cache_dir="/data/mat-cache")

# Prefetch whole tier (CI, air-gap)
Material.prefetch(source="ambientcg", tier="2k")

# No cache write (low-disk)
steel = Material(..., cache=False)
```

Cache layout:

```
~/.cache/mat-vis/
  ambientcg/
    2k/
      Metal064/
        color.png
        normal.png
        roughness.png
        ao.png
        metadata.json
  .index/
    ambientcg-2k.rowmap.json
```

Content-addressable by `(release_tag, source, tier, material_id)`.
New release tag forces re-fetch on touch.

### Cache hygiene (see mat-vis#2)

- Soft cap (default 5 GB) with visible warning, no silent delete.
- Stale-tag GC prompted on client version upgrade.
- `MAT_VIS_CACHE_DIR`, `MAT_VIS_CACHE_MAX_SIZE` env vars.
- CLI: `mat cache status`, `mat cache prune --older-than 90d`.

## Consequences

**Enables**: instant reload in notebooks; shared cache across
ocp_vscode + Jupyter; ~200 MB cache for typical 20-material usage
vs 25 GB tier bundle.

**Costs**: first-access latency (~200–500 ms RTT); old tags
accumulate without pruning.

## Upgrade triggers

1. **Cache sizes balloon** — add automatic LRU with soft cap.
2. **Multi-user machines** — shared cache with lockfile concurrency.
