# 0002. Hosting via GitHub Releases; updates via watch-and-PR

- Status: Accepted
- Date: 2026-04-16
- Deciders: @gerchowl

## Context

`mat-vis` needs to host 5–80 GB of versioned texture data (see
tier math in ADR-0003) and track upstream source changes across
four libraries (ambientcg, polyhaven, gpuopen, physicallybased).
Constraints:

- **Free hosting** — this is a research-tooling repo, not a
  funded product. We don't want a recurring cost line item.
- **CDN-backed** — consumers download from CI pipelines,
  Jupyter notebooks, and laptops around the world. Slow
  downloads kill adoption.
- **Versioned** — a release tag needs to pin a reproducible
  snapshot. `mat-vis==2026.04.0` should mean *exactly these
  material bytes forever*.
- **Change-auditable** — upstream sources update
  independently; we want reviewers to see what's changing
  before a new release goes live.
- **Language-agnostic HTTPS** — Python, Rust, JS, C++ consumers
  all need to fetch the same files via plain `curl`-style
  HTTPS with Range header support.

## Decision

**Host the data on GitHub Release assets under
`MorePET/mat-vis`. Update via a watch-and-PR workflow, not
calendar-based rebuilds.**

Concretely:

1. **Hosting layer**: each release publishes one or more
   Parquet files (per source × tier, partitioned per ADR-0003)
   as Release assets. GitHub Release URLs are stable,
   CDN-fronted, and free for public repos with no bandwidth cap.
2. **Change detection**: a `.github/workflows/watch.yml` runs
   daily, fetches the four upstream sources' indexes, and
   diffs them against `index/*.json` in the repo. When
   something changed, it opens a PR with the new index JSON(s)
   and a human-readable diff.
3. **Release trigger**: merging a change-PR to `main` fires
   `.github/workflows/release.yml`, which does the expensive
   work (bake MaterialX → flat PNGs, build Parquet files,
   upload as Release assets, tag the version).
4. **Versioning**: calver-ish tags (`vYYYY.MM.N`) match release
   dates and give a naturally ordered history.

## Consequences

**Enables**:

- **Zero ongoing cost.** Public GitHub repos get free Actions
  minutes, free Release asset storage, free Release asset
  bandwidth, and CDN distribution.
- **Reproducibility.** Tagged releases are immutable. Release
  assets aren't overwritten; a user who pins
  `mat-vis==2026.04.0` gets exactly those bytes forever.
- **Human-reviewable upstream changes.** Every PR from
  `watch.yml` has a diff a reviewer can approve or reject.
  If an upstream source renames a material or changes its
  shape in a way we don't want, the reviewer catches it
  before release.
- **No spurious rebuilds.** Calendar-based "rebuild every
  Sunday" pipelines produce new content hashes even when
  upstream didn't change, which means consumers see upgrade
  signals that aren't real upgrades. Watch-and-PR fires only
  on actual change.
- **Graceful source-outage handling.** If polyhaven's API is
  down when `watch.yml` polls, the workflow logs and exits
  cleanly. No failed partial-release.

**Costs**:

- **Bound to GitHub.** If GitHub revokes free Release asset
  hosting (no indication they would; public-repo bandwidth has
  been explicitly uncapped for years), we'd need to migrate.
  Mitigation: the release process is scripted; migrating to a
  different host (Hugging Face Datasets, Cloudflare R2,
  Zenodo) is a configuration change in `release.yml`.
- **One PR per upstream update.** A busy week could mean 3–5
  small PRs. Not burdensome but needs a reviewer. Mitigation:
  at least one maintainer watches merge-queue events. The
  PRs are small and well-scoped.
- **Limited release-asset file size.** GitHub caps single
  assets at 2 GB — drives the partitioning in ADR-0003.

**Rules out**:

- **Calendar-based rebuilds** (weekly cron, etc.) — produce
  spurious hash changes, waste CI minutes, and hide upstream
  changes behind "everything refreshed" diffs.
- **Hosting parquets on GitHub Pages directly.** GH Pages has
  a 1 GB soft limit — fits the 1K tier but nothing bigger.
  Good for the tiny JSON indexes and the optional
  web-shell bundle (ADR-0005), bad for tier data.
- **PyPI / crates.io as distribution**. PyPI caps individual
  wheels at 100 MB. Can't fit textures. Fine for the client
  packages themselves, which ship only the URL-table shim.

## Alternatives considered

- **Hugging Face Datasets**: generous free tier, fast CDN,
  Git-LFS-native. Viable as a mirror. Rejected as primary
  because it requires an HF account for some uses and has
  weaker alignment with the rest of the GitHub-centric
  workflow. Worth revisiting if bandwidth becomes an issue.
- **Zenodo**: free, unlimited, DOI-generating, CERN-backed.
  Rejected as primary because download speeds are slower than
  GitHub's CDN and the `publish-a-dataset-with-citation`
  ergonomics don't fit "weekly rolling releases." Good mirror
  candidate for tagged scientific releases with DOIs.
- **Cloudflare R2 / Backblaze B2**: cheap and fast. Free
  tiers are generous but capped; at mat-vis's projected
  scale we'd hit paid tiers. Rejected for the
  "zero ongoing cost" requirement.
- **Self-hosted on a VPS**: opposite of the cost requirement.
- **Calendar-based daily/weekly rebuild**: rejected, see
  "Rules out" above.

## Upgrade trigger

Revisit when any of:

1. **Bandwidth limits announced**. GitHub's current policy on
   public-repo Release asset bandwidth is uncapped. If that
   changes or if we start seeing throttling in practice,
   mirror to Hugging Face Datasets (primary) or Cloudflare R2
   (budget-dependent).
2. **Per-asset 2 GB cap raised**. Simplifies partitioning in
   ADR-0003.
3. **MaterialX source APIs change their discovery mechanism**.
   `watch.yml` assumes each source has a stable index or RSS
   feed we can poll. If a source goes behind auth or removes
   programmatic access, we'd need a different detection
   strategy or to drop that source.
4. **Publishing scientific citations becomes important**.
   Cross-post tagged releases to Zenodo for DOI generation.
   Doesn't change primary hosting.
