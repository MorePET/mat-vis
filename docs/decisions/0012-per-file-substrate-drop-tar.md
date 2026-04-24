# ADR-0012: per-file substrate on Hugging Face — drop the tar container

- Status: Accepted
- Date: 2026-04-21
- Deciders: @gerchowl
- Supersedes (in part): ADR-0007 (tar container), ADR-0010's
  sharded-tar / merge-shards pipeline
- Related: ADR-0008 (tree-as-SoT), ADR-0011 (mat_vis curated record)
- Milestone: v0.6.0 — clean break, no back-compat aliases

## Context

ADR-0007 established a per `(source, tier)` **tar container**: one
`polyhaven-1k.tar` holds every channel for every material, with a
sidecar `polyhaven-1k-rowmap.json` giving byte offset + length for
each channel. Clients do HTTP Range reads against the tar URL.

ADR-0010 layered sharding on top: the tar can be split into K
`.shard-N-of-K.tar` files, each atomic-committed independently, then
reassembled by `mat-vis-baker merge-shards`.

During the v0.6.0 staging bake the **ambientcg 2k tar reached ~70 GB
locally and filled the 126 GB `/home` on anvil-dev** (#179) — the
bake wedged mid-write, leaving no durable state. Sharding at K=8
relieves the pressure but scales linearly: 4k and 8k tiers would
need K=32+, which starts fighting HF's commit-rate ceiling.

## Decision

**Drop the tar. Store textures as one file per (source, tier,
material, channel) directly on HF.**

```
gerchowl/mat-vis/resolve/<tag>/
├── polyhaven.json                           # catalog (unchanged)
├── polyhaven/
│   ├── 1k/
│   │   ├── aerial_asphalt_01/
│   │   │   ├── color.png
│   │   │   ├── normal.png
│   │   │   └── roughness.png
│   │   └── …
│   ├── 512/…
│   └── ktx2-1k/
│       └── aerial_asphalt_01/color.ktx2, …
└── physicallybased.json                     # scalar-only (no tier dirs)
```

Metadata (`mat_vis` block, `upstream` mirror) stays in the
per-source catalog JSON at repo root — same shape as ADR-0011.
Only **textures** move from tar to per-file. The rowmap JSON
disappears entirely — tree listing replaces it.

The atomicity unit shrinks from "one tier = one tar commit" to
"one batch of materials = one commit" (bound by HF's 10k
files-per-directory cap; our subdirectory-per-material layout
keeps each directory at ≤7 entries so the practical commit
ceiling is ~13k files ≈ a full source × tier).

## Measured invariants

Empirical probe against `gerchowl/mat-vis-tst` (2026-04-21):

| Files/commit | Wall-clock | Outcome |
|---:|:---|:---|
| 100    | 3.8 s | ✅ |
| 1,000  | 9.7 s | ✅ |
| 5,000  | 38 s  | ✅ |
| 10,000 | 88 s  | ✅ |
| 25,000 | 193 s | ❌ `too many files per directory` |

The real HF limit is **10,000 files per directory**, not per commit.
Our subdirectory-per-material layout fits with enormous headroom.

## Consequences

**Good** (net-positive LOC, simpler ops):
- Peak local disk = O(one batch of materials), not O(full tar).
  ambientcg 2k at batch=50 peaks at <1 GB local instead of 70 GB.
- `src/mat_vis_baker/tar_writer.py` (99 LOC) deleted.
- `src/mat_vis_baker/merge_shards.py` (289 LOC) deleted.
- `mat-vis-baker merge-shards` CLI subcommand retired.
- Rowmap JSON format + test surface retired.
- HTTP Range code in 4 clients (Python, standalone, JS, Rust,
  shell) replaced by plain GET on `<source>/<tier>/<mid>/<channel>.png`.
  The shell client becomes a one-line `curl` for a single texture.
- Xet's chunk-level CDC deduplicates identical channel bytes
  **across tiers automatically** — storing `color.png` at 128 /
  256 / 512 / 1k costs one xorb, not four (tar substrate pays
  four copies).
- Batch commits become durable checkpoints. Mid-bake crash at
  material 1500/1993 keeps 1450 committed; next run picks up from
  1451 via a tree-listing pre-flight. Resumable by construction.

**Bad / accepted tradeoffs:**
- Client needs a minor version bump. Old clients reading old tags
  keep working because the tar tags (v2026.04.x) remain frozen on
  HF. New clients only read v2026.05.x+ per-file tags.
- Sharding (#134) becomes redundant for the disk-size reason it
  existed. Shard-aware CLI stays as a way to parallelize work
  across runners (not to bound disk), but `merge-shards` has no
  equivalent under per-file and retires.
- HF commit rate limit (~10–20/hr/user, HF-API probe) budgets how
  many tier-commits we can queue per release cycle. ~15 source×tier
  combos per release fits comfortably.
- Mid-batch crashes leave orphan LFS blobs on HF's object store
  (no documented auto-GC for dataset repos). Next bake re-uploads
  → Xet SHA-check makes the reupload bytes-free, but an orphan
  housekeeping command is a follow-up (`baker audit-orphans`).

## Alternatives rejected

- **Push sharding further (Option A in #182):** zero new code, but
  K=32 on 8k tiers fights commit-rate budget and keeps 4 clients
  forever carrying tar+range code. Architect call: local minimum.
- **LFS multipart streaming of one tar (Option B):** breaks Xet
  chunk-dedup (falls back to legacy LFS HTTP when BinaryIO
  streamer is used), orphan-blob risk on mid-stream crash, no
  resume. All four reviewers ranked it last.

## Implementation notes (v0.6.0)

- New `src/mat_vis_baker/hf_bake_per_file.py` with
  `bake_one_per_file(source, tier, release_tag, hf_token, repo_id,
  batch_size=50, allow_prod=False)`. Pre-flight tree scan skips
  already-committed materials. Batch commits every `batch_size`
  materials. Each batch = one `HfApi.create_commit` with up to
  `batch_size × channels` `CommitOperationAdd` entries.
- `hf_bake.bake_one` routes textured sources to `bake_one_per_file`;
  `bake_scalar_source` for physicallybased is unchanged (the catalog
  path was already per-file).
- Derive (`hf_derive.py`) changes: no tar to range-read. Clients
  GET individual PNGs, bake writes resized PNGs. HTTP-Range code
  replaced by plain GET per channel.
- Client (`MatVisClient.fetch_texture`): remove rowmap read,
  remove Range header, plain GET on the resolve URL.
- Dagger (`_baker_container`): unchanged; it's a runtime, not a
  substrate concept.

## Review trail

- /spike 182 filed, 4 parallel reviewers (HF-API, systems architect,
  client I/O, ops burden). Architect winner was C; client winner was
  C; HF-API specialist and ops-burden preferred A short-term.
- User selected option 2 ("straight to C, autonomously") after the
  empirical probe confirmed the 10k-files-per-directory cap is the
  only real HF constraint and our subdirectory layout fits.
- ADR-0007's "atomic unit = tier" becomes "atomic unit = batch".
  Migration: each tier writes a `.tier_complete` sentinel file as
  its final batch commit, restoring tier-level atomicity observable
  by clients (ADR-0008 tree-as-SoT already makes this check cheap).

## References

- #182 — spike issue (Option C chosen)
- #179 — staging-bake-disk-wall that triggered the spike
- ADR-0007 — original tar substrate
- ADR-0008 — tree-as-SoT (extends to per-material granularity)
- ADR-0011 — two-layer record contract (unchanged under C)
