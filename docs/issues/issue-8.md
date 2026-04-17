---
type: issue
state: open
created: 2026-04-16T17:27:47Z
updated: 2026-04-16T17:27:47Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/8
comments: 0
labels: milestone
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:29.651Z
---

# [Issue 8]: [M4: Python client in mat — Material.textures works end-to-end](https://github.com/MorePET/mat-vis/issues/8)

## Goal

mat's built-in texture client (~150 lines) works end-to-end:
`Material("ambientcg/Metal064").properties.pbr.textures.color`
returns PNG bytes via rowmap + HTTP range read.

## Tasks (in MorePET/mat, not mat-vis)

- [ ] Implement texture client: rowmap fetch, HTTP range-read,
      cache write to `~/.cache/mat-vis/`
- [ ] Wire into `Material.properties.pbr.textures` — lazy load
      on first access
- [ ] `Material.prefetch(source, tier)` — bulk download
- [ ] `MAT_VIS_CACHE_DIR` env var support
- [ ] `cache=False` parameter for no-cache mode
- [ ] Integration test against a local test Parquet (from M1)

## Test cases

```python
def test_texture_fetch_and_cache(tmp_path):
    # Serve a test parquet + rowmap on localhost
    mat = Material("ambientcg/Metal064", tier="1k",
                   cache_dir=tmp_path)
    png = mat.properties.pbr.textures.color
    assert png[:4] == b'\x89PNG'

    # Second access hits cache, not network
    png2 = mat.properties.pbr.textures.color
    assert png2 == png

def test_prefetch(tmp_path):
    Material.prefetch(source="ambientcg", tier="1k",
                      cache_dir=tmp_path)
    # All rowmap entries should be cached
    cached = list(tmp_path.glob("ambientcg/1k/*/color.png"))
    assert len(cached) > 0
```

## Gate

`pip install mat` + `Material(...).properties.pbr.textures.color`
returns valid PNG bytes. Cache works.

## Related

- MorePET/mat#35 — output adapters (to_threejs, to_gltf)
