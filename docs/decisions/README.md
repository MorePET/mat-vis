# Architectural Decision Records

This directory contains Architectural Decision Records (ADRs) for
`mat-vis`.

An ADR captures a decision that shapes the architecture — what was
decided, why, what was rejected, and what the consequences are. It
exists so that a reader months later can reconstruct the reasoning
and spot the trigger that would overturn it.

We use a light MADR-ish format. Each ADR is one markdown file named
`NNNN-short-dash-separated-title.md`, where `NNNN` is a zero-padded
sequential number.

## Current ADRs

1. [0001 — Storage architecture: JSON indexes + Parquet textures](0001-storage-architecture-json-index-parquet-textures.md)
2. [0002 — Hosting via GitHub Releases; updates via watch-and-PR](0002-hosting-github-releases-watch-and-pr.md)
3. [0003 — Resolution tiers + category partitioning at 4K+](0003-resolution-tiers-and-partitioning.md)
4. [0004 — Lazy local cache as the default client access mode](0004-access-modes-lazy-local-cache-default.md)
5. [0005 — SQL shim embedded in clients; optional GH Pages DuckDB-WASM shell](0005-sql-shim-embedded-in-clients.md)

## Statuses

- **Proposed**: under discussion, not yet in effect
- **Accepted**: in effect
- **Deprecated**: no longer applies but kept for historical context
- **Superseded by NNNN**: replaced by another ADR

## Writing a new ADR

1. Copy the template below into `NNNN-title.md`.
2. Fill it in. Keep each section short — ADRs are not design docs.
3. Open a PR. Discussion happens there, not in the file.
4. Once merged, the ADR is Accepted.

## Template

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Deprecated | Superseded by NNNN
- Date: YYYY-MM-DD
- Deciders: @handles

## Context

What is the forcing function? Who cares? What constraints apply?

## Decision

The decision itself, stated as a single sentence if possible.

## Consequences

What this enables, what it costs, what it rules out.

## Alternatives considered

Named alternatives with one-line rationale for rejection.

## Upgrade trigger

Under what future condition should this ADR be revisited or superseded?
```
