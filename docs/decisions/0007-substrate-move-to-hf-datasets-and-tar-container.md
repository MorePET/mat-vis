# 0007. Substrate move to Hugging Face Datasets + tar container, drop per-category partitioning

- Status: Accepted
- Date: 2026-04-19
- Deciders: @gerchowl
- Supersedes: ADR-0001 (storage container), ADR-0002 (hosting substrate), ADR-0003 (per-category partitioning)
- Tracking: umbrella issue #100, milestone `v0.5.0 — HF substrate`

## Context

ADR-0001 chose **Parquet bundles + rowmap JSON sidecar on GitHub Releases**, partitioned per `(source, tier, category)`. ADR-0002 chose **GitHub Releases as the CDN**, ADR-0003 chose **per-category partitioning** for chunk-size management. After 5 months of operation those choices produced four recurring bug classes:

| Bug class | Example | Root | Substrate-attributable? |
|---|---|---|---|
| Dangling rowmap → missing parquet | #79 (4 missing parquets in v2026.04.0) | Non-atomic multi-file upload | Yes — GitHub Releases has no atomic multi-file commit |
| Empty-materials rowmap | #82 | Per-pipeline copy-paste of rowmap-emit loop iterating wrong dict | Partial — cross-pipeline drift, magnified by GH's clobber semantics |
| Categorization drift between pipelines | #98 | Same fact (filename schema) parsed by 3 different regexes | No — design-internal |
| Massively incomplete index files | #99 (ambientcg.json: 15/1965 entries; polyhaven.json: 26/753) | Index writers clobber rather than merge across batches | Partial — clobber-friendly substrate makes it worse |

### Falsify reviews (2026-04-19)

Three independent adversarial reviewers were spawned with no shared context, each told to **refute** the claim that the current architecture was minimum-complexity. All three returned counterexamples; the agents and verdicts are filed in the umbrella issue.

| Angle | Concrete simplification |
|---|---|
| First-principles design-from-scratch | Drop the `category` partitioning dimension — it lives where the schema already says it should (column inside the file + field in `index.json`). Collapses ~120 file pairs to ~12 per release; eliminates "missing parquet per category" + "categorization drift" structurally. |
| Substrate choice | Move from GitHub Releases to Hugging Face Datasets. Atomic multi-file commits eliminate #79 by construction; immutable revisions eliminate #98/#99 drift class; no 2 GB-per-file cap removes the chunk-rotation state machine; CloudFront-backed range reads eliminate the signed-URL retry/cache complexity in the client. |
| Implementation reality | The same fact (filename schema, payload length, source-index entries) is encoded in N places that drifted. Consolidate behind single primitives: one `parse_release_filename`, one `RowmapCollector`, one source-index writer. |

### Upstream license verification

| Source | License | HF-redistributable |
|---|---|:---:|
| ambientcg | CC0-1.0 | ✅ |
| polyhaven | CC0-1.0 | ✅ |
| physicallybased | CC0-1.0 | ✅ |
| gpuopen | MIT (©2022 AMD; verified via matlib.gpuopen.com per-material display) | ✅ |

`src/mat_vis_baker/sources/gpuopen.py` previously stamped `source_license="TBV"` — fix is part of Phase 0.

## Decision

Combine all three reviewers' simplifications into a single architectural cut, **v0.5.0**.

### Substrate

- **Hugging Face Datasets**, repo `huggingface.co/datasets/gerchowl/mat-vis` (personal account; transfer to org TBD).
- **Atomic commits** via `huggingface_hub.HfApi.create_commit` — manifest + every parquet/tar lands together or not at all.
- **Tag-named revisions** retain calver naming (`v2026.04.1`, …) — same external version model.
- **CloudFront-backed CDN** — `https://huggingface.co/datasets/gerchowl/mat-vis/resolve/<revision>/<path>`. Range reads work natively, no signed URLs.

### Container

- **Tar archives** replace Parquet bundles. One tar per `(source, tier)`.
- **Layout inside tar**: `{material_id}/{channel}.png` (or `.ktx2`).
- **Sidecar rowmap** — same JSON shape as today: `{material_id: {channel: {offset, length}}}`. Offset points at first byte after the 512-byte tar header; length is the channel's byte count.
- **No per-category partitioning** — category becomes a column-equivalent (a field in `index/{source}.json`) and a tag in the `release-manifest.json`. Search-by-category remains a client-side filter on the index, exactly as today.

### Files per release (target)

```
v2026.04.1/
  release-manifest.json              # {tier: {source: {tar_url, rowmap_url, materials_count}}}
  ambientcg.json                     # catalog: id/category/scalars/source_url/source_license
  polyhaven.json
  gpuopen.json
  physicallybased.json
  ambientcg-1k.tar       + ambientcg-1k-rowmap.json
  ambientcg-2k.tar       + ambientcg-2k-rowmap.json
  polyhaven-1k.tar       + polyhaven-1k-rowmap.json
  ...                                 # 12 source/tier combos × 2 = 24 PNG files
  ktx2/ambientcg-1k.tar  + ktx2/ambientcg-1k-rowmap.json
  ...                                 # 12 KTX2 combos × 2 = 24 KTX2 files
  gpuopen-mtlx.json + polyhaven-mtlx.json + ambientcg-mtlx.json
```

Total: **~57 files per release** (vs ~440 today).

### Client

- Base URL: `https://huggingface.co/datasets/gerchowl/mat-vis/resolve/<revision>/<path>`.
- Range arithmetic: `bytes={offset}-{offset+length-1}` against `<base>/<source>-<tier>.tar`.
- Delete: `_redirect_cache`, signed-URL stale retry, GH-specific 60 req/h rate-limit branch.
- Keep: `RateLimitError`, `MAX_RETRIES`, exponential backoff (HF rate limits are looser but real).

### Workflow collapse

- DELETE: `release-validate.yml` — immutable revisions don't drift.
- DELETE: `rebuild-manifest.yml` — atomic commits don't desync.
- DELETE: `regenerate-rowmaps.yml` — sidecar `RowmapCollector` is authoritative; no scanner re-derivation.
- DELETE: `promote-data-release.yml` — HF revisions are immutable, no pre-release / promote dance.
- REWRITE: `bake.yml` — calls baker which pushes to HF directly via `HF_TOKEN` secret.
- KEEP: `derive-ktx2.yml` — KTX2 transcode pipeline, just pushes to HF instead of GH Releases.
- KEEP unchanged: `pypi.yml`, `ci.yml`, `labeler.yml`.

## Consequences

### Eliminated by construction

- **#79** dangling rowmap → missing parquet (atomic commits)
- **#98** categorization drift between pipelines (no per-category partitioning to drift over)
- **#99** index incompleteness via clobber (atomic commits + single index writer per source)
- **#83** chunk-split inconsistency across pipelines (no chunk-split needed; HF takes 50 GB+ files)
- **~330 lines** of substrate-coping code in `upload.py` + `client.py`

### Still requires code changes

- **#82** empty-materials rowmap — partially fixed in current arch (`emit_rowmaps_for_bake` consolidation already landed). The new tar-based emitter must preserve the same "always emit, even if empty" invariant.

### New constraints

- **HF account dependency** — single-vendor risk. Mitigation: HF Datasets is git-LFS-backed and downloadable verbatim; can mirror to a second substrate (Zenodo for archival) if HF ever changes terms.
- **License attribution** — MIT requires preserving AMD copyright + license text. Handled via `LICENSES/` directory at dataset root + dataset card.

### Back-compat

- `mat-vis-client 0.4.x` continues working against `v2026.04.0` on GitHub Releases. The release stays as-is, frozen, no further updates.
- `mat-vis-client 0.5.0+` only knows the HF substrate. New base URL, new tar-based access pattern.
- A grace period of "both work" is unavoidable but bounded — once 0.5.0 is published, encourage migration via the update-check notice.

### Testing strategy

- Tar roundtrip + offset arithmetic tests in `tests/test_tar_writer.py` (new).
- HF-push primitive mocked via `huggingface_hub` test client.
- End-to-end proof bake of `physicallybased` (smallest source, scalar-only) for Phase 2.
- Live tests in `clients/python/test_client.py` repointed to the new HF URL.

## Upgrade triggers

Revisit this ADR if:

- HF changes pricing model for public datasets to non-free.
- HF rate-limits become more restrictive than GitHub's (currently looser).
- A multi-vendor mirror requirement emerges (governance, durability concerns).
- The corpus exceeds HF's practical limits (no documented cap; LAION-5B at 240 TB is the public reference point — we are nowhere near).
