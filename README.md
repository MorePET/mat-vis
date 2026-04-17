# mat-vis

**PBR texture data factory for [MorePET/mat][mat].**

Curates ~3 000 PBR materials from four open sources, bakes them to
flat PNGs, and hosts the output as Parquet files on GitHub Releases.
Consumers fetch individual textures via HTTP range reads — no bulk
download, no pyarrow, no binary deps.

```python
pip install mat-vis-client
```

```python
from mat_vis_client import MatVisClient

client = MatVisClient()                                      # auto-discovers latest release
png = client.fetch_texture("ambientcg", "Rock064", "color")  # 1k PNG bytes, one HTTP range read
results = client.search(category="wood")                      # filter by category
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  IN GIT (this repo, ~40 MB, reviewable)                        │
│                                                                 │
│  index/*.json        — material metadata per source             │
│  mtlx/<source>/*.mtlx — MaterialX XML (gpuopen originals)      │
│  src/mat_vis_baker/  — fetch → bake → pack pipeline             │
│  clients/            — Python, JS, Rust, Shell reference clients│
│  .dagger/            — Dagger CI pipeline                       │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼  Dagger bake pipeline (streaming writer, constant memory)
┌─────────────────────────────────────────────────────────────────┐
│  ON GITHUB RELEASES (calver: v2026.04.0)                       │
│                                                                 │
│  mat-vis-<source>-<tier>-<category>.parquet — PNG bytes         │
│  <source>-<tier>-<category>-rowmap.json    — byte offset lookup │
│  release-manifest.json                     — discovery index    │
│  <source>.json                             — material metadata  │
│  physicallybased.json                      — scalar properties  │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼  HTTP range reads (stdlib urllib, zero deps)
┌─────────────────────────────────────────────────────────────────┐
│  CONSUMER                                                      │
│                                                                 │
│  pip install mat-vis-client      (PyPI, zero deps)             │
│  — or —                                                        │
│  <script src="mat-vis-client.mjs">  (browser/Node)             │
│  — or —                                                        │
│  curl + jq (mat-vis.sh)                                        │
└─────────────────────────────────────────────────────────────────┘
```

## Sources

| Source | Materials | License | Content |
|---|---|---|---|
| [ambientcg](https://ambientcg.com) | ~1 965 | CC0-1.0 | PNG textures |
| [polyhaven](https://polyhaven.com) | ~752 | CC0-1.0 | PNG textures |
| [gpuopen](https://matlib.gpuopen.com) | ~300 | per-material | MaterialX + PNG textures |
| [physicallybased.info](https://physicallybased.info) | ~86 | CC0-1.0 | scalar only (IOR, roughness, color) |

## Resolution tiers

All tiers use the same Parquet + rowmap + client API.

| Tier | Per material | Status |
|---|---|---|
| 128 | ~10 KB | released (ambientcg, polyhaven, gpuopen) |
| 256 | ~40 KB | released (ambientcg, polyhaven, gpuopen) |
| 512 | ~150 KB | released (ambientcg, polyhaven, gpuopen) |
| 1k | ~2 MB | released (ambientcg, polyhaven, gpuopen) |
| 2k | ~10 MB | released (polyhaven); ambientcg + gpuopen baking |

Sub-1k tiers are derived from 1k parquets via `derive-from-release`
(reads from our own release, resizes, packs — no upstream download).

## Client usage

### Python

```python
from mat_vis_client import MatVisClient

client = MatVisClient()

# fetch a single texture channel
png = client.fetch_texture("ambientcg", "Rock064", "color", tier="1k")
with open("rock064_color.png", "wb") as f:
    f.write(png)

# list available materials
for mat_id in client.materials("ambientcg", "1k"):
    print(mat_id)

# search across all sources
results = client.search("marble")

# export to three.js / glTF / MaterialX
from mat_vis_client.adapters import to_threejs, to_gltf, export_mtlx
```

### JavaScript (browser or Node)

```js
import { MatVisClient } from './mat-vis-client.mjs';
const client = new MatVisClient();
const png = await client.fetchTexture('polyhaven', 'castle_brick_02_red', 'color', '1k');
```

### Shell (curl + jq)

```bash
source mat-vis.sh
mat_vis_fetch ambientcg Rock064 color 1k > rock064.png
```

### SQL (DuckDB / pyarrow)

```sql
SELECT id, source, category FROM
  'https://github.com/MorePET/mat-vis/releases/download/v2026.04.0/mat-vis-ambientcg-1k-ceramic.parquet'
WHERE category = 'ceramic'
```

## Development

### Prerequisites

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- [Dagger](https://dagger.io) (CI pipeline)
- Nix + direnv (optional, provides full devShell)

### Local bake

```bash
uv sync
source .venv/bin/activate

# bake a single source + tier
mat-vis-baker all ambientcg 1k ./output --release-tag v2026.04.0

# derive smaller tiers from a release
mat-vis-baker derive-from-release v2026.04.0 512 ./output-512

# generate catalog from release
mat-vis-baker catalog-from-release v2026.04.0 --output-dir .
```

### Dagger CI

```bash
# smoke test
dagger call -m .dagger smoke --src=.

# full bake + release upload
dagger call -m .dagger bake-and-release \
  --src=. --source=ambientcg --tier=1k \
  --release-tag=v2026.04.0 --registry-pass=env:GITHUB_TOKEN
```

## Versioning

- **Data releases**: calver (`v2026.04.0`) — tied to upstream source updates
- **Code/client releases**: semver (`v0.1.0`) — API changes

## Key design decisions

Architecture is captured in [`docs/decisions/`](docs/decisions/):

1. [**ADR-0001**](docs/decisions/0001-storage-architecture-json-index-parquet-textures.md)
   — Three-layer storage: JSON indexes + .mtlx in git,
   Parquet bundles as Release assets, rowmap for byte-level access.
2. [**ADR-0002**](docs/decisions/0002-hosting-github-releases-watch-and-pr.md)
   — GitHub Releases hosting (free, CDN-backed); weekly
   watch for upstream change detection.
3. [**ADR-0003**](docs/decisions/0003-resolution-tiers-and-partitioning.md)
   — Per (source x tier) Parquet files; category partitioning
   with dynamic size splitting to stay under GitHub's 2 GB limit.
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
| User installs? | yes (`pip install mat`) | `pip install mat-vis-client` |

## License

- **Code** (build scripts, workflows, clients): MIT — see
  [`LICENSE`](LICENSE).
- **Data**: license inherits from each upstream source. Three of
  four are **CC0 1.0** (public domain). gpuopen license per-material.

## Links

- [MorePET/mat][mat] — the user-facing library (physical props + PBR textures)
- [mat-vis-client on PyPI](https://pypi.org/project/mat-vis-client/) — Python client package
- [gumyr/build123d](https://github.com/gumyr/build123d) — primary CAD consumer

[mat]: https://github.com/MorePET/mat
