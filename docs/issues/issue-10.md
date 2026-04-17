---
type: issue
state: open
created: 2026-04-16T17:27:54Z
updated: 2026-04-16T17:27:54Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/10
comments: 0
labels: milestone
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:28.934Z
---

# [Issue 10]: [M6: 4K tier with category partitioning](https://github.com/MorePET/mat-vis/issues/10)

## Goal

4K tier with category-based partitioning per ADR-0003. Each
partition < 2 GB. Rowmap handles partition lookup transparently.

## Tasks

- [ ] Partitioner in `parquet_writer.py` — split by category
- [ ] Rowmap extended: material → partition file + offset/length
- [ ] Naming: `mat-vis-<source>-4k-<category>.parquet`
- [ ] Per-partition rowmaps or one combined rowmap per
      (source × tier)
- [ ] All partitions < 2 GB

## Test cases

- Each partition validates as Parquet with correct schema
- Combined rowmap covers all materials across all partitions
- Range-read works when rowmap points at a specific partition file
- No material appears in more than one partition

## Gate

4K tier published. All partitions < 2 GB. Client handles
partitions transparently.
