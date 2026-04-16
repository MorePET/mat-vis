# 0004. Lazy local cache as the default client access mode

- Status: Accepted
- Date: 2026-04-16
- Deciders: @gerchowl

## Context

Given the storage layout of ADR-0001 (JSON indexes + Parquet
texture bundles) and the hosting layout of ADR-0002 (GitHub
Release assets, CDN-fronted), a consumer asking for a single
material has three plausible access strategies:

1. **Remote random access** — HTTP range-read the Parquet
   footer, find the target row's byte offset, range-read the
   row. Minimal bytes over the wire; requires a network round
   trip per material; no offline fallback.
2. **Full prefetch** — download the entire
   `mat-vis-<source>-<tier>.parquet` once, read locally from
   then on. Zero network per read after the first; pays the
   full tier cost up-front (2–80 GB depending on tier); ideal
   for CI pipelines and air-gapped environments.
3. **Lazy local cache** — on first access to a material, fetch
   its Parquet row via HTTP range read AND store the PNG
   bytes under a local cache directory keyed by
   `(source, tier, material_id, map_name)`. Subsequent
   accesses of the same material from the same process or
   any other process read directly from disk.

Most real client sessions (Jupyter notebooks, `ocp_vscode`
viewers, ad-hoc scripts) want exactly one thing: "give me
Stainless Steel Brushed at 2K". They don't want to download
25 GB of 2K. They also don't want to pay a network round trip
on every reload of the notebook cell or every shape re-render.

Option 1 punishes iteration. Option 2 punishes first-time
users. Option 3 is the right default.

## Decision

**Client libraries default to lazy local cache mode, with
opt-ins for the other two strategies.**

Concretely for the Python client (`py-materials` /
`mat-vis` — analogous shape in Rust/JS clients):

```python
# Default — lazy cache at ~/.cache/mat-vis/
material = mat_vis.get("ambientcg/Metal064", tier="2k")

# Explicit cache directory
material = mat_vis.get(..., cache_dir="/data/mat-cache")

# Prefetch the whole tier (CI, air-gapped)
mat_vis.prefetch(source="ambientcg", tier="2k")

# Direct range-read, no cache write (low-disk environments)
material = mat_vis.get(..., cache=False)
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
        metadata.json           # scalars copy
  .index/
    ambientcg-2k.footer         # cached Parquet footer (~few KB)
    ambientcg-2k.rowmap.json    # id → (offset, length) lookup
```

The cache is content-addressable by `(release_tag, source,
tier, material_id)`. A new release tag forces re-fetch of any
material the user touches; older-tag files stay on disk until
the user prunes.

There's also a separate decoded-image cache
(`cache_decoded=True`, default off) for the pathological case
of reloading the same material 100× in the same process:

```python
material = mat_vis.get(..., cache_decoded=True)
# Also stores the numpy array alongside the PNG bytes
# — saves ~5 ms × number of reloads, costs 8× disk per material
```

Default is off because:

- Decode is ~5 ms per 1K texture, cheap once per session.
- Decoded matrices are ~8× bigger on disk than the PNGs they
  came from.
- Most consumers load a material once per notebook cell and
  move on.

Opt-in when you know your workflow repeatedly reloads the
same materials — Monte Carlo sweeps, parametric studies.

## Consequences

**Enables**:

- **Best default UX across consumer types.** Notebook user
  gets their material in under a second on first call,
  instantly on reload. CI pipeline can `prefetch()` once in a
  setup step and then run offline. Air-gapped user downloads
  the Parquet file directly via `curl` and points
  `cache_dir` at it.
- **Cache coherence via release tags.** Pinning
  `mat-vis==2026.04.0` means cache keys include `2026.04.0`,
  so upgrading the package never silently serves stale bytes.
- **No size blowup for casual users.** A researcher who
  accesses 20 materials at 2K has a ~200 MB cache, not a
  25 GB tier bundle.
- **Shared cache across tools.** `ocp_vscode` and a Jupyter
  notebook sharing `~/.cache/mat-vis/` read the same files —
  no duplication.

**Costs**:

- **Cache invalidation is the user's problem.** Old release
  tags accumulate over time. Mitigation: ship
  `mat_vis.cache.prune(keep_tags=["current", "previous"])`
  and document it.
- **First-access latency.** The first fetch of a material
  pays the range-read RTT (~200–500 ms on a typical
  residential connection). Subsequent accesses are disk-fast.
  Prefetch path exists for users who want to amortize this.
- **Filesystem assumptions.** The cache layout assumes case-
  sensitive, Unicode-safe paths. Fine on Linux/macOS;
  Windows NTFS handles it; Windows FAT or exotic filesystems
  could have trouble. Not a near-term concern.

**Rules out**:

- **No-cache-ever default.** Would punish every notebook
  reload with a round trip. Rejected.
- **Mandatory full prefetch.** Forces 25 GB download on a
  user who wants one material. Rejected.
- **In-memory-only cache.** Doesn't survive process restart;
  notebook kernel restarts re-download. Rejected.

## Alternatives considered

- **HTTP cache headers + `requests_cache`** (transparent
  via Last-Modified / ETag): would work for full-file fetches
  but doesn't play well with range-read semantics, and
  doesn't give us tag-pinned immutability. Rejected.
- **Single monolithic cache file** (one SQLite DB for all
  cached materials): nice invariants, but torture for users
  who want to prune or rsync selectively. Rejected.
- **Cache in the package install dir**: pollutes site-
  packages, breaks when package is reinstalled, fights
  package managers. Rejected.

## Upgrade trigger

Revisit when any of:

1. **Cache sizes balloon in practice.** If typical users
   end up with 10+ GB of cache because they routinely touch
   large slices of the corpus, add automatic LRU pruning
   with a configurable soft cap.
2. **Multi-tenant / multi-user machines become common.** If
   several users on one host want to share a cache, add an
   explicit shared-cache mode with lockfile-based concurrency
   control. Current default of `~/.cache/mat-vis/` is
   single-user by design.
3. **Decode cost dominates for some use case.** If someone
   reports that 5 ms × N reloads is actually painful in their
   workflow, revisit whether `cache_decoded` should become
   the default or get its own always-on in-memory tier.
