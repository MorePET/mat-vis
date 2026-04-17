---
type: issue
state: open
created: 2026-04-16T17:27:42Z
updated: 2026-04-16T17:27:42Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/6
comments: 0
labels: milestone
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:30.395Z
---

# [Issue 6]: [M2: Full ambientcg ingest at 1K](https://github.com/MorePET/mat-vis/issues/6)

## Goal

Ingest all ~2000 ambientcg materials at 1K. Validate Parquet
size, rowmap completeness, index schema conformance.

## Tasks

- [ ] Batch fetch all ambientcg materials (paginated API)
- [ ] Batch bake (pure Python for all — ambientcg is 100% flat)
- [ ] Write `mat-vis-ambientcg-1k.parquet` (< 2 GB)
- [ ] Write `ambientcg-1k-rowmap.json`
- [ ] Write `index/ambientcg.json`
- [ ] Validate index against `docs/specs/index-schema.json`
- [ ] Validate rowmap against `docs/specs/rowmap-schema.json`
- [ ] Spot-check 10 random materials via rowmap range-read

## Test cases

```python
def test_ambientcg_full_ingest():
    # Parquet exists and is under 2 GB
    assert pq_path.stat().st_size < 2 * 1024**3

    # Rowmap covers all materials in the index
    index = json.loads(index_path.read_text())
    rowmap = json.loads(rowmap_path.read_text())
    for entry in index:
        assert entry["id"] in rowmap["materials"]

    # Spot-check: range-read 10 random materials
    for mat_id in random.sample(list(rowmap["materials"]), 10):
        for channel, r in rowmap["materials"][mat_id].items():
            with open(pq_path, "rb") as f:
                f.seek(r["offset"])
                data = f.read(r["length"])
            assert data[:4] == b'\x89PNG'
```

## Gate

Full 1K corpus for one source. Parquet + rowmap + index valid.
