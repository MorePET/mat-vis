# User stories

How people use mat (with mat-vis behind the scenes). Each story
shows the path through the system and what the user doesn't
need to care about.

## 1. Radiation physicist — scalars only

`pip install mat` on an HPC login node.

```python
from pymat import Material
steel = Material("Stainless Steel 316L")
print(steel.density, steel.composition)
```

Scalars come from TOML data shipped in the wheel. No network,
no Parquet, no textures. Works offline immediately.

## 2. build123d CAD user — realistic materials

Already has build123d + ocp_vscode. Adds `pip install mat`.

```python
from build123d import Box
from pymat import Material

shape = Box(10, 10, 10)
shape.material = Material("polyhaven/wood_table_001")
show(shape)  # ocp_vscode renders with PBR textures
```

First call: ~1 second (lazy cache fetches PNG bytes via HTTP
range read). Every subsequent `show()`: instant from disk.
User never learns what a Parquet or rowmap is.

## 3. Data scientist — exploring the corpus

```python
from pymat import materials

# Filter against the JSON index (~3100 entries, in memory)
woods = [m for m in materials()
         if m["category"] == "wood" and m["roughness"] < 0.4]
```

No DuckDB, no SQL, no pyarrow. It's a list of dicts.

Power users who prefer SQL can query the Parquet files directly:

```sql
-- Their DuckDB install, their choice
SELECT * FROM 'https://github.com/MorePET/mat-vis/releases/...'
WHERE category = 'wood' AND roughness < 0.4
```

## 4. CI pipeline — reproducible renders

```yaml
- run: pip install mat==2.2.0
- run: python -c "from pymat import Material; Material.prefetch(source='ambientcg', tier='2k')"
- run: python render_benchmark.py  # reads from cache, no network
```

Calver release tags on mat-vis pin byte-exact assets. mat's
semver pins the API. Reproducible.

## 5. Air-gapped researcher

On an internet-connected machine:

```bash
curl -LO https://github.com/MorePET/mat-vis/releases/download/v2026.04.0/mat-vis-ambientcg-2k.parquet
curl -LO https://github.com/MorePET/mat-vis/releases/download/v2026.04.0/ambientcg-2k-rowmap.json
```

Transfer via USB. On the air-gapped machine:

```bash
export MAT_VIS_CACHE_DIR=/mnt/transfer/mat-cache
python my_script.py  # reads from local cache, no network
```

## 6. Source contributor

Opens a PR adding a new source adapter to `watch.yml` + a
`sources/<name>.py` fetcher. On merge, `release.yml` bakes at
all tiers and publishes new Parquet assets. Next mat release
exposes the source transparently.

Requirements: CC0 / Apache / BSD-3 / MIT license only.

## Anti-stories

- **Realtime streaming.** Not the model — static assets + cache.
- **Mutating materials upstream.** Sources are upstream-owned.
- **Hosted SQL endpoint.** 3100 materials is a dict, not a database.
- **Animated / time-varying materials.** Out of scope.
