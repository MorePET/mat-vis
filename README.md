# mat-vis

**PBR texture mirror for [MorePET/mat][mat].**

Curates ~3100 PBR materials from four open MaterialX sources, bakes
them to flat PNGs in CI, and hosts the output as Parquet files on
GitHub Releases. Users never install this repo — they
`pip install mat` and textures are available automatically via a
pure-Python client (~150 lines, zero binary deps).

## How it works

```
┌─────────────────────────────────────────────────────────────────┐
│  IN GIT (this repo, ~40 MB, reviewable)                        │
│                                                                 │
│  index/*.json        — scalar metadata per source               │
│  mtlx/<source>/*.mtlx — flattened MaterialX XML per material    │
│                                                                 │
│  .github/workflows/                                             │
│    watch.yml   — daily: poll upstream, open PR on change        │
│    release.yml — on tag: bake .mtlx → PNG, pack Parquet, upload │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼  CI runner (pure Python for ~90%; materialx for layered gpuopen only)
┌─────────────────────────────────────────────────────────────────┐
│  ON GITHUB RELEASES (5–300 GB, derived artifacts)              │
│                                                                 │
│  mat-vis-<source>-<tier>.parquet  — PNG bytes per material      │
│  <source>-<tier>-rowmap.json      — id → byte offset lookup     │
│  release-manifest.json            — master URL table            │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼  pure-Python HTTP range reads (stdlib urllib)
┌─────────────────────────────────────────────────────────────────┐
│  USER'S MACHINE                                                │
│                                                                 │
│  pip install mat  (~2 MB, no extras needed)                     │
│                                                                 │
│  from pymat import Material                                     │
│  steel = Material("Stainless Steel 316L")                       │
│  steel.density                        # from TOML (in wheel)    │
│  steel.properties.pbr.textures.color  # PNG bytes (from Release)│
│  steel.properties.pbr.textures.normal # cached at ~/.cache/mat/ │
└─────────────────────────────────────────────────────────────────┘
```

## Sources

| Source | Materials | License | Textures |
|---|---|---|---|
| [ambientcg](https://ambientcg.com) | ~2000 | CC0 | PNG (pre-baked) |
| [polyhaven](https://polyhaven.com) | ~752 | CC0 | PNG + EXR |
| [gpuopen](https://matlib.gpuopen.com) | ~300 | TBV | MaterialX (some need baking) |
| [physicallybased.info](https://physicallybased.info) | ~86 | CC0 | scalar-only |

## Resolution tiers

| Tier | Per material | Corpus total | GitHub hosting |
|---|---|---|---|
| 1K | ~2 MB | ~5 GB | GitHub Releases |
| 2K | ~10 MB | ~25 GB | GitHub Releases |
| 4K | ~35 MB | ~80 GB | GitHub Releases (partitioned by category) |
| 8K | ~130 MB | ~300 GB | Hugging Face Datasets (too large for GH) |

Future: KTX2/Basis Universal tier (~5× smaller, GPU-native) once
the baker emits it.

## Key design decisions

Architecture is captured in [`docs/decisions/`](docs/decisions/):

1. [**ADR-0001**](docs/decisions/0001-storage-architecture-json-index-parquet-textures.md)
   — Three-layer storage: JSON indexes + .mtlx sources in git,
   Parquet texture bundles as Release assets, companion rowmap
   for pure-Python byte-level access.
2. [**ADR-0002**](docs/decisions/0002-hosting-github-releases-watch-and-pr.md)
   — GitHub Releases hosting (free, CDN-backed); daily
   watch-and-PR flow for upstream change detection.
3. [**ADR-0003**](docs/decisions/0003-resolution-tiers-and-partitioning.md)
   — Per (source × tier) Parquet files; category partitioning
   at 4K+ to fit GitHub's 2 GB per-asset limit.
4. [**ADR-0004**](docs/decisions/0004-access-modes-lazy-local-cache-default.md)
   — Lazy local cache at `~/.cache/mat-vis/` as default;
   prefetch and no-cache modes opt-in.

## Relationship to mat

mat-vis is the **data factory**. [MorePET/mat][mat] is the
**user-facing library**.

| | mat | mat-vis (this repo) |
|---|---|---|
| What | Python API + material data | Data pipeline + hosting |
| Source data | TOML (physical properties) | .mtlx + JSON (appearance) |
| Artifact | PyPI wheel (~2 MB) | Parquet on GH Releases (GB) |
| Versioning | semver (API-driven) | calver (upstream-driven) |
| User installs? | yes (`pip install mat`) | no (CI-only) |

mat's built-in texture client reads mat-vis's Release assets
directly — rowmap JSON for offsets, stdlib HTTP for range reads,
local cache for persistence. No pyarrow, no DuckDB, no binary deps.

Power users who want SQL can query the Parquet files directly
with their own DuckDB/pyarrow install (scalar metadata columns
like `category` are queryable; binary texture columns are opaque
blobs):

```sql
SELECT id, source, category FROM
  'https://github.com/MorePET/mat-vis/releases/download/v2026.04.0/mat-vis-ambientcg-2k.parquet'
WHERE category = 'wood'
```

## License

- **Code** (build scripts, workflows, schemas): MIT — see
  [`LICENSE`](LICENSE).
- **Data**: license inherits from each upstream source. Three of
  four are **CC0 1.0** (public domain). gpuopen license TBV.

## Links

- [MorePET/mat][mat] — the user-facing library (physical props + PBR + textures)
- [gumyr/build123d](https://github.com/gumyr/build123d) — primary CAD consumer
- [bernhard-42/threejs-materials](https://github.com/bernhard-42/threejs-materials) — consumer-side PBR dataclass + Three.js adapter (used by ocp_vscode, not by our bake pipeline)
- [build123d#598](https://github.com/gumyr/build123d/issues/598) — material system roadmap thread

[mat]: https://github.com/MorePET/mat
