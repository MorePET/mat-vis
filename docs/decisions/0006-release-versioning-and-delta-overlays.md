# 0006. Release versioning: client semver + data calver + delta overlays

- Status: Accepted (versioning); Proposed (delta overlays)
- Date: 2026-04-18
- Deciders: @gerchowl

## Context

mat-vis has two independent-ish artifacts that have been drifting in how
they are versioned:

1. **Data releases** on GitHub Releases (`v2026.04.0`) — snapshot of the
   upstream world at a point in time. Drivers of a new release: new
   materials added upstream (ambientcg, polyhaven, gpuopen), bug fixes
   to the bake pipeline that re-generate artifacts, new tiers (KTX2,
   mtlx), schema evolution.
2. **Client packages** (`mat-vis-client` on PyPI, JS/Rust/shell
   equivalents) — the read-path code that consumers install. Drivers:
   API additions (new adapters, new methods), bug fixes, schema support.

These have been bumped in lockstep during development (`2026.4.0` →
`2026.4.1` → `2026.4.2`) which misleadingly suggests they are coupled.
In reality:

- A data refresh (weekly watch picks up 3 new materials) **must not**
  force every consumer to `pip install -U mat-vis-client`.
- A client bug fix (a PNG-validation regression) **must not** wait for
  a data release to ship.
- A client upgrade must not break consumers pinned to an older data
  release, and vice versa.

Separately: data releases are currently **full** — every release
re-uploads every parquet, even unchanged ones. For ambientcg 2k that is
~60 GB per release. Weekly watch cycles would churn this regardless of
how few materials actually changed upstream.

## Decision (part 1 — versioning)

**Decouple client and data versions. Negotiate compatibility via a
shared schema number.**

| Artifact | Scheme | Example | Driven by |
|---|---|---|---|
| Data release | calver | `v2026.04.0` | upstream source updates |
| Client package | semver | `0.1.0` | API surface changes |
| Schema version | integer | `1` | wire format changes |

The **schema version** is the bridge. It is an integer field on the
release manifest (`schema_version: 1`). The client declares the set of
schema versions it understands. When the manifest loads, the client
validates:

```python
COMPATIBLE_SCHEMA_VERSIONS = frozenset([1])  # add 2 once the client can read v2
```

If the loaded manifest declares a schema the client does not know, it
refuses with a clear upgrade message rather than silently misreading.

Schema bumps are **rare** — reserved for breaking changes to the
manifest / rowmap layout. Adding tiers, categories, or sources is
additive and does not require a bump. Changing `offset`/`length`
semantics, renaming fields, or restructuring the manifest tree does.

### Consequences

- `pip install -U mat-vis-client` is a pure API-compat decision. It does
  not re-download data.
- Baking a new data release does not require a client release. Consumers
  pick it up via the `releases/latest` pointer automatically.
- Old clients remain pinned to their compatible schema. When we ship a
  breaking manifest change we bump `schema_version` *and* release a new
  client major that understands both versions for one grace cycle.
- No calver-style client version numbers. The current PyPI
  `mat-vis-client==2026.4.2` will be superseded by `0.1.0` on the next
  push.

## Decision (part 2 — data source selection)

**The client is data-source-agnostic.** Three injection points, already
in the codebase, promoted in docs:

```python
client = MatVisClient()                                    # latest release
client = MatVisClient(tag="v2026.04.0")                     # pinned tag
client = MatVisClient(manifest_url="https://mirror/...")    # custom / offline
client = MatVisClient(cache_dir=Path("/scratch/mat-vis"))   # custom cache
```

The `manifest_url` escape hatch supports mirrors, air-gapped environments,
test fixtures, and eventually Hugging Face Datasets (see ADR-0003 follow-on
for 4K+8K hosting).

## Decision (part 3 — delta overlays, proposed)

**Each release may declare a parent. The client transparently merges.**

For a weekly data refresh where 3 of 3000 materials changed upstream, we
do not want to re-bake and re-upload the other 2997. Introduce a **delta
overlay** model:

```json
{
  "schema_version": 1,
  "release_tag": "v2026.05.0",
  "base_release": "v2026.04.0",
  "tiers": {
    "1k": {
      "ambientcg": {
        "parquet_files": [
          {"name": "mat-vis-ambientcg-1k-stone.parquet", "from": "v2026.05.0"},
          {"name": "mat-vis-ambientcg-1k-wood.parquet",  "from": "v2026.04.0"}
        ]
      }
    }
  },
  "removed": [
    {"source": "ambientcg", "id": "SomeDeprecatedMaterial"}
  ]
}
```

Each entry carries a `from` tag that resolves the actual asset URL. The
client renders the full view by walking the base chain once and caching
the flattened manifest. Unchanged parquets keep their original CDN cache
and range-read URLs — no re-download, no re-fetch.

Compared to a "pure full" release chain, this:

- Cuts weekly release storage from ~60 GB to roughly (changed × per-material-size)
- Preserves browser / CDN caching for unchanged assets
- Leaves consumers' local caches valid across releases (same bytes at
  the same URL)
- Keeps each release **self-documenting** — the manifest fully declares
  every file the consumer should see, even if most live on the parent.

### Tombstones

The `removed` array allows upstream deletions to propagate. The client
strips matching material IDs from search / materials / fetch calls.

### Detection pipeline

`watch.yml` already polls upstream material lists weekly. Extend it to:

1. Diff upstream listings against the current release's index JSONs.
2. Classify each material as `new`, `changed` (hash mismatch), `unchanged`,
   or `removed`.
3. Bake only `new` + `changed`. Skip `unchanged`. Emit tombstones for
   `removed`.
4. Produce delta manifest pointing at parent for unchanged entries.

Material-level hashing is already implemented (`common.hash_textures`);
we only need to persist hashes per material in the index JSONs and
compare across bakes.

### Consequences

- Release storage grows sublinearly with upstream churn rather than
  linearly with release count.
- A single release can be resolved only with its full ancestor chain
  available on GitHub. We **never** remove older releases once a newer
  one points at them. `promote-data-release.yml` enforces this.
- Client logic must cache-flatten the manifest tree. Straightforward
  (~30 lines) but worth a separate ADR-amendment to specify the
  traversal rules.
- Tooling to "reconstruct a full release from a delta chain" becomes
  useful for mirroring and archival. Punted to a follow-on tool.

## Status

- **Part 1 (versioning)**: Accepted. `mat-vis-client` bumps to `0.1.0`
  on next push. Manifest emits `schema_version: 1`. Client validates.
- **Part 2 (data source selection)**: Accepted. API already supports
  it; docstring now calls it out.
- **Part 3 (delta overlays)**: Proposed. Not implemented yet —
  implementation blocked on first real upstream-driven refresh. Revisit
  when watch.yml detects non-trivial churn (more than a few materials
  per week).

## Amends

- ADR-0001 / ADR-0002: release-manifest schema extended with
  `schema_version` (additive, no breakage).
- Future: ADR-0007 spec for delta manifest resolution rules when part 3
  is implemented.
