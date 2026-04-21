# Architectural Decision Records

ADRs for mat-vis. Each captures what was decided, why, what was
rejected, and what would trigger revisiting.

## Current ADRs

1. [0001 — Storage: JSON + .mtlx in git, Parquet + rowmap as Release assets](0001-storage-architecture-json-index-parquet-textures.md)
2. [0002 — Hosting via GitHub Releases; tiered hosting for 8K+](0002-hosting-github-releases-watch-and-pr.md)
3. [0003 — Resolution tiers + category partitioning at 4K+](0003-resolution-tiers-and-partitioning.md)
4. [0004 — Lazy local cache as default access mode](0004-access-modes-lazy-local-cache-default.md)
5. [0005 — ~~SQL shim in clients~~ — superseded by ADR-0001](0005-sql-shim-embedded-in-clients.md)
6. [0006 — Release versioning: client semver + data calver + delta overlays](0006-release-versioning-and-delta-overlays.md)
7. [0007 — Substrate move to HF Datasets + tar container, drop per-category partitioning](0007-substrate-move-to-hf-datasets-and-tar-container.md) — supersedes 0001/0002/0003
8. [0008 — Dataset tree is the source of truth; `release-manifest.json` is an optional convenience snapshot](0008-dataset-tree-as-source-of-truth.md) — partly supersedes 0007
9. [0009 — Derive pipeline: HTTP-range streaming, parallel workers, ICC-strip, fail-fast](0009-derive-pipeline-processing.md)
10. [0010 — Sharded derive pipeline via GH Actions matrix](0010-sharded-pipeline-via-gh-matrix.md) — partly supersedes 0009
11. [0011 — mat_vis curated + upstream.raw mirror (hybrid index)](0011-mat-vis-curated-plus-upstream-mirror.md) — partly reshapes 0001's record surface

## Template

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Superseded by NNNN
- Date: YYYY-MM-DD
- Deciders: @handles

## Context
## Decision
## Consequences
## Upgrade triggers
```
