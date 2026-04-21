# ADR-0011: mirror upstream metadata verbatim alongside normalized fields (hybrid index)

- Status: Accepted
- Date: 2026-04-20
- Deciders: @gerchowl
- Supersedes: none. Partly reshapes ADR-0001's index-record surface.
- Milestone: v0.6.0 — sharded pipeline + Dagger

## Status

**Accepted design, v0.6.0.** Produced by /spike review round (2026-04-20): four parallel fresh-agent reviews (data-modeling, consumer-DX, ops-burden, storage/cost), consolidated with user decisions on the four open questions. ADR-0011 draft to follow at `docs/decisions/0011-mat-vis-curated-plus-upstream-mirror.md`.

## Decision (locked)

Two-layer index record. **Clean break at v0.6.0** — no back-compat aliases.

### Layer 1 — `mat_vis`: curated, unified, semver-stable

The *only* query surface. `search()` / `index()` / `filter()` look here and nowhere else. Missing values are `null`, never absent (stable key set).

```json
"mat_vis": {
  "name": "Bricks 097",
  "category": "ceramic",
  "tags": ["brick", "red", "damaged"],
  "description": "...",
  "physical": {
    "dimensions_m": [0.5, 0.5, 0.02],
    "max_resolution_px": [1024, 1024]
  },
  "pbr": {
    "color_rgb": null,
    "roughness": null,
    "metalness": null,
    "ior": null,
    "specular_f0": null,
    "transmission": null,
    "complex_ior": null
  },
  "attribution": {
    "authors": [],
    "license_spdx": "CC0-1.0",
    "source_url": "https://ambientcg.com/a/Bricks097"
  },
  "dates": {
    "published": "2024-11-22",
    "updated": "2024-11-22"
  },
  "upstream_id": "Bricks097"
}
```

**Every curated field is populated at v0.6.0** — no phased rollout. Sources that don't expose a given field write `null`; sources that do get normalized values in the declared units.

| Field | Unit / shape | Source coverage |
|---|---|---|
| `name` | str | all 4 |
| `category` | enum (10 canonical) | all 4 |
| `tags` | list[str] lowercased | all 4 |
| `description` | str / null | all 4 |
| `physical.dimensions_m` | `[x, y, z?]` m | ambientcg, polyhaven |
| `physical.max_resolution_px` | `[w, h]` | polyhaven; derived elsewhere |
| `pbr.color_rgb` | `[r, g, b]` float | physicallybased |
| `pbr.roughness` / `metalness` / `ior` | float | physicallybased |
| `pbr.specular_f0` | `[r, g, b]` | physicallybased |
| `pbr.transmission` | float | physicallybased |
| `pbr.complex_ior` | 6-float | physicallybased (passthrough) |
| `attribution.authors` | list[str] | polyhaven, gpuopen |
| `attribution.license_spdx` | SPDX str | all 4 |
| `attribution.source_url` | URL | all 4 |
| `dates.published` | ISO-8601 date | polyhaven, ambientcg, gpuopen |
| `dates.updated` | ISO-8601 date | ambientcg, gpuopen |
| `upstream_id` | str | all 4 |

### Layer 2 — `upstream`: verbatim, per-source, unstable

Source-shaped escape hatch. Explicitly **unstable** — no semver guarantees on shape. Inline in the same catalog JSON (no sidecar — texture bakes dwarf catalog size, so the storage-reviewer's split-file argument doesn't apply; the footgun concern is addressed by the typed accessor instead).

```json
"upstream": {
  "source": "ambientcg",
  "schema_version": 1,
  "fetched_at": "2026-04-20T16:00:00Z",
  "raw": { /* allowlisted verbatim keys, per-source allowlist */ }
}
```

### Record envelope

```json
{
  "id": "Bricks097",
  "source": "ambientcg",
  "mat_vis": { ... },
  "upstream": { ... },
  "available_tiers": ["1k"],
  "maps": ["color", "normal", "roughness", "ao"],
  "texture_hashes": { ... }
}
```

Top-level retains bake-pipeline fields (`id`, `source`, `available_tiers`, `maps`, `texture_hashes`). Every semantic field moves to `mat_vis.*`.

## Client API

Typed accessor only — `upstream.raw` never rides in `search()` / `index()` return values.

```python
# canonical queries: look at mat_vis only
entries = client.search(category="metal", source="physicallybased")
# → each entry has { id, source, mat_vis: {...}, available_tiers, maps, texture_hashes }
# No `upstream` key. Strip happens at the accessor boundary.

# explicit opt-in to raw:
raw = client.upstream("ambientcg", "Bricks097")
# → dict; the source-shaped fields. Documented as unstable.

raw["displayCategory"]  # may change / disappear without semver bump
```

Strip policy in client: `index(source)` returns entries with `upstream` elided; `upstream(source, id)` fetches + returns `entry["upstream"]["raw"]`.

## Ops gates (required at merge time)

From ops-burden reviewer, non-optional:

1. **Per-source allowlist** for `upstream.raw` keys. Lives colocated with the extractor (e.g. `src/mat_vis_baker/sources/ambientcg.py::UPSTREAM_ALLOWLIST`). Denylist rejected: unbounded maintenance trap.
2. **CI schema-diff gate**. Per-source key-set hash, diffed against the previous published revision. New keys in allowlist → warn + require ack label. Removed keys → fail. Canonical field presence regression >5% → fail.
3. **Extractor policy**: strict on `mat_vis.*` (missing required canonical field → fail record, quarantine, continue run); permissive on `upstream.raw` (accept arbitrary JSON-shaped blob within the allowlist).
4. **Stability marker** in schema docs. `upstream.raw` documented as `stability: "experimental"`. Clients warned in the `client.upstream()` docstring.

## Alternatives considered (and why they lost)

### (A) Keep normalize-at-bake, evolve `_CATEGORY_MAP` only
Rejected after audit: ~40% of real upstream categories collapse to `"other"` across all four sources; physicallybased's entire scientific scalar corpus (`complexIor`, `transmission`, `density`, ...) has no home in the existing schema; polyhaven's `dimensions` lost; asset-family graph (`variations`, `basedOnThis`) lost. Incremental keyword additions cannot recover these.

### (B) Mirror upstream verbatim, no canonical layer
Rejected by consumer-DX + data-modeling reviewers: every downstream client becomes a normalizer; cross-source filters (`category=metal` across sources) die; py-mat facade cannot offer stable queries.

### (C) Canonical + inline `upstream.raw` (original proposal in this issue's first draft)
Refined, not rejected. Four guardrails added during review:
- Typed client accessor (not in-dict) — resolves footgun without split files.
- Per-source allowlist (not denylist) for `raw` contents.
- CI schema-diff gate.
- Strict/permissive extractor policy.

### (D) Split catalog + sidecar file per source
Considered and dropped. Storage reviewer's size math (~25-30 MB ceiling, ambientcg breaching HF's 10 MB LFS line) was the original motivator, but:
- Texture tar bakes are multi-GB; catalog JSON size is within rounding error.
- HF LFS is transparent to clients (redirect handled).
- Footgun is already solved by the typed accessor.
So sidecar adds a second-fetch hop for no observable benefit.

## Phased implementation

Three child issues to land sequentially inside v0.6.0 milestone:

### Phase A — `mat_vis` block: schema + extractor scaffolding
- `src/mat_vis_baker/common.py`: rename `MaterialRecord`'s per-source fields into a `mat_vis: MatVisBlock` dataclass. Add nested `PhysicalBlock`, `PBRBlock`, `AttributionBlock`, `DatesBlock`.
- `docs/specs/index-schema.json`: bump to v3. Require `id`, `source`, `mat_vis` at top level. Declare `upstream` optional.
- `docs/specs/mat-vis-block-v3.json`: the Layer-1 JSON Schema.
- Unit tests for the new dataclasses.

### Phase B — per-source curated-field population
One sub-PR per source: `ambientcg`, `polyhaven`, `physicallybased`, `gpuopen`. Each wires the source's API response into every `mat_vis.*` field (nulls where unavailable). Includes unit test with a mocked upstream response asserting every field's shape.

### Phase C — `upstream.raw` + accessor + CI gates
- `UPSTREAM_ALLOWLIST` constant per source.
- `MatVisClient.upstream(source, material_id) → dict`.
- `index()` / `search()` strip `upstream` from returned entries.
- `.github/workflows/bake.yml`: add schema-diff gate.
- `scripts/check_upstream_schema_drift.py`: per-source key-set hash vs. previous revision.

Rebake of all four sources is an ops follow-up once the three phases merge. No back-compat with v2026.04.0 — a v0.6.0 rebake is required before clients upgrade.

## Acceptance criteria

- [ ] Index schema v3 landed, validated in CI
- [ ] `mat_vis.*` populated for every record across all 4 sources
- [ ] `upstream.raw` populated with per-source allowlist, inline in catalog
- [ ] `client.upstream(source, material_id)` works against live v0.6.0 release
- [ ] `client.search(source="physicallybased")` returns entries with `mat_vis.pbr.complex_ior` populated (the canonical "this exists because of #152" test)
- [ ] Schema-diff CI gate green on a rebake
- [ ] `docs/decisions/0011-*.md` ADR merged
- [ ] v0.6.0 release notes call out the breaking top-level → `mat_vis` move with a migration example

## Appendix: audit data

(See original issue draft above — retained for the record.)

### Quantified category loss at v2026.04.0

| Source | Entries audited | Category → `"other"` | Tags populated | Biggest dropped field |
|---|---|---|---|---|
| ambientcg | 5 samples | 1/5 (20%) | 5/5 | `dimensionX/Y/Z` |
| polyhaven | 5 samples | 2/5 (40%) | 5/5 | `description`, `dimensions` |
| physicallybased | 8 samples | 3/8 (38%) | 0/86 | `complexIor`, `transmission` |
| gpuopen | 13 cats | 5/13 (38%) | rebake pending (#142) | `material_type` (layered) |

Broader ambientcg sweep: **10/23 real browse categories → `"other"`**.

### Review consolidation (agent angles → summary)

- **Data-modeling** — ship it; inline nested; per-source versioned upstream schemas; canonical-wins.
- **Consumer-DX** — inline `raw` is a footgun; typed accessor mandatory; add `physical` projection (now in Layer 1).
- **Ops-burden** — land it with allowlist + CI schema-diff gate + `unstable` marker + strict/permissive extractor policy.
- **Storage/cost** — size estimate off 2-3×; sidecar or parquet if >20 MB; split-file recommendation superseded by "texture bakes dwarf catalog" observation during consolidation.

## Related

- mat-vis#150 — `normalize_category` plurals (complementary; still lands).
- mat-vis#151 — physicallybased tags (complementary; still lands).
- mat-vis#142 — gpuopen rewrite (landed in #145).
- mat-vis ADR-0007 / ADR-0008 — HF substrate; this fits under.
- py-mat#90 — downstream consumer; benefits from richer `mat_vis` fields but is already unblocked by #145.
