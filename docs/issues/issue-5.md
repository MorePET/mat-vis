---
type: issue
state: open
created: 2026-04-16T17:27:39Z
updated: 2026-04-16T17:27:39Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/5
comments: 0
labels: milestone
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:30.733Z
---

# [Issue 5]: [M1: Single-material smoke test (ambientcg, 1 material, 1K)](https://github.com/MorePET/mat-vis/issues/5)

## Goal

End-to-end proof: fetch one material from ambientcg, parse its
.mtlx, extract flat PNGs, write a Parquet file, emit a rowmap,
range-read back, verify bytes match.

## Tasks

- [ ] `sources/ambientcg.py` — fetch + extract one ZIP
- [ ] `bake.py` — parse .mtlx XML, copy referenced PNGs (no
      materialx dep needed for flat graphs)
- [ ] `parquet_writer.py` — write single-row Parquet with
      UNCOMPRESSED binary columns
- [ ] `parquet_writer.py` — emit rowmap JSON matching
      `docs/specs/rowmap-schema.json`
- [ ] `index_builder.py` — emit index JSON entry matching
      `docs/specs/index-schema.json`
- [ ] Round-trip test: range-read at rowmap offset → bytes
      match original PNG

## Test cases

```python
def test_single_material_roundtrip():
    """Fetch Metal064, bake, write parquet, read back via rowmap."""
    # 1. Fetch
    assets = ambientcg.fetch("Metal064", "1k", tmp_path)
    assert (tmp_path / "material.mtlx").exists()

    # 2. Bake (pure Python — flat graph)
    maps = bake.extract_flat_maps(tmp_path / "material.mtlx")
    assert "color" in maps
    assert maps["color"][:4] == b'\x89PNG'  # PNG magic

    # 3. Write Parquet + rowmap
    pq_path = tmp_path / "test.parquet"
    rowmap = parquet_writer.write(pq_path, [{"id": "Metal064", **maps}])
    assert pq_path.exists()
    assert "Metal064" in rowmap["materials"]

    # 4. Range-read back
    entry = rowmap["materials"]["Metal064"]["color"]
    with open(pq_path, "rb") as f:
        f.seek(entry["offset"])
        data = f.read(entry["length"])
    assert data == maps["color"]  # exact match
```

## Gate

Test passes. One material, one tier, full pipeline.
