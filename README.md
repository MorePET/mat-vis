# mat-vis

**PBR texture data factory for [MorePET/mat][mat].**

Curates ~3 260 PBR materials from four open sources, bakes them to
flat PNGs, and hosts the output as a per-file Hugging Face dataset
(``huggingface.co/datasets/gerchowl/mat-vis``, ADR-0012). Consumers
fetch individual textures with one plain HTTP GET — no range
reads, no pyarrow, no binary deps.

```bash
pip install mat-vis-client
```

```python
from mat_vis_client import MatVisClient

client = MatVisClient()                                         # defaults to v2026.04.2
mats = client.materials("ambientcg", "1k")
png  = client.fetch_texture("ambientcg", mats[0], "color", "1k")  # PNG bytes, one HTTP GET
results = client.search(category="wood")                          # filter by category
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  IN GIT (this repo, ~40 MB, reviewable)                         │
│                                                                 │
│  index/*.json         — material metadata per source            │
│  mtlx/<source>/*.mtlx — MaterialX XML (gpuopen originals)       │
│  src/mat_vis_baker/   — fetch → bake → push pipeline            │
│  clients/             — Python, JS, Rust, Shell reference clients│
│  .dagger/             — Dagger CI pipeline                      │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼  Dagger / GH Actions bake (per-file substrate, ADR-0012)
┌─────────────────────────────────────────────────────────────────┐
│  ON HUGGING FACE DATASETS — gerchowl/mat-vis @ v2026.04.2       │
│                                                                 │
│  release-manifest.json                       — top-level index  │
│  <source>.json                               — material catalog │
│  <source>/<tier>/<material_id>/<channel>.png — texture file     │
│  <source>/<tier>/.tier_complete              — tier sentinel    │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼  one plain HTTP GET per texture (stdlib urllib, zero deps)
┌─────────────────────────────────────────────────────────────────┐
│  CONSUMER                                                       │
│                                                                 │
│  pip install mat-vis-client      (PyPI, zero deps)              │
│  — or —                                                         │
│  <script src="mat-vis-client.mjs">  (browser/Node)              │
│  — or —                                                         │
│  curl + jq (mat-vis.sh)                                         │
└─────────────────────────────────────────────────────────────────┘
```

## Sources

| Source | Materials | License | Content |
|---|---|---|---|
| [ambientcg](https://ambientcg.com) | ~1 965 | CC0-1.0 | PNG textures |
| [polyhaven](https://polyhaven.com) | ~756 | CC0-1.0 | PNG textures |
| [gpuopen](https://matlib.gpuopen.com) | ~454 | per-material | MaterialX + PNG textures |
| [physicallybased.info](https://physicallybased.info) | ~86 | CC0-1.0 | scalar only (IOR, roughness, color) |

## Resolution tiers

All tiers share the per-file substrate and client API. Each tier is
served as a flat tree of PNGs under `<source>/<tier>/<material>/`.
The latest release `v2026.04.2` ships 4 PNG tiers (1k, 512, 256, 128)
across ambientcg, polyhaven, and gpuopen, plus the scalar-only
physicallybased catalog. Sub-1k tiers are produced by `hf-derive`
(no upstream re-fetch); the 2k tier exists upstream and is rolled
out per-source as the bake schedule allows.

| Tier | Per material | Status (v2026.04.2) |
|---|---|---|
| 128 | ~10 KB  | released (ambientcg, polyhaven, gpuopen) |
| 256 | ~40 KB  | released (ambientcg, polyhaven, gpuopen) |
| 512 | ~150 KB | released (ambientcg, polyhaven, gpuopen) |
| 1k  | ~2 MB   | released (ambientcg, polyhaven, gpuopen) |
| 2k  | ~10 MB  | future — staged per-source |

## Client usage

### Feature matrix

The Python client is the reference implementation and full-featured. JS and Rust
clients are minimal per-file fetchers — the shell / SQL bindings are a couple
of curl lines. Pick based on runtime needs.

| Feature                                        | Python       | JS           | Rust         | Shell | SQL  |
| ---------------------------------------------- | :----------: | :----------: | :----------: | :---: | :--: |
| `fetch_texture` (per-file PNG GET)             | ✅           | ✅           | ✅           | ✅    | —    |
| Catalog discovery                              | ✅           | ✅           | ✅           | ✅    | —    |
| Per-source materials list                      | ✅           | ✅           | ✅           | —     | —    |
| Local file cache (`~/.cache/mat-vis/`)         | ✅           | —            | —            | —     | —    |
| Cache soft-cap + `MAT_VIS_CACHE_MAX_SIZE`      | ✅           | —            | —            | —     | —    |
| Per-file size cap (`MAT_VIS_MAX_FETCH_SIZE`)   | ✅           | —            | —            | —     | —    |
| Rate-limit auto-retry (429/503/403)            | ✅           | —            | —            | —     | —    |
| Redirect / signed-URL cache                    | ✅           | —            | —            | —     | —    |
| `search` by category + scalar ranges           | ✅           | —            | —            | —     | ✅   |
| `prefetch` bulk download                       | ✅           | —            | —            | —     | —    |
| MaterialX export (synthesized)                 | ✅           | —            | —            | —     | —    |
| MaterialX original (gpuopen)                   | ✅           | —            | —            | —     | —    |
| Format adapters (three.js, glTF)               | ✅           | —            | —            | —     | —    |
| Typed `RateLimitError` / `MatVisError`         | ✅           | —            | —            | —     | —    |
| CLI                                            | ✅           | ✅ (Node)    | ✅           | ✅    | —    |

If you need search, prefetch, MaterialX, or format adapters, use Python.
For drop-in per-file fetches in a browser or lightweight Rust binary, the smaller
clients have what you need.

### Python

```python
from mat_vis_client import MatVisClient

client = MatVisClient()  # defaults to v2026.04.2 (post-#243)

# fetch a single texture channel
png = client.fetch_texture("ambientcg", "Rock064", "color", tier="1k")
with open("rock064_color.png", "wb") as f:
    f.write(png)

# list available materials
for mat_id in client.materials("ambientcg", "1k"):
    print(mat_id)

# search across all sources (kwargs — category + optional scalar ranges)
results = client.search(category="stone", roughness_range=(0.4, 0.9))

# MaterialX export — dotted API
# Synthesized (always works: UsdPreviewSurface wrapper over our PNGs)
mtlx_path = client.mtlx("ambientcg", "Rock064", tier="1k").export("./out")

# Original upstream document (gpuopen today; None elsewhere)
orig = client.mtlx("gpuopen", "<material-uuid>").original
if orig is not None:
    xml = orig.xml                      # raw upstream XML
    orig.export("./out")                # PNGs + upstream mtlx with local paths

# Low-level adapters (generic: scalars dict + textures dict)
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
-- The per-source catalog is a JSON sidecar, served directly from HF
-- with the same one-GET semantics as the PNGs (ADR-0012).
SELECT id, source, category FROM
  read_json_auto(
    'https://huggingface.co/datasets/gerchowl/mat-vis/resolve/v2026.04.2/ambientcg.json'
  )
WHERE category = 'ceramic';
```

## Development

### Prerequisites

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- [Dagger](https://dagger.io) (CI pipeline)
- Nix + direnv (optional, provides full devShell)

### Local bake

The pipeline lives in `mat-vis-baker`. Each command writes to a
scratch `work_dir` and pushes per-file artifacts to the HF dataset
in atomic commits (one batch = one commit, bounded by count
*and* bytes per #228; see `--batch-size` / `--batch-max-bytes`).

```bash
uv sync
source .venv/bin/activate

# bake a single (source, tier) and push to HF (default repo: gerchowl/mat-vis).
# Scratch repos (*-tst) are the default safety net; --allow-prod is required
# for the canonical dataset.
mat-vis-baker hf-bake ambientcg 1k ./work \
  --release-tag v2026.04.2 \
  --repo-id gerchowl/mat-vis-tst

# derive a smaller PNG tier from an existing per-file HF tier (no upstream
# re-fetch). source-tier must already be baked; target-tier must be ≤ source-tier.
mat-vis-baker hf-derive \
  --source ambientcg --source-tier 1k --target-tier 512 \
  --release-tag v2026.04.2 --work-dir ./work \
  --repo-id gerchowl/mat-vis-tst

# inspect available subcommands (fetch, catalog, pack-mtlx, hf-derive-ktx2, ...)
mat-vis-baker --help
```

### Workflows

Production bakes run via two GitHub Actions dispatches:

- `.github/workflows/bake.yml` — `workflow_dispatch` with `sources` (one or
  more, or `sources=all`) × `tier`. Fans out via a matrix; matrix siblings
  serialize within one dispatch via `max-parallel: 1` (post-#235), so the
  HF commit-rate budget (128/hr/repo) stays comfortable.
- `.github/workflows/derive.yml` — same matrix shape, same serialization,
  for `hf-derive` runs (sub-1k tiers off an existing 1k bake).

Both default to the canonical `gerchowl/mat-vis` repo and require the
release tag as a dispatch input.

### Operator's guide: orphan LFS cleanup

Under the per-file substrate (ADR-0012) HF Hub uploads each LFS blob
*before* finalizing the commit. A mid-batch crash can therefore leave
orphan blobs on the object store — uploaded, but not referenced by any
committed file. Xet dedup makes future re-uploads bytes-free, so the
practical damage is the storage accounting line; cleanup is optional
but housekeeping-friendly.

```bash
# Dry-run audit against the scratch repo (default behaviour).
mat-vis-baker audit-orphans --repo gerchowl/mat-vis-tst

# Pin to a specific revision.
mat-vis-baker audit-orphans --repo gerchowl/mat-vis-tst --revision v2026.04.2

# Delete orphans (interactive: type DELETE to confirm).
mat-vis-baker audit-orphans --repo gerchowl/mat-vis-tst --delete

# Auditing the canonical prod repo requires --allow-prod.
mat-vis-baker audit-orphans --repo gerchowl/mat-vis --allow-prod

# Bypass the interactive prompt (e.g. inside a CI job):
MAT_VIS_AUDIT_FORCE=1 mat-vis-baker audit-orphans \
  --repo gerchowl/mat-vis-tst --delete
```

### Dagger CI

```bash
# smoke test
dagger call -m .dagger smoke --src=.

# full bake + HF push
dagger call -m .dagger bake-and-release \
  --src=. --source=ambientcg --tier=1k \
  --release-tag=v2026.04.2 --registry-pass=env:GITHUB_TOKEN
```

## Versioning

- **Data releases**: calver (`v2026.04.2`) — tied to upstream source updates
- **Code/client releases**: semver (`v0.6.x`) — API changes

**Release tags are immutable.** Once a CalVer tag is published (e.g.
`v2026.04.2`), the data at that revision will not change — bytes pinned
to a tag stay pinned. New upstream snapshots, fixes, or rebakes ship as
a new CalVer tag, never as an in-place rewrite of an existing one. This
contract is what lets clients use cheap `If-None-Match` conditional GETs
on the manifest (#258) and trust pinned-tag deployments across long
intervals without re-validating every byte.

## Key design decisions

Architecture is captured in [`docs/decisions/`](docs/decisions/). The
substrate that landed at v0.6.0 is described in the newer ADRs; the
earlier ones (ADR-0001…0007) describe storage predecessors that were
retired in #189.

1. [**ADR-0007**](docs/decisions/0007-substrate-move-to-hf-datasets-and-tar-container.md)
   — Substrate move from GitHub Releases to Hugging Face Datasets
   (the original container layout has since been superseded; see
   ADR-0012).
2. [**ADR-0008**](docs/decisions/0008-dataset-tree-as-source-of-truth.md)
   — The dataset tree (not a sidecar manifest) is the source of truth
   for what's published.
3. [**ADR-0010**](docs/decisions/0010-sharded-pipeline-via-gh-matrix.md)
   — Per (source × tier) bake jobs fanned out via GitHub Actions
   matrix; one job = one HF push.
4. [**ADR-0011**](docs/decisions/0011-mat-vis-curated-plus-upstream-mirror.md)
   — Two-layer index record: curated `mat_vis.*` block + optional
   verbatim `upstream` mirror.
5. [**ADR-0012**](docs/decisions/0012-per-file-substrate-drop-tar.md)
   — **Per-file substrate**: PNG-per-channel directly on HF, atomic
   per-batch commits, `.tier_complete` sentinel, `release-manifest.json`
   at root. Replaces the container layout from ADR-0007 (#189).

See the [ADR index](docs/decisions/README.md) for the full ordering,
including the retired storage predecessors (ADR-0001…0006).

## Upstream metadata vocabulary

The baker normalizes four upstream vocabularies (ambientcg,
polyhaven, gpuopen, physicallybased) onto 10 canonical categories.
The captured vocabulary — every category title and top-100 tag
per source, with counts — is committed as
[`docs/sources/metadata-vocabulary.md`](docs/sources/metadata-vocabulary.md)
(and the machine-readable sidecar `metadata-vocabulary.json`).
Regenerate with `uv run python scripts/probe-metadata-vocab.py`
when an upstream schema shifts.

## Relationship to mat

mat-vis is the **data factory**. [MorePET/mat][mat] is the
**user-facing library**.

| | mat | mat-vis (this repo) |
|---|---|---|
| What | Python API + material data | Data pipeline + hosting |
| Source data | TOML (physical properties) | .mtlx + JSON (appearance) |
| Artifact | PyPI wheel (~2 MB) | HF dataset (per-file PNGs) |
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
