# 0003. Resolution tiers + category partitioning at 4K+

- Status: Accepted
- Date: 2026-04-16
- Deciders: @gerchowl

## Context

Upstream sources ship materials at multiple resolutions. Sizes
scale with the square of the linear dimension:

| Tier | Per-map PNG | Per-material (5 maps) | Corpus total |
|---|---|---|---|
| 1K | ~500 KB | ~2 MB | ~5 GB |
| 2K | ~2 MB | ~10 MB | ~25 GB |
| 4K | ~8 MB | ~35 MB | ~80 GB |
| 8K | ~30 MB | ~130 MB | ~300 GB |

Different consumers need different tiers:

- **ocp_vscode / three-cad-viewer**: 1K–2K (screen-limited).
- **Product renders**: 4K baseline, 8K for close-ups.
- **Research / Monte Carlo**: scalars only, no textures.

GitHub caps Release assets at **2 GB**. Some per-tier Parquets
exceed that at 4K+.

## Decision

**Publish Parquet files per (source × tier). Partition by category
at 4K+ to respect the 2 GB limit.**

```
mat-vis-<source>-1k.parquet              # one file per source
mat-vis-<source>-2k.parquet

mat-vis-<source>-4k-metals.parquet       # split by category
mat-vis-<source>-4k-woods.parquet

mat-vis-<source>-8k-metals-A-M.parquet   # category + alphabetic
mat-vis-<source>-8k-metals-N-Z.parquet
```

| Tier | Materials per 2 GB | Partition? |
|---|---|---|
| 1K | ~1000 | no |
| 2K | ~200 | no |
| 4K | ~60 | yes — by category |
| 8K | ~15 | yes — category + alpha sub-split |

Category-first partitioning is semantically meaningful and matches
access patterns ("give me all the woods").

Future: KTX2 tier (~5× smaller than PNG) would push the partition
boundary up — 4K-KTX2 likely fits in one file per source.

## Consequences

**Enables**: download only what you need; rowmap handles partition
transparency; incremental updates at high tiers.

**Costs**: more Release assets at 4K+ (8–20 per source). Mitigated
by build pipeline automation.

## Upgrade triggers

1. **2 GB limit raised** — collapse partitions.
2. **Category taxonomy diverges across sources** — normalize in
   the index layer.
3. **KTX2 tier added** — re-evaluate partition boundaries.
