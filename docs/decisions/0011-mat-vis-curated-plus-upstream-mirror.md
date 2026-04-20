# 0011. mat_vis curated block + upstream mirror (two-layer index record)

- Status: Proposed
- Date: 2026-04-20
- Deciders: @gerchowl
- Related: mat-vis#152, mat-vis#150, mat-vis#151, py-mat#90, ADR-0007, ADR-0008

## Context

The pre-v0.6.0 catalog schema (see ADR-0001, ADR-0007) flattened a small,
hand-picked subset of upstream fields into each index entry: `name`,
`category`, `tags`, `source_url`, `source_license`, plus four scalars
(`color_hex`, `roughness`, `metalness`, `ior`) for `physicallybased`.
Four reviewer passes on a representative sample (5 ambientcg, 5 polyhaven,
8 physicallybased, 13 gpuopen categories) found:

- **~40 % of upstream categories collapse to `"other"`** across the four
  sources (mat-vis#150 fixed some of this, but the shape is still fragile).
- **physicallybased scientific scalars** (`complexIor`, `transmission`,
  `specular_f0`, …) have no home in the schema.
- **polyhaven physical dimensions** and long-form `description` are lost.
- **gpuopen asset-family graph** (`variations`, `basedOnThis`,
  `material_type`) is lost.

Incrementally growing the flat field list doesn't recover these — each
addition is a semver-breaking catalog-shape change, and the single-layer
shape has no place to park source-specific metadata that clients might
want but that has no meaningful cross-source normalization.

## Decision

Every index record carries two layers:

**Layer 1 — `mat_vis`: curated, unified, semver-stable.** The *only*
query surface. `search()`, `index()` and `filter()` look here and nowhere
else. Missing values are `null`, never absent — the key set is stable.
Nested by concern:

```
mat_vis.{name, category, tags, description, upstream_id}
mat_vis.physical.{dimensions_m, max_resolution_px}
mat_vis.pbr.{color_rgb, roughness, metalness, ior,
             specular_f0, transmission, complex_ior}
mat_vis.attribution.{authors, license_spdx, source_url}
mat_vis.dates.{published, updated}
```

**Layer 2 — `upstream`: verbatim, per-source, unstable.** Source-shaped
escape hatch. Explicitly unstable — no semver guarantees on shape.
Inline in the same catalog JSON (no sidecar).

Record envelope:

```json
{ "id": ..., "source": ..., "mat_vis": {...}, "upstream": {...},
  "available_tiers": [...], "maps": [...], "texture_hashes": {...} }
```

Four guardrails make this safe:

1. **Per-source allowlist** colocated with the extractor
   (`UPSTREAM_ALLOWLIST` in `sources/<src>.py`). Denylist is a
   maintenance trap; allowlist fails closed.
2. **CI schema-diff gate** on per-source key-set hash vs. the previous
   published revision. New allowlisted keys: warn + require ack label.
   Removed keys: fail. Canonical-field presence regression >5 %: fail.
3. **Strict/permissive extractor policy.** `mat_vis.*` is strict —
   missing required canonical field fails the record (quarantine,
   continue run). `upstream.raw` is permissive — accept any
   JSON-shaped blob within the allowlist.
4. **Typed client accessor.** `index()` / `search()` strip `upstream`
   from returned entries; `client.upstream(source, id)` is the only
   way to reach the verbatim block, and its docstring advertises
   `stability: "experimental"`.

Phased implementation (three stacked PRs inside v0.6.0):

- **Phase A** — dataclasses, v3 index schema, extractor refactor to
  emit `mat_vis.*`, client reads `mat_vis.*`. Layer-2 NOT added.
- **Phase B** — per-source population of the remaining canonical
  fields (`description`, `dimensions_m`, `complex_ior`, etc.). Unit
  tests with mocked upstream responses.
- **Phase C** — `upstream.raw` allowlist, `client.upstream()`, CI
  schema-diff gate.

## Consequences

### Positive
- Semver-stable query surface — additive changes to `mat_vis.*` never
  break clients, the escape hatch absorbs churn.
- Cross-source filters keep working because every source emits the
  same key set.
- Downstream consumers (py-mat, mat-sci) get the scientific scalars
  they need for physically-based simulation, without each rewriting
  an extractor.
- `index()` strip policy means the common-case return shape is small
  even when `upstream.raw` is large (ambientcg's full blob is ~3 KB
  per material).

### Negative
- Two-layer shape is more verbose than the flat form — index JSONs
  grow (roughly 2–3× for non-physicallybased sources).
- Allowlist maintenance: every upstream key added to `raw` needs a
  conscious edit. CI gate makes this a feature, not a chore.
- Clean break at v0.6.0 — no back-compat aliases. A client pinned at
  v0.5.x will not read a v0.6.0 release, and vice versa. Release
  notes must call this out.

## Alternatives rejected

**(A) Normalize-only, evolve `_CATEGORY_MAP`.** Rejected after audit:
keyword additions can't recover `complexIor`, `transmission`,
`dimensions`, or the gpuopen asset-family graph. These fields have no
place to live in a single-layer schema, so the fundamental limitation
is structural, not a keyword-coverage gap.

**(B) Mirror upstream verbatim, no canonical layer.** Rejected by the
consumer-DX reviewer: every downstream client becomes a normalizer;
cross-source filters (e.g. `category="metal"` across ambientcg,
polyhaven, gpuopen) stop working; the py-mat facade can't offer stable
queries. Pushing the normalization burden onto N clients is strictly
worse than doing it once in the baker.

**(D) Sidecar files per source.** Storage reviewer flagged that
ambientcg's verbatim blob breaches HF's 10 MB inline LFS boundary.
Dropped because: (i) texture tar bakes are multi-GB, so catalog JSON
size is in the rounding error; (ii) HF LFS is transparent to clients
via redirect; (iii) the footgun that motivated a split is already
solved by the typed accessor. A sidecar adds a second fetch hop for
no observable win.

## References

- mat-vis#152 — this ADR's locked design issue.
- mat-vis#150 — category normalization fix that exposed the
  structural limit of the keyword-map approach.
- mat-vis#151 — physicallybased `tags` field fix that drove home the
  "upstream has more we're dropping" audit.
- py-mat#90 — downstream facade's request for scientific scalars.
- ADR-0007 — HF substrate rollout that established the v2 catalog
  shape this ADR replaces.
- ADR-0008 — tree-as-SoT decision; `available_tiers` is already
  derived at read time, and the same principle extends cleanly to
  `upstream.raw`.
