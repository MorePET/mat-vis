# Release Matrix Design (mat-vis#349)

## Status

**Shipped**: B with DAG-cheap-later structure.

## Problem

Production `gerchowl/mat-vis@v2026.04.2` ships **6 tiers per textured source** (`128/256/512/1k/ktx2-512/ktx2-1k`) plus `scalar` for physicallybased — 14 cells across 3 production phases. Per #306 the canonical declaration was supposed to be the single source of truth, but it only covered the **bake** phase (4 cells). Derive + ktx2 happened via `derive.yml`'s runtime expansion logic with no canonical record.

## Decision

A 3-angle sub-agent spike (schema / data-modeling, workflow / CI ergonomics, future-proofing) returned three different verdicts:

| Angle | Verdict | Core argument |
|---|---|---|
| Schema | **D** (DAG) | "Phase is a property of an *edge*, not a cell. `source_tier=None` is a sum type pretending to be a record." |
| Workflow ergonomics | **A** (single matrix, phase as field) | One CLI, single `release.yml` umbrella collapses 3 dispatches to 1. |
| Future-proofing | **B** (peer modules) | Cleanest stepping-stone to D. Per-phase dataclasses encode validity in the type system. |

Synthesis: **B with DAG-cheap-later structure**. Three peer modules per phase, but each cell shaped like a DAG node (`produces: ArtifactID`, `inputs: tuple[ArtifactID, ...]`) so the eventual D migration is mechanical, not a rewrite.

## Implementation

```
src/mat_vis_baker/
├── _artifact.py                  ← shared ArtifactID (becomes DAG node ID later)
├── release_matrix.py             ← bake phase (kept name for back-compat with #306)
├── derive_matrix.py              ← derive phase (NEW)
├── ktx2_matrix.py                ← ktx2 phase (NEW)
└── release_registry.py           ← cross-phase composer + DAG validator (NEW)
```

### Per-phase modules

Each phase has its own dataclass with phase-specific fields:

- `release_matrix.Cell` — bake phase. `produces` (computed property) yields the `ArtifactID`. No `inputs` (bake fetches from upstream). Back-compat shape preserved for #306 callers.
- `derive_matrix.DeriveCell` — derive phase. `produces: ArtifactID`, `inputs: tuple[ArtifactID, ...]`. Today `inputs` is always single-element; the tuple shape is multi-input ready for future content-addressed substrate or chained derives.
- `ktx2_matrix.Ktx2Cell` — ktx2 phase. Same shape as derive but for transcode operations.

### Cross-phase composer

`release_registry.release_dag(line) -> ReleaseDAG` reads from all three peers and validates topology:

- No two cells (across any phase) `produces` the same artifact.
- Every `inputs` element is `produces` by some cell in the same release.
- No cycles (today's matrix is 2-deep so this is trivially satisfied; the v2 expansion will have multi-input chains).

`ReleaseDAG` exposes both per-phase tuples (workflow plan jobs filter to one phase via `cells_for_phase("derive")`) and a flat `derivations` tuple plus a `by_artifact` dict for graph traversal.

### CLI

`mat-vis-baker matrix list <line> [--phase=bake|derive|ktx2|all]` returns JSON. `--phase=bake` is the default for #306 back-compat. `--phase=all` emits the unified DAG view; consumers that care about edges read the new `inputs` field on each cell.

## DAG-cheap-later property

| Property | Today (B) | v2 D-migration | Cost |
|---|---|---|---|
| Stable artifact identity | `ArtifactID` | Same `ArtifactID` (DAG node ID) | Zero |
| Explicit dependency edges | `inputs: tuple[ArtifactID, ...]` | Same `inputs` field | Zero |
| Cross-phase validation | `release_registry.release_dag()` | Same validator promoted to primary | Zero |
| Per-phase iteration | 3 modules + 3 helpers | `release_dag().cells_for_phase(phase)` | Already present |
| `source_tier=None` smell | **Doesn't exist** — derive cells declare `inputs` explicitly | Same | N/A |

The migration to D is "promote `release_registry` to primary; collapse 3 modules into a single declarations file with `Derivation` nodes" — sized at ~1 day given the structural prep here.

## When to migrate to D

When the per-phase split starts showing seams. Plausible triggers:

- A 4th phase (e.g. `graph_bake` from #284) lands. Adding a 4th peer module is fine but a single declarations file with a `Phase` enum starts looking cleaner.
- Multi-input derivations become routine (e.g. content-addressed substrate where a derived artifact reads multiple parents). The single-`source_tier` mental model breaks down; `inputs: tuple[ArtifactID, ...]` is already there but the per-phase split obscures it.
- Per-cell metadata (`max_bytes`, `upstream_pin`, `signed`, etc.) becomes dense and the per-phase shapes diverge enough that a unified dataclass feels right.

Until then B is sufficient and the cells already have the DAG shape, so the migration cost stays low.

## What this unblocks

- `derive.yml` migration off runtime `sources=all` expansion (sibling to #323's bake.yml migration). Plan job becomes `mat-vis-baker matrix list <line> --phase=derive` → JSON cells → matrix expansion. Tracked as P1 follow-up.
- `validate_release.py` cross-phase coverage check ("substrate has every artifact the matrix declares") — reads `release_dag(line).all_artifacts()`. P1.
- #345 prod-preflight tier-coverage gate becomes meaningfully comprehensive when the matrix is complete (today's gate compares prev-prod cells to tst cells; with a complete matrix it can also assert tst cells match the matrix declaration).

## References

- mat-vis#306 (canonical-declaration epic — extending it; current bake-only state preserved)
- mat-vis#323 (bake.yml migration — derive.yml needs same surgery)
- mat-vis#344 / #347 (post-cut validate gate; tier-missing fix)
- mat-vis#345 / #348 (prod-preflight tier-parity)
- mat-vis#284 (graph_bake / TextureBaker — natural 4th phase)
- mat-vis#66 (manifest signing — phase-agnostic but easy under per-phase metadata)
