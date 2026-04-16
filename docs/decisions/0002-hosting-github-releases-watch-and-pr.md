# 0002. Hosting via GitHub Releases; updates via watch-and-PR

- Status: Accepted
- Date: 2026-04-16
- Deciders: @gerchowl

## Context

mat-vis needs to host 5–80 GB of versioned texture data (1K–4K
tiers on GitHub; 8K on Hugging Face, see note below) and track
upstream changes across four sources. Constraints:

- **Free hosting** — research tooling, no recurring cost.
- **CDN-backed** — consumers worldwide need fast downloads.
- **Versioned** — a release tag pins reproducible bytes forever.
- **Change-auditable** — reviewers see what changed before release.
- **Plain HTTPS with Range headers** — any language, any tool.

## Decision

**Host on GitHub Release assets. Update via watch-and-PR, not
calendar rebuilds.**

1. **Hosting**: each release publishes Parquet files + rowmap
   JSONs as Release assets. CDN-fronted, free for public repos.
2. **Change detection**: `watch.yml` runs daily, polls upstream
   indexes, diffs against `index/*.json` + `mtlx/`, opens a PR.
3. **Release trigger**: merging a change-PR to main fires
   `release.yml` — bake, build Parquet, upload assets, tag.
4. **Versioning**: calver tags (`vYYYY.MM.N`).

### Tiered hosting for large tiers

GitHub's Acceptable Use Policy reserves the right to act on
"significantly excessive" usage without defining thresholds.
1K + 2K (~30 GB) is comfortably within norms. 4K (~80 GB) is
defensible. 8K (~300 GB) is not.

Strategy: **1K–4K on GitHub Releases, 8K on Hugging Face
Datasets.** The rowmap's per-tier base URL makes this
transparent — the client doesn't know or care where the bytes
physically live.

```json
{
  "1k": {"base_url": "https://github.com/MorePET/mat-vis/releases/download/v2026.04.0/"},
  "2k": {"base_url": "https://github.com/MorePET/mat-vis/releases/download/v2026.04.0/"},
  "4k": {"base_url": "https://github.com/MorePET/mat-vis/releases/download/v2026.04.0/"},
  "8k": {"base_url": "https://huggingface.co/datasets/MorePET/mat-vis-8k/resolve/main/"}
}
```

## Consequences

**Enables**:

- **Zero cost** for 1K–4K. HF Datasets is also free for public.
- **Reproducibility.** Tagged releases are immutable.
- **Human-reviewable changes.** Every PR has diffable JSON + .mtlx.
- **No spurious rebuilds.** Fires only on actual upstream change.
- **Transparent multi-host.** Per-tier base URLs in the manifest.

**Costs**:

- **Bound to GitHub + HF.** Mitigated: the release process is
  scripted; migrating hosts is a config change.
- **One PR per upstream update.** Could be 3–5/week. Small, scoped.
- **2 GB per-asset limit.** Drives partitioning (ADR-0003).

## Alternatives considered

- **Hugging Face only** — viable, but weaker GitHub workflow
  integration. Good mirror, not primary for 1K–4K.
- **Zenodo** — free, DOI-generating, CERN-backed. Slow downloads,
  poor fit for rolling releases. Good for tagged scientific
  snapshots.
- **Cloudflare R2 / Backblaze B2** — cheap but not free at scale.
- **Calendar-based rebuilds** — spurious hash changes, wasted CI.

## Upgrade triggers

1. **GitHub throttling observed.** Mirror everything to HF.
2. **Per-asset 2 GB cap raised.** Simplifies ADR-0003.
3. **Source API goes behind auth.** Need new detection strategy.
4. **Scientific citations needed.** Cross-post to Zenodo for DOIs.
