# User stories

How people actually use `mat-vis`. Each story names a persona,
their goal, the path they take through the system, and what
they *don't* want to care about.

These stories are the check against over-engineering. When an
ADR decision seems clever but makes one of these paths worse,
something is wrong.

## 1. Radiation-physics researcher doing particle transport

**Persona**: PhD/postdoc working on Monte Carlo dose
simulations — Geant4 / TOPAS / PHITS / OpenMC. Needs
material scalar properties (density, composition, mean
atomic number) for hundreds of materials. Does not care
about texture PNGs.

**Story**:

1. `pip install mat-vis` on an HPC login node.
2. In a Python REPL:

   ```python
   import mat_vis
   for m in mat_vis.materials(source="physicallybased"):
       print(m.id, m.density, m.color_hex)
   ```

3. All scalar data arrives from the local JSON index bundled
   with the package (ADR-0001 two-tier: metadata is local).
   No network, no Parquet, no texture bytes touched.
4. Exports a CSV of (id, density, color) to feed into
   Geant4 NIST material definitions.

**What this story needs from the architecture**:

- Scalars must be readable **without** triggering Parquet
  downloads (ADR-0001).
- The client must work offline once `pip install`ed — the
  JSON indexes are part of the wheel or a trivial first-run
  fetch (ADR-0004 lazy cache covers this).
- Density, refractive index, mean atomic number must be
  present in the index. If upstream sources don't have them,
  the index layer needs to derive or mark as null — never
  silently fabricate.

## 2. build123d CAD user wanting "realistic wood"

**Persona**: mechanical engineer / maker designing parts in
build123d. Wants shapes to render with proper PBR materials
in `ocp_vscode` instead of flat colors.

**Story**:

1. Already has `build123d` and `ocp_vscode` installed. Adds
   `pip install mat-vis`.
2. In a notebook:

   ```python
   from build123d import Box
   import mat_vis

   shape = Box(10, 10, 10)
   shape.material = mat_vis.get("polyhaven/wood_table_001",
                                tier="2k")
   show(shape)  # ocp_vscode picks up the PBR material
   ```

3. First call: ~1 second (lazy cache fetches the row).
4. Every subsequent `show()` is instant.
5. Never learns what a Parquet is. Never opens a shell.

**What this story needs**:

- `mat_vis.get(id)` with sensible tier default (probably 2K
  per ADR-0003 for desktop viewers).
- Returned object is protocol-compatible with
  `mat.Material.pbr_source` so `ocp_vscode`'s extractor
  picks it up without copy-paste.
- Lazy cache default (ADR-0004) — the notebook reload loop
  doesn't re-fetch.
- Clear error if id is malformed, not a silent empty
  texture.

## 3. Jupyter data scientist exploring the corpus

**Persona**: researcher writing a notebook that needs to
characterize "what materials exist with low roughness and
high metalness". Statistical / ML-adjacent work.

**Story**:

1. `pip install mat-vis[sql]` (the SQL extra pulls DuckDB,
   per ADR-0005).
2.

   ```python
   import duckdb, mat_vis
   con = duckdb.connect(":memory:")
   mat_vis.register_views(con, tier="1k")

   con.execute("""
     SELECT source, id, roughness, metalness
     FROM (
       SELECT * FROM ambientcg
       UNION ALL SELECT * FROM polyhaven
       UNION ALL SELECT * FROM gpuopen
     )
     WHERE roughness < 0.2 AND metalness > 0.8
     ORDER BY roughness
   """).fetch_df()
   ```

3. Predicate pushdown means only matching row groups move
   over the network — query completes in seconds even
   though the underlying Parquets sum to ~5 GB.
4. Later, for the 20 materials that survive the filter,
   calls `mat_vis.get(id)` to download and inspect the
   actual textures.

**What this story needs**:

- ADR-0005 embedded shim for view registration.
- ADR-0003 category partitioning is orthogonal here —
  DuckDB handles the directory-of-Parquets pattern.
- Range-read pushdown must actually work (tested in CI).

## 4. CI pipeline maintainer for a rendering benchmark

**Persona**: engineer running a nightly rendering benchmark
that needs consistent materials across runs. Bandwidth and
reproducibility matter; random access does not.

**Story**:

1. In the CI config, pins `mat-vis==2026.04.0`.
2. First step of the nightly job:

   ```bash
   mat-vis prefetch --source ambientcg --tier 4k
   ```

3. This pulls down the 4K ambientcg Parquet files
   (partitioned per ADR-0003) into the CI cache directory
   (`.cache/mat-vis/`).
4. All subsequent render jobs read materials locally. No
   network traffic past that one prefetch.
5. Because ADR-0002 pins release asset bytes, the next 90
   days of nightly runs read the same bytes. Benchmark
   results are comparable.

**What this story needs**:

- `prefetch` CLI / Python API (ADR-0004).
- Release asset immutability (ADR-0002 calver tags).
- No hidden "phone home" fetches at import time — the
  client must work strictly offline once prefetched.

## 5. three-cad-viewer maintainer integrating PBR

**Persona**: JS developer on the three-cad-viewer side
wanting real PBR without requiring users to host textures
themselves.

**Story**:

1. Adds the `@morepet/mat-vis` npm package.
2. In the viewer:

   ```js
   import { loadMaterial } from '@morepet/mat-vis';
   const mat = await loadMaterial('ambientcg/wood_001',
                                  { tier: '1k' });
   mesh.material = mat;  // a three.js MeshStandardMaterial
   ```

3. Under the hood, `loadMaterial` range-reads the Parquet,
   extracts PNG bytes, hands them to `THREE.TextureLoader`.
4. Range-read keeps the bytes-over-wire minimal; the user
   never sees "loading 5 GB" progress bars.

**What this story needs**:

- ADR-0001 Parquet wire format readable by `parquet-wasm` or
  `duckdb-wasm`.
- ADR-0002 CORS on Release asset URLs (open risk — if
  broken, fall back to HF Datasets mirror).
- ADR-0003 tier splits so 1K path doesn't touch 4K bytes.
- Small npm footprint — the JS client is a thin URL/shim,
  not a bundled corpus.

## 6. Materials vendor contributing a new source

**Persona**: someone from a MaterialX-producing studio
(texturelib, a new CC0 source, etc.) wanting their library
included.

**Story**:

1. Opens an issue or PR adding a new entry to the watch
   workflow's source list.
2. The `watch.yml` (ADR-0002) picks up the new source on its
   next run, generates an `index/<new-source>.json`, and
   opens a PR for review.
3. Reviewer checks scalar coverage, license, id-naming
   collisions with existing sources.
4. On merge, `release.yml` bakes textures at all tiers and
   publishes `mat-vis-<new-source>-*.parquet` assets.
5. Next `mat-vis` release tag exposes the new source
   transparently — consumers get it on `pip install -U`.

**What this story needs**:

- Watch pipeline is source-pluggable — adding a source is a
  config change plus a small adapter, not a rewrite.
- The index schema is the same across sources, or uses
  an explicit "schema-normalize at the index layer"
  discipline (flagged in ADR-0003's upgrade trigger).
- License metadata is per-material, not per-source (some
  sources mix CC0 with other terms).

## 7. Air-gapped / sensitive-environment researcher

**Persona**: someone running in a hospital, defense, or
pharma environment where the research workstation can't
reach the public internet. They have bulk-transfer across
the air-gap via USB or a scrubbed cache server.

**Story**:

1. On an internet-connected machine: downloads the
   Parquet files for the tiers and sources they need
   (plain `curl https://github.com/...` or the `prefetch`
   CLI against a scratch cache dir).
2. Transfers the cache dir across the air-gap.
3. On the air-gapped machine: sets
   `MAT_VIS_CACHE_DIR=/mnt/transfer/mat-cache` and uses
   `mat_vis.get(...)` normally. No network.

**What this story needs**:

- Cache dir is portable — copying the files works, no
  machine-specific paths baked in.
- Release asset URLs are plain HTTPS (ADR-0002), not
  auth-gated.
- Scalars must be in the JSON index (ADR-0001), so the
  air-gapped machine doesn't need network for metadata
  queries either.

## Anti-stories — what we explicitly won't optimize for

- **Realtime streaming of materials over WebSocket.** Not
  the use case. Static-asset HTTP range reads + local
  cache is the model.
- **Mutating materials in-place and re-uploading.** Sources
  are upstream-owned. If a user wants a custom tweak,
  they derive locally and don't write back to mat-vis.
- **Multi-tenant hosted SQL with per-user ACLs.** See
  ADR-0005 — no hosted endpoint.
- **Animated or time-varying materials.** Out of scope;
  PBR is static surface properties.
