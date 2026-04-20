# 0008. Dataset tree is the source of truth; `release-manifest.json` is an optional convenience snapshot

- Status: Accepted
- Date: 2026-04-20
- Deciders: @gerchowl
- Partly supersedes: ADR-0007 (the initial HF substrate rollout shipped
  with a single `release-manifest.json` read-modify-write on every bake
  and derive)

## Context

ADR-0007 moved the substrate from GitHub Releases + parquet to HF
Datasets + tar. In the initial implementation, **every bake and
derive run did read-modify-write on `release-manifest.json` at the
repo root**, and on each `<source>.json` catalog's
`available_tiers` field. Under sequential operation this was fine;
under the concurrent derive matrix (15 runs at once for
`v2026.04.1`) it produced clobber races — two runs reading the same
base manifest, each merging in their own tier entry, the second push
overwriting the first's addition.

Workaround shipped: serial dispatch. 1.5 hour single-threaded runs
instead of 15 minutes of parallel.

## Falsify

Every field in the materialized manifest is **derivable** from the
dataset tree listing:

| Field | Derivation |
|---|---|
| `schema_version` | Constant per client version |
| `release_tag` | Identical to the branch/tag the client pinned |
| `sources[src].catalog` | Filename convention: `<src>.json` |
| `sources[src].tiers[t]` | Filenames: `<src>-<t>.tar` + `<src>-<t>-rowmap.json` |
| `sources[src].materials_count` | `len(catalog)` when asked |

One HTTPS GET of `/api/datasets/<repo>/tree/<revision>?recursive=true`
returns everything. The "manifest" is a materialized view of the
tree, not a source of truth. Treating the view as SoT is what
introduced the race class.

## Decision

Drop `release-manifest.json` as a baker write target. Clients build
the manifest in memory from the tree listing. Every bake/derive run
writes only files unique to its own output:

- initial bake: `<src>.json` (only if no remote copy exists),
  `<src>-<t>.tar`, `<src>-<t>-rowmap.json`;
- derive resize: `<src>-<t>.tar`, `<src>-<t>-rowmap.json`;
- derive KTX2: `ktx2/<src>-<t>.tar`, `ktx2/<src>-<t>-rowmap.json`.

All unique paths → no concurrent run ever writes the same file → no
race class possible.

The `available_tiers` field on catalog entries is also retired for
the same reason (concurrent same-source derives raced on the
union). Clients compute tier availability from the tree.

### Freeze snapshot (optional)

`scripts/freeze_release.py <tag>` runs once per release after all
bakes and derives have settled. It walks the tree, builds the
manifest, and writes a single immutable `release-manifest.json`
under the revision. Single writer, single moment, race-free by
definition. Present for consumers that prefer one-file-grabs:

- HF's built-in dataset viewer (picks up the file and surfaces it
  prominently),
- `curl`-based tools that don't want to learn the tree API,
- human readers browsing the dataset on the HF UI.

Clients ignore it (they already have the manifest in memory by the
time they'd see it).

## Consequences

### Eliminated

- Manifest-merge race class (the Phase-3e failure mode).
- Per-bake `merge_remote_manifest` + `merge_remote_index` round-
  trips (one HEAD + one GET per source per run).
- Two sources of truth (file on disk vs reality-in-tree).

### Cost

- Clients make one extra HTTPS GET per session (tree listing,
  cacheable for hours). Negligible — clients already do dozens
  of requests per session.
- Filename conventions are the contract now. Breaking them
  (e.g. renaming `<src>-<t>.tar` to `<src>/<t>.tar`) is a
  schema change the client has to learn.

### What `release-manifest.json` means now

- If present, it's a convenience snapshot written by the freeze
  step, known to be correct for the revision it lives on.
- Clients **do not** use it. They build from the tree regardless,
  so a missing/stale manifest cannot make clients misread the
  dataset.

## Upgrade triggers

Revisit if:

- The tree listing API becomes rate-limited or slow enough that
  per-session discovery is painful.
- Filename conventions grow complex enough that tree-parsing
  becomes hard to keep in sync between baker output and client
  expectations. (Mitigation if so: write the manifest
  post-commit as an authoritative index, same pattern as the
  freeze step but per-bake.)
