# 0003. Resolution tiers + category partitioning at 4K+

- Status: Accepted
- Date: 2026-04-16
- Deciders: @gerchowl

## Context

MaterialX sources ship each material at multiple texture
resolutions. Sizes scale with the square of the linear resolution:

| Tier | Pixel dims | Per-map PNG size | Per-material total (5 maps avg) |
|---|---|---|---|
| 1K | 1024×1024 | ~500 KB | ~2 MB |
| 2K | 2048×2048 | ~2 MB | ~10 MB |
| 4K | 4096×4096 | ~8 MB | ~35 MB |
| 8K | 8192×8192 | ~30 MB | ~130 MB |

Corpus-wide, that's roughly:

| Tier | All four sources combined |
|---|---|
| 1K | ~5 GB |
| 2K | ~25 GB |
| 4K | ~80 GB |
| 8K | ~300 GB |

Consumers have wildly different needs:

- **Web CAD viewers** (`ocp_vscode`, `three-cad-viewer`): 1K or
  2K is plenty. 4K is a waste of bandwidth and GPU memory on
  most screens.
- **Photorealistic product renders**: 4K is the baseline. 8K for
  close-ups.
- **Research / Monte Carlo**: texture resolution is usually
  irrelevant; they want scalars, not pixels.

One-size-fits-all doesn't work. Neither does forcing users to
choose between "everything at one resolution" bundles that might
be 5 GB or 300 GB.

Additionally, GitHub caps individual Release assets at **2 GB**.
Some per-tier Parquet files exceed that at 4K+.

## Decision

**Publish Parquet files per (source × tier), and partition by
category (plus alphabetic sub-split if needed) at 4K+ to respect
the 2 GB per-asset limit.**

File naming convention:

```
# Fits in one file (<2 GB per source)
mat-vis-<source>-1k.parquet
mat-vis-<source>-2k.parquet

# Split by category at 4K
mat-vis-<source>-4k-metals.parquet
mat-vis-<source>-4k-woods.parquet
mat-vis-<source>-4k-fabrics.parquet
...

# Sub-split alphabetically at 8K for large categories
mat-vis-<source>-8k-metals-A-M.parquet
mat-vis-<source>-8k-metals-N-Z.parquet
```

Size math that drives this:

| Tier | Avg size / material | Materials per 2 GB asset | Partition needed? |
|---|---|---|---|
| 1K | ~2 MB | ~1000 | No — one file per source |
| 2K | ~10 MB | ~200 | No — one file per source (sources ≤ 2000 materials) |
| 4K | ~35 MB | ~60 | Yes — partition by category |
| 8K | ~130 MB | ~15 | Yes — partition by category + alphabetic sub-split |

Partitioning strategy at 4K+ is **category-first** (wood, metal,
stone, fabric, plastic, ...) because it's semantically meaningful,
roughly-balanced in size (categories scale proportionally across
sources), and matches common consumer access patterns ("give me
all the woods").

## Consequences

**Enables**:

- **Consumers download only what they need.** 1K and 2K stay as
  single files per source — trivial curl or lazy range-read
  patterns. 4K+ splits are still transparent via `pyarrow`'s
  Dataset API, which treats a directory of same-schema Parquets
  as one logical table.
- **Predicate pushdown across partitions.** A SQL query like
  `WHERE category='wood'` naturally skips entire non-wood
  Parquet files at 4K+. Free optimization from the partition
  scheme.
- **Fits GitHub's 2 GB per-asset limit** at all tiers.
- **Incremental updates are cheaper at high tiers.** When
  upstream updates only wood materials, we only re-release the
  wood Parquet partitions, not the whole 80 GB 4K bundle.

**Costs**:

- **More files at 4K+.** Per-source, a 4K release might have
  8–12 category partitions; at 8K, potentially 20+. Release
  asset upload steps and listing take longer. Mitigated: the
  full asset list is driven by the build pipeline — no manual
  management.
- **Consumer code needs partition awareness** for raw HTTP
  range-read scenarios. `pyarrow.dataset.dataset(path_glob)`
  handles it. DuckDB `read_parquet('...*-4k-*.parquet')`
  handles it. Bare consumers writing their own byte-range
  logic would need to iterate partitions; unlikely to matter
  at this scale.

**Rules out**:

- **One monolithic Parquet per source** (all tiers combined).
  Would mean forcing 1K users to download 4K data.
- **One massive Parquet across all sources and tiers.**
  Unbounded size, violates per-asset limit, loses the
  semantic structure.
- **Per-material Parquet files.** 3000+ files per release,
  ops nightmare, predicate pushdown loses meaning.
- **Lossy re-compression to fit within one file.** Would
  degrade PBR quality for no real win — tier splitting
  already handles the same concern without quality loss.

## Alternatives considered

- **Hash-based partitioning** (bucket by ID hash into N shards):
  guaranteed balance, but opaque. No semantic skipping. Rejected.
- **First-letter partitioning** (A-M, N-Z): naïve but works.
  Rejected because category partitioning is strictly more
  useful for access patterns (a wood-only render shouldn't
  need to touch the metals partition).
- **Per-tier single file without partitioning**: rejected, see
  "Rules out" above.
- **Mipmap-embedded single-file-per-material** (each file holds
  all resolutions for one material, lazy-load by tier): clever
  but non-standard, loses `pyarrow`/DuckDB compatibility.
  Rejected for format portability.

## Upgrade trigger

Revisit when any of:

1. **GitHub raises the 2 GB per-asset limit.** At 10 GB, the
   4K partition split becomes unnecessary; we'd collapse back
   to one file per (source × tier).
2. **Category taxonomy diverges significantly across sources**.
   If ambientcg starts labeling materials with schemas we
   can't map to polyhaven's categories, category-partitioning
   breaks down. Mitigation: normalize categories in the index
   layer (ADR-0001) during watch/build.
3. **Basis Universal / KTX2 tier added** (per ADR-0001 upgrade
   trigger). Those are ~5× smaller than PNG, so 4K Basis
   likely fits in a single file without partitioning. Would
   add tiers like `4k-ktx2` alongside `4k`.
4. **Users start requesting 8K+ routinely.** Currently 8K is
   specialist. If it becomes default for some workflow, the
   alphabetic sub-split inside categories gets more awkward;
   might need a completely different partitioning scheme
   (e.g. one Parquet per-material at 8K+, accepting the
   ops-cost tradeoff for the size relief).
