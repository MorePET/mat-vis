---
type: issue
state: open
created: 2026-04-16T14:35:29Z
updated: 2026-04-16T14:35:29Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/1
comments: 0
labels: none
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:32.161Z
---

# [Issue 1]: [Schema: one physical material, many appearances (brushed/polished/matte)](https://github.com/MorePET/mat-vis/issues/1)

## Context

From the build123d material-system roadmap thread
([gumyr/build123d#598](https://github.com/gumyr/build123d/issues/598)),
[@jwagenet](https://github.com/jwagenet) flagged a design constraint
we haven't addressed yet:

> A single physical material like aluminum may have more than one
> appearance (brushed, smooth, matte) and more than one mechanical
> property set/alloy (6061, 7075).

This maps onto the mat / mat-vis split cleanly in principle — mat
owns physical properties (per-alloy), mat-vis owns appearances (per
finish) — but mat-vis's current schema assumes a flat per-material
id (`ambientcg/Metal_Brushed_001`) with no formal link to the
underlying physical material.

## The question

How should mat-vis express the relation between:

- **upstream appearance rows** — what the sources actually ship
  (ambientcg has e.g. `Metal_Brushed_001`, `Metal_Polished_002`,
  `Metal_Painted_007`; these are pure appearance, no alloy semantics)
- **physical materials** — the thing a user means when they say
  \"6061 aluminum\" (scalars live in py-mat / `physicallybased`)
- **consumer intent** — a build123d user writes
  `shape.material = \"aluminum 6061 brushed\"` and expects both
  the right density for `compute_mass()` AND the right PBR
  textures for `show()`

## Options

1. **Flat, no linkage.** Keep mat-vis rows independent. Consumer
   picks appearance by id; density lookup is separate. Simplest,
   but shifts the \"what finishes exist for 6061 Al?\" question
   onto the consumer.
2. **Soft tags on appearance rows.** Add `physical_family`
   (`aluminum`), `finish` (`brushed`|`polished`|`matte`) as
   optional metadata columns. Queryable via the SQL shim
   (ADR-0005). Lossy but cheap — upstream tagging is
   heterogeneous.
3. **Hard linkage table.** Publish a separate
   `appearance_to_physical.json` that maps mat-vis ids → physical
   material ids + finish enum. Maintenance cost: every new
   upstream appearance needs a classification pass.
4. **Consumer-layer problem.** mat-vis just ships appearances;
   py-mat (or a new thin layer) holds the family/finish mapping
   and exposes a resolver. Keeps mat-vis lean.

Gut lean is (2) for v1 (soft tags, best-effort, nullable) plus
(4) for the resolver logic — but this needs a proper think before
the schema locks.

## Blocks / related

- Doesn't block initial data release (we can ship flat ids and
  add tags later) but does influence the index JSON schema
  frozen by the first `watch.yml` run.
- Cross-ref to ADR-0001 (index schema) and ADR-0005 (SQL
  queryability of those tags).
- Upstream conversation:
  - [build123d#598 thread root](https://github.com/gumyr/build123d/issues/598)
  - [jwagenet's dataclass proposal](https://github.com/gumyr/build123d/issues/598#issuecomment-2698793213)
    (exact comment anchor approximate — the `MaterialAppearance` /
    `MaterialProperties` sibling-class sketch)

## Decision needed before

First `release.yml` that writes the index schema — once tags
are in the index JSON, renaming them is a migration.
