---
type: issue
state: open
created: 2026-04-16T17:27:51Z
updated: 2026-04-16T17:27:51Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/9
comments: 0
labels: milestone
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:29.287Z
---

# [Issue 9]: [M5: watch.yml + release.yml live — first published release](https://github.com/MorePET/mat-vis/issues/9)

## Goal

End-to-end automated pipeline: watch detects upstream change →
PR → merge → bake → Parquet + rowmap uploaded as Release assets
→ calver tag.

## Tasks

- [ ] `.github/workflows/watch.yml` — daily poll all 4 sources,
      diff against `index/*.json`, open PR on change
- [ ] `.github/workflows/release.yml` — on tag push: bake all
      tiers, build Parquets + rowmaps, upload as Release assets,
      publish `release-manifest.json`
- [ ] `release.yml` uses `ghcr.io/morepet/mat-vis-baker` container
      to avoid rebuilding deps
- [ ] First manual release: tag `v2026.xx.0`, verify assets
- [ ] `release-manifest.json` contains per-tier base URLs
- [ ] GitHub App secrets (COMMIT_APP_*, RELEASE_APP_*) configured

## Test cases

- watch.yml: manually trigger, verify it opens a PR with index
  diff (or "no changes" log)
- release.yml: manually trigger on a tag, verify Release assets
  appear with correct names
- Download a rowmap + range-read a material from the published
  Release → valid PNG

## Gate

First calver release published. Assets downloadable. Range-read
works against published URLs.
