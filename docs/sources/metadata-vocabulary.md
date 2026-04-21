# Upstream metadata vocabulary

The material records flowing into `mat_vis.category` and `mat_vis.tags`
(see [ADR-0011](../decisions/0011-mat-vis-curated-plus-upstream-mirror.md))
come from four upstream APIs. Each has its own vocabulary — its own
category field name, its own category titles, and its own tag
conventions — that the baker must normalize onto our 10 canonical
categories (metal, wood, stone, fabric, plastic, concrete, ceramic,
glass, organic, other).

This doc is the machine-readable record of what those upstream
vocabularies actually contain, captured from live APIs on
**2026-04-21**. It's the input to the `normalize_category` /
`normalize_tags` mapping work in
`src/mat_vis_baker/common.py`. When the mapping expands, the
`docs/sources/metadata-vocabulary.json` sidecar gets re-probed and
re-committed so future-us can diff before-vs-after.

Re-probe with:

```bash
uv run python scripts/probe-metadata-vocab.py > docs/sources/metadata-vocabulary.json
```

## Counts

| Source          | Records | Unique categories | Category field             |
|-----------------|--------:|------------------:|----------------------------|
| ambientcg       |   1,993 |                95 | `displayCategory` (string) |
| polyhaven       |     754 |                54 | `categories[]` (list)      |
| gpuopen         |     454 |                22 | `category` → resolved title|
| physicallybased |      86 |                 7 | `category` (string)        |

## Why 'other' is not necessarily a bug

Some upstream categories intentionally fall through to `'other'`
because no single canonical category is correct:

| Upstream label         | Why it stays 'other'                         |
|------------------------|----------------------------------------------|
| `Wallpaper`            | Multi-material context (paper + print) |
| `Interior Flooring`    | Multi-material (wood, tile, stone, ...)|
| `Exterior Flooring`    | Same reason                            |
| `Base Materials` (gpuopen) | gpuopen-internal parent bucket   |
| `SciFi`                | Stylistic, not a material              |
| `Atlas`, `Decal`, `Sign`, `OnlyPBR` (ambientcg) | Meta/format tags |
| `Manmade`, `Human`, `Liquid` (physicallybased) | Too broad |
| `Facade`, `Roofing`    | Multi-material contexts                |

See `normalize_category` in `src/mat_vis_baker/common.py` — the
comment block above that function catalogs these.

## Top-15 by source (hot path for mapping)

### ambientcg — 95 categories via `displayCategory`

| Count | Label | Maps to |
|------:|-------|---------|
| 158 | Tiles | ceramic |
| 154 | Paving Stones | stone (via "stone") |
| 121 | Ground | organic |
| 115 | Bricks | ceramic (via plural handler → "brick") |
| 101 | Wood | wood |
| 101 | Metal | metal |
| 87 | Fabric | fabric |
| 74 | Wood Floor | wood (first token matches) |
| 67 | Rock | stone |
| 61 | Concrete | concrete |
| 59 | Planks | wood (expand needed) |
| 50 | Leather | fabric |
| 45 | Asphalt | concrete |
| 43 | Gravel | stone |
| 31 | Road | concrete (via "asphalt" token in tags) |

### polyhaven — 54 categories via `categories[]` (tag-like list)

| Count | Label | Maps to |
|------:|-------|---------|
| 466 | outdoor | — (context, not material) |
| 403 | man made | — (context) |
| 254 | floor | — (context) |
| 228 | wall | — (context) |
| 153 | natural | — (context) |
| 126 | terrain | organic |
| 120 | plaster-concrete | concrete |
| 113 | dirty | — (context) |
| 112 | rock | stone |
| 102 | brick | ceramic |
| 76 | concrete | concrete |
| 75 | wood | wood |
| 73 | indoor | — (context) |
| 57 | sand | organic |
| 49 | clean | — (context) |

Polyhaven's top-level "categories" are a mix of material types,
contexts (outdoor / indoor / dirty), and locations — the baker has
to pick the material token from the list, not treat the whole list
as the category name.

### gpuopen — 22 categories via `_category_title` (resolved from UUID)

| Count | Label | Maps to |
|------:|-------|---------|
| 46 | Fabrics | fabric |
| 41 | Wood | wood |
| 36 | Interior Flooring | other (intentional) |
| 32 | Marble | stone |
| 31 | Wallpaper | other (intentional) |
| 29 | Base Materials | other (intentional) |
| 28 | Brick Wall | ceramic (via "brick") |
| 24 | Marble Tiles | stone (first token) |
| 23 | Concrete | concrete |
| 22 | Metal | metal |
| 19 | Plaster | concrete |
| 19 | Exterior Flooring | other (intentional) |
| 18 | Ground | organic |
| 16 | Tile | ceramic |
| 16 | Cobblestone | stone |

### physicallybased — 7 categories

| Count | Label | Maps to |
|------:|-------|---------|
| 30 | Metal | metal |
| 13 | Manmade | other (intentional) |
| 10 | Organic | organic |
| 10 | Human | other (intentional) |
| 9 | Crystal | glass (via "crystal") |
| 8 | Liquid | other (intentional) |
| 6 | Plastic | plastic |

## How this drives roadmap

When the normalizer map gets expanded (gaps detected by the round-trip
test in `tests/test_mat_vis_block_roundtrip.py`), the workflow is:

1. Add the missing token(s) to `_CATEGORY_MAP` in
   `src/mat_vis_baker/common.py`.
2. Re-run the probe script; re-commit `metadata-vocabulary.json`.
3. Rebake the scratch dataset and re-run the integration test.

Expanding the map is cheap; shrinking it is expensive (consumer
contract). Keep the canonical-10 boundary stable and lean on the
`other` bucket for true-multi-material categories.

## See also

- [ADR-0011 — mat_vis curated + upstream.raw mirror](../decisions/0011-mat-vis-curated-plus-upstream-mirror.md)
- [`src/mat_vis_baker/common.py`](../../src/mat_vis_baker/common.py) — `normalize_category`, `_CATEGORY_MAP`
- [`tests/test_mat_vis_block_roundtrip.py`](../../tests/test_mat_vis_block_roundtrip.py) — regression gate
