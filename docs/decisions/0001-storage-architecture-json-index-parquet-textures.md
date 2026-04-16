# 0001. Storage architecture: JSON indexes + .mtlx in git, Parquet bundles as Release assets

- Status: Accepted
- Date: 2026-04-16
- Deciders: @gerchowl

## Context

mat-vis curates ~3100 PBR materials from four upstream sources.
Per-material payload sizes vary by resolution tier:

| Tier | Per-map PNG | Per-material (5 maps avg) | Corpus total |
|---|---|---|---|
| 1K | ~500 KB | ~2 MB | ~5 GB |
| 2K | ~2 MB | ~10 MB | ~25 GB |
| 4K | ~8 MB | ~35 MB | ~80 GB |
| 8K | ~30 MB | ~130 MB | ~300 GB |

The storage layout must support:

1. **Reviewable change tracking** — PRs show "roughness went
   0.3 → 0.25", not "binary file changed".
2. **Pure-Python random access** — fetch one material's textures
   without pyarrow or any binary dependency.
3. **Offline / air-gapped** — download once, work forever.
4. **GitHub's 2 GB per-asset limit** (see ADR-0003).

## Decision

**Three-layer storage:**

- **JSON indexes** — `index/<source>.json`, tracked in git.
  Scalar metadata (color, roughness, metalness, ior, category,
  tags, source URL, license). ~10 MB total. Ships inside mat's
  wheel for offline scalar access.
- **MaterialX sources** — `mtlx/<source>/<id>.mtlx`, tracked in
  git. Flattened/baked XML with texture references rewritten to
  column names. ~15–30 MB total. Diffable, reviewable.
  Source of truth for baking.
- **Parquet texture bundles** — `mat-vis-<source>-<tier>.parquet`,
  distributed as GitHub Release assets. One row per material;
  binary columns hold raw PNG bytes. Not tracked in git.

All three layers share the same material IDs and are versioned
together via calver release tags (`v2026.04.0`).

## Companion rowmap for lightweight client access

`release.yml` publishes a `<source>-<tier>-rowmap.json` alongside
each Parquet as a Release asset:

```json
{
  "Metal064": {
    "color":     {"offset": 102400, "length": 51200},
    "normal":    {"offset": 153600, "length": 48000},
    "roughness": {"offset": 201600, "length": 12800}
  }
}
```

mat's built-in texture client fetches the rowmap (~few hundred KB,
cached), then does pure-Python HTTP range reads (`urllib.request`
with `Range` header) to pull individual materials from the Parquet.
No pyarrow. No binary deps.

**Binary texture columns use UNCOMPRESSED Parquet encoding.** PNG
is already internally compressed; Zstd on top saves ~2-5% but
requires a decompressor on the client. UNCOMPRESSED means the
range-read bytes ARE the raw PNG — write to cache, done.

**Scalar/string columns use ZSTD.** These are only read by
pyarrow/DuckDB power users who have decompression built in.

## Wire format

Texture data: **raw PNG bytes** in `color`, `normal`, `roughness`,
`metalness` (nullable), `ao` (nullable) binary columns.

Alternatives rejected:

- **Raw pixel matrices** — 8–10× larger on disk. PNG's prediction
  filters compress far better than general-purpose codecs on raw
  pixels.
- **Basis Universal / KTX2** — deferred. ~5× smaller than PNG,
  GPU-native transcoding, Three.js has `KTX2Loader`. Future tier
  alongside PNG once the baker emits it (see upgrade triggers).

## Parquet schema

```
id                 STRING       -- e.g. "Metal064"
source             STRING       -- e.g. "ambientcg"
category           STRING       -- e.g. "metal"
resolution_px      INT32        -- 1024, 2048, 4096, 8192

color              BINARY       -- PNG bytes, UNCOMPRESSED
normal             BINARY       -- PNG bytes, UNCOMPRESSED
roughness          BINARY       -- PNG bytes, UNCOMPRESSED
metalness          BINARY       -- PNG bytes, nullable, UNCOMPRESSED
ao                 BINARY       -- PNG bytes, nullable, UNCOMPRESSED

source_url         STRING       -- upstream page URL
source_mtlx_url    STRING       -- nullable, upstream .mtlx URL
source_license     STRING       -- CC0 / Apache / etc.
baker_version      STRING
baked_at           STRING       -- ISO timestamp
```

Future HDR columns (`displacement`, `displacement_hdr`,
`emission`, `emission_hdr`) can be added without breaking
existing consumers — Parquet schemas are forwards-compatible.

## DuckDB / pyarrow access

Power users query the Parquet files directly with their own
tooling:

```sql
SELECT * FROM 'https://.../mat-vis-ambientcg-1k.parquet'
WHERE category = 'wood' AND roughness < 0.4
```

No hosted DuckDB. No client-side shim. The files are valid,
self-describing, industry-standard Parquet — any tool that reads
Parquet works.

## Consequences

**Enables**:

- **Reviewable PRs.** `watch.yml` touches `index/*.json` and
  `mtlx/` — reviewer reads a GitHub-rendered diff.
- **Pure-Python random access.** Rowmap + stdlib HTTP. No binary
  deps on the user's machine.
- **Offline mode.** Prefetch the Parquet via `curl`, point the
  cache dir at it. Or use the JSON index for scalar-only work.
- **Power-user SQL.** DuckDB/pyarrow against the same files.

**Costs**:

- **Writing Parquet requires pyarrow in CI.** One pip install in
  the runner. Never touches users.
- **Binary diffability lost** for textures. Mitigated by .mtlx
  diffs in git; texture bit-flips are rare upstream.

## Upgrade triggers

1. **Corpus > ~100K materials** — current layout becomes unwieldy.
2. **KTX2 output** from the baker — add `ktx2` tier alongside PNG.
3. **GitHub raises the 2 GB limit** — simplifies ADR-0003.
