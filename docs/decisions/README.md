# Architectural Decision Records

ADRs for mat-vis. Each captures what was decided, why, what was
rejected, and what would trigger revisiting.

## Current ADRs

1. [0001 — Storage: JSON + .mtlx in git, Parquet + rowmap as Release assets](0001-storage-architecture-json-index-parquet-textures.md)
2. [0002 — Hosting via GitHub Releases; tiered hosting for 8K+](0002-hosting-github-releases-watch-and-pr.md)
3. [0003 — Resolution tiers + category partitioning at 4K+](0003-resolution-tiers-and-partitioning.md)
4. [0004 — Lazy local cache as default access mode](0004-access-modes-lazy-local-cache-default.md)
5. [0005 — ~~SQL shim in clients~~ — superseded by ADR-0001](0005-sql-shim-embedded-in-clients.md)

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
