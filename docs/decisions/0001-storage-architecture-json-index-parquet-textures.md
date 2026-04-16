# 0001. Storage architecture: JSON indexes + Parquet texture bundles

- Status: Accepted
- Date: 2026-04-16
- Deciders: @gerchowl

## Context

`mat-vis` distributes roughly 3100 PBR materials consolidated from
four upstream MaterialX / scalar libraries:

- **ambientcg** — ~2000 materials, CC0, MaterialX + texture PNGs
- **polyhaven** — ~752 materials, CC0, MaterialX + texture PNGs/EXRs
- **gpuopen** (matlib.gpuopen.com) — ~300 materials, license TBV, MaterialX
- **physicallybased.info** — ~86 materials, CC0, scalar JSON only (no textures)

Per-material payload sizes vary wildly by resolution tier:

| Tier | PNG per map | Per-material total (5 maps avg) |
|---|---|---|
| 1K | ~500 KB | ~2 MB |
| 2K | ~2 MB | ~10 MB |
| 4K | ~8 MB | ~35 MB |
| 8K | ~30 MB | ~130 MB |

Full 1K-only corpus: ~5 GB. Full 4K corpus: ~80 GB. We need a
storage layout that simultaneously supports:

1. **Reviewable change tracking** — the `watch` workflow
   (ADR-0002) opens PRs when upstream sources change; the reviewer
   needs to see "Metal064's roughness went 0.3 → 0.25" as a
   diff, not a binary blob.
2. **Language-agnostic consumption** — Python (py-materials), Rust
   (rs-materials), JS (three-cad-viewer), possibly C++ (Geant4).
3. **Random-access remote reads** — consumers routinely want "just
   the Stainless Steel Brushed material" without downloading the
   full corpus.
4. **Offline / air-gapped support** — one-file-download-forever
   for CI, HPC, and air-gapped environments.
5. **Respects GitHub's 2 GB per-asset limit** (see ADR-0003 for
   the partitioning consequence).

## Decision

**Two-tier storage, each tier in its native-optimal format**:

- **Metadata index — JSON, one file per source, tracked in the
  repository** under `index/<source>.json`. Contains scalar values
  (color, metalness, roughness, ior, etc.), source URL, license,
  tags, and *texture references* (opaque keys). No binary data.
  Total size across all four sources: approximately 10 MB.
- **Texture bundles — Parquet, partitioned per (source × tier),
  distributed as GitHub Release assets** under
  `mat-vis-<source>-<tier>.parquet`. One row per material;
  columns include all scalar values AND binary columns with the
  raw PNG/EXR bytes (`color`, `normal`, `roughness`, `metalness`,
  `ao`). Not tracked in the repository.

The two layers share the same set of material IDs and are
versioned together via release tags.

## Consequences

**Enables**:

- **Reviewable PRs** on upstream source changes. The `watch`
  workflow touches only `index/*.json` — a reviewer reads a
  GitHub-rendered diff and approves or rejects.
- **HTTP range reads** for remote random access. Parquet stores
  its metadata footer at the end of the file; a consumer
  `HTTP GET`s the footer (~few KB), parses it to find the target
  material's row offset + length, then issues a second `HTTP GET`
  for exactly that byte range. Total bandwidth per material
  access: ~10 MB regardless of total dataset size.
- **Offline / prefetch mode**: consumers download the whole
  per-(source × tier) parquet once and read it locally forever.
  No tarball extraction. One file per tier.
- **Columnar filtering**: "all materials with `category ==
  'wood'` and `roughness < 0.4`" pushes the predicate down into
  Parquet, reading only matching row groups.
- **Language portability**: JSON indexes are zero-dep to read
  anywhere. Parquet has mature readers in Python (`pyarrow`),
  Rust (`parquet` + `arrow`), JS (`parquet-wasm`, `duckdb-wasm`),
  and C++ (`arrow-cpp`).

**Costs**:

- **Parquet readers have install weight**. `pyarrow` is ~30 MB.
  `parquet-wasm` is ~500 KB gzipped but less mature than
  `pyarrow` for range-read edge cases. Mitigation: the `mat-vis`
  Python client wraps this so the user only imports one thing,
  and `pyarrow` is a dependency of the client, not of
  `py-materials` itself.
- **Writing Parquet requires tooling in CI** — `release.yml`
  needs `pyarrow` in its runner. Minor (one pip install in the
  workflow).
- **Binary diffability lost** for texture content. Mitigated by
  scalar metadata changes staying in JSON; texture bit-flips are
  rare (upstream sources rebake infrequently).

**Rules out**:

- **All-JSON** (metadata only, textures as individual file URLs).
  Kills remote random access — consumer would make N HTTP
  requests for N maps, no predicate pushdown. Also awkward to
  version (3000+ loose files per tier per source).
- **All-Parquet** (metadata bundled with textures, no separate
  JSON index). Kills the watch-and-PR audit trail: PRs become
  "binary file changed" blobs.
- **Tarballs** (`.tar.gz` of per-material folders). No random
  access — gzip is sequential, tar has no index. First-mile UX
  is also awful (consumer has to pick: one-tarball-per-material
  = 3000+ release assets, or one-tarball-per-source = gigabytes
  downloaded for a single material).
- **ZIP** (end-of-file central directory enables random access).
  Works in principle but weaker tooling ecosystem for
  programmatic multi-file reads than Parquet; also no columnar
  metadata predicate pushdown. Acceptable fallback if Parquet
  ever becomes painful, not a default.

## Alternatives considered

- **All-JSON with URL refs** — rejected, O(N) HTTP requests per
  material, no pushdown.
- **All-Parquet with embedded indexes** — rejected, loses PR
  diffability.
- **Tarballs per source per tier** — rejected, no random access;
  forced all-or-nothing download.
- **ZIP with remote central-directory reads** — rejected as a
  default (weaker tooling, no pushdown), kept as an emergency
  fallback if Parquet causes pain.
- **SQLite with BLOB columns** — works, but SQLite's HTTP-range
  story (via `sqlite-wasm`, `libsqlite3-vfs-http`) is less mature
  than Parquet's. Also no Arrow ecosystem alignment.

## Wire format (what's in the parquet binary columns)

Texture data is stored as **raw PNG bytes** in the `color`,
`normal`, `roughness`, `metalness`, `ao` binary columns. Not raw
pixel matrices, not GPU-compressed formats (yet).

Alternatives considered:

- **Raw pixel matrices** (RGBA8 arrays) — rejected: ~8–10× larger
  on disk than PNG. PNG's image-specific prediction filters
  (sub/up/average/paeth) produce decorrelated residuals that
  compress far better than general-purpose compressors (zstd,
  brotli) can manage on raw pixel data. Shipping matrices
  would turn the 1K corpus from ~5 GB to ~40 GB.
- **Basis Universal / KTX2** (GPU-native supercompressed) —
  deferred: smaller than PNG *and* zero CPU decode (the GPU
  transcodes on upload). Three.js has native `KTX2Loader`. Would
  be a clean future upgrade once `threejs-materials`' baker
  emits KTX2. Flagged under the upgrade trigger below.

CPU decode cost is ~5 ms per 1K texture on modern hardware —
negligible for the typical once-per-session load pattern of CAD
shapes. Not worth optimizing at the format layer. The decoded-
image caching flag (`cache_decoded=True`, default off, see
ADR-0004) covers the pathological repeat-load cases locally.

## Companion rowmap for lightweight client access

`release.yml` publishes a `<source>-<tier>-rowmap.json` alongside
each Parquet file as a GitHub Release asset. The rowmap maps each
material ID to byte offset + length per binary column:

```json
{
  "Metal064": {
    "color":     {"offset": 102400, "length": 51200},
    "normal":    {"offset": 153600, "length": 48000},
    "roughness": {"offset": 201600, "length": 12800}
  }
}
```

This enables mat's built-in texture client to fetch textures via
pure-Python HTTP range reads (`urllib.request` with `Range` header)
without requiring `pyarrow` or any binary dependency. The client
loads the JSON index (~10 MB) for filtering (a list comprehension,
not a database query) and the rowmap (~few hundred KB) for
byte-level access to the Parquet file's binary columns.

**Binary texture columns use UNCOMPRESSED encoding** in the Parquet
file. Because the column values are raw PNG bytes (already
compressed internally by PNG's deflate), applying Parquet-level
compression (e.g. ZSTD) on top saves only ~2-5% and would require
a decompressor on the client side. Leaving them uncompressed means
the range-read bytes are raw PNG — the client writes them directly
to the cache with no decompression step.

Scalar and string columns (metadata, tags, color hex, etc.) still
use **ZSTD** compression. These columns are only read by
pyarrow/DuckDB power users who already have decompression support
built in, not by the default lightweight client.

## DuckDB-file format not chosen

We considered shipping the data as a DuckDB native `.duckdb`
file instead of Parquet. Rejected for language portability:
Parquet has first-class readers in Python (`pyarrow`), Rust
(`parquet` + `arrow`), JS (`parquet-wasm`), and C++ (`arrow-cpp`)
as a multi-vendor commodity format. `.duckdb` files are
DuckDB-only.

Users who want SQL ergonomics get them for free against our
Parquet files — DuckDB CLI or `duckdb` Python can
`SELECT * FROM 'https://.../mat-vis-ambientcg-1k.parquet'`
directly. No hosting of `.duckdb` files as endpoints needed;
no client-side shim or embedded DuckDB required. The Parquet
files are valid, self-describing, industry-standard Parquet —
DuckDB and pyarrow users query them directly with their own
tooling. Documentation (not code) shows them how.

## Upgrade trigger

Revisit when any of:

1. **Corpus > ~100K materials** makes per-source Parquet files
   unwieldy even with partitioning (ADR-0003).
2. **Basis Universal / KTX2 output** in `threejs-materials`
   becomes standard. Then we can add a `ktx2` tier alongside
   or replacing PNG bytes — smaller on disk, zero CPU decode,
   direct GPU upload in `three-cad-viewer`.
3. **Web-side Parquet tooling proves painful** in practice for
   `three-cad-viewer` or similar JS consumers. A ZIP-central-
   directory fallback for PNG files could ship alongside the
   Parquet primary — both accessible via HTTP range reads from
   the same Release.
4. GitHub substantially raises the 2 GB per-asset limit —
   partitioning logic in ADR-0003 could simplify.
