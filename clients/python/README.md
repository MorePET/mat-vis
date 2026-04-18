# mat-vis-client

Pure Python client for [mat-vis](https://github.com/MorePET/mat-vis) —
PBR texture data distributed as Parquet files on GitHub Releases.

- **Zero dependencies** — stdlib only (`urllib`). No pyarrow, no numpy, no binary wheels.
- **HTTP range reads** — fetch a single texture channel with one ranged GET against a
  consolidated Parquet asset. No whole-archive downloads.
- **Local cache** — results cached under `~/.cache/mat-vis/` with a configurable soft cap.
- **Search** — filter materials by category, roughness / metalness ranges, source, tier.
- **MaterialX export** — synthesized `UsdPreviewSurface` documents or original
  upstream authored documents (gpuopen).
- **Format adapters** — three.js, glTF 2.0 KHR_materials_pbrSpecularGlossiness, MaterialX.

---

## Install

```bash
pip install mat-vis-client
```

Python 3.10+.

## Quick start

```python
from mat_vis_client import MatVisClient

client = MatVisClient()  # latest release
png = client.fetch_texture("ambientcg", "Rock064", "color", tier="1k")

with open("rock.png", "wb") as f:
    f.write(png)
```

## Search

```python
results = client.search(
    category="stone",
    roughness_range=(0.4, 0.9),
    source="ambientcg",
)
for r in results:
    print(r["id"], r["category"], r["roughness"])
```

Categories come from the release manifest (metal, wood, stone, fabric, plastic,
concrete, ceramic, glass, organic, other). Inspect them at runtime with
`client.categories()`.

## MaterialX

Synthesized `UsdPreviewSurface` wrapper — always available:

```python
src = client.mtlx("ambientcg", "Rock064", tier="1k")
src.export("./out")        # writes .mtlx + referenced PNGs
xml = src.xml              # raw MaterialX XML
```

Original upstream document (gpuopen materials only; `None` elsewhere):

```python
orig = client.mtlx("gpuopen", "<uuid>").original
if orig is not None:
    orig.export("./out")
```

## Bulk prefetch

```python
n = client.prefetch("ambientcg", tier="1k")  # download + cache every material
```

## CLI

```bash
mat-vis-client list                              # sources × tiers
mat-vis-client materials ambientcg 1k            # IDs
mat-vis-client fetch ambientcg Rock064 color 1k -o rock.png
mat-vis-client search metal --roughness 0.2:0.6
mat-vis-client prefetch ambientcg 1k
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MAT_VIS_CACHE` | `~/.cache/mat-vis` | Cache directory |
| `MAT_VIS_CACHE_MAX_SIZE` | `5GB` | Soft cap — warning only, no eviction |
| `MAT_VIS_MAX_FETCH_SIZE` | `500MB` | Hard cap per range-read (OOM defense) |
| `MAT_VIS_MAX_RETRIES` | `5` | Rate-limit retry attempts |
| `MAT_VIS_BACKOFF_BASE` | `1.0` | Exponential backoff base (seconds) |
| `MAT_VIS_NO_UPDATE_CHECK` | — | Disable the once-a-day PyPI update check |
| `MAT_VIS_UPDATE_CHECK` | — | Force update check even when stderr is not a TTY |

## Rate limits

Backed by GitHub's unauthenticated rate limit (60 req/h). The client:

- Caches signed CDN URLs for 4 min to skip the github.com redirect
- Retries 429 / 503 / rate-limited 403 with `Retry-After` / `X-RateLimit-Reset` honoring
- Raises `mat_vis_client.RateLimitError` when retries are exhausted

## Adapters

```python
from mat_vis_client import to_threejs, to_gltf, export_mtlx

scalars = {"roughness": 0.5, "metalness": 0.0}
textures = client.fetch_all_textures("ambientcg", "Rock064", tier="1k")

three = to_threejs(scalars, textures)
gltf = to_gltf(scalars, textures)
```

## Links

- Source / issue tracker: <https://github.com/MorePET/mat-vis>
- Changelog: <https://github.com/MorePET/mat-vis/blob/main/CHANGELOG.md>
- Data license: CC0 (AmbientCG, PolyHaven) + MIT (GPUOpen) per material
