---
type: issue
state: open
created: 2026-04-16T17:27:44Z
updated: 2026-04-16T17:27:44Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/7
comments: 0
labels: milestone
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:30.048Z
---

# [Issue 7]: [M3: All 4 sources at 1K + 2K](https://github.com/MorePET/mat-vis/issues/7)

## Goal

All four sources ingested at 1K and 2K. Index JSONs and .mtlx
files committed to git. Parquets + rowmaps as local artifacts
(not yet published as Release assets).

## Tasks

- [ ] `sources/polyhaven.py` — handle individual file downloads
      + EXR displacement (convert to PNG-16)
- [ ] `sources/gpuopen.py` — handle ZIP downloads. Flag layered
      materials that need materialx baking (defer or handle)
- [ ] `sources/physicallybased.py` — scalar-only index, no
      textures
- [ ] All 4 index JSONs validate against schema
- [ ] All .mtlx files committed under `mtlx/<source>/`
- [ ] 8 Parquet files (4 sources × 2 tiers)
- [ ] 8 rowmap JSONs
- [ ] Total 1K corpus ~5 GB, 2K corpus ~25 GB

## Test cases

- Each source's index validates against index-schema.json
- Each source's rowmap validates against rowmap-schema.json
- Spot-check range-reads across all sources
- physicallybased has no texture columns (all null), but scalar
  metadata is complete

## Gate

4 sources × 2 tiers, all valid. .mtlx + index in git.
