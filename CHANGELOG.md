# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

### Changed

### Deprecated

### Removed

### Fixed

### Security

## mat-vis-client 0.7.0

Ergonomic-tier rollout
([#374](https://github.com/MorePET/mat-vis/issues/374)). Downstream
consumers no longer have to learn about resolution-tier names before
they can render — pass no `tier=` and the client picks a working tier
automatically.

**Breaking**: default `tier` flipped from `"1k"` to `"auto"` on
`fetch_all_textures`, `fetch_texture`, `prefetch`, `materialize`,
`mtlx`, and `client.asset(...)`. Callers passing `tier="1k"`
explicitly are unaffected. Bumps the minor version per semver since
the default-value change is observable.

### Added

- Two new tier sentinels (`tier="auto"`, `tier="best"`) that collapse
  to a concrete tier per-material via the v3 catalog's
  `available_tiers`. `"auto"` is REPL-friendly — walks
  `(scalar-precheck) → 1k → 512 → 256 → 128` and returns `{}`
  textures for scalar-only materials so `to_threejs` / `to_gltf`
  still compose. `"best"` is the archival contract — walks
  `8k → 4k → … → 128` with NO scalar fallback, raises
  `MaterialNotStagedError` when nothing is staged.
- `VisAsset.resolved_tier` — the concrete tier the auto/best resolver
  picked. Useful for bake-pipeline manifests that need to record what
  was actually consumed (vs the literal `"auto"` the user passed).
- `MatVisClient._TIER_RANK` ClassVar with forward-compat tier-name
  filtering: unknown tier names from future substrates sort to
  nowhere and are skipped, never crash old clients.

### Changed

- Default `tier` is now `"auto"` on the consumer-fetch surface.
  `tier="1k"` callers keep their exact pre-0.7.0 behavior; only
  default-using callers see the new resolution path.
- The bake-side preview orchestrator (`bake/preview/run.py`) replaces
  its manual `(1k → 512 → 256 → 128)` ladder with a single
  `fetch_all_textures(..., tier="auto")` call — one source of truth
  for tier picking, in the client.

### Notes

- The `bash` and `Rust` thin clients don't backport `"auto"` /
  `"best"`. Help text now flags them as Python-client only; bash /
  Rust callers must pass an explicit tier name.

## mat-vis-client 0.6.4

Five client-side hotfixes off bernhard-42's
[build123d#1270](https://github.com/gumyr/build123d/pull/1270) review
thread. All on the existing substrate — no re-bake required.

### Fixed

- `MaterialNotStagedError` now preserves the user-given name when
  name resolution succeeds but the resolved entry isn't staged for
  the requested tier
  ([#280](https://github.com/MorePET/mat-vis/issues/280)). Adds
  optional `original_name` kwarg + dual-id message; the direct-UUID
  path emits the legacy single-id form unchanged so existing callers
  see no diff.
- `_resolve_material_id` falls back to the top-level `name` field so
  ambientcg / polyhaven flat-v2 entries become name-addressable,
  matching the gpuopen UX shipped in
  [#143](https://github.com/MorePET/mat-vis/issues/143)
  ([#284](https://github.com/MorePET/mat-vis/issues/284)).
  Bypasses the v3-schema migration tail
  ([#291](https://github.com/MorePET/mat-vis/issues/291)).
- `UnknownMaterialError` and `AmbiguousMaterialError` now list display
  names instead of UUIDs, prepend `difflib.get_close_matches` (n=5,
  cutoff=0.6) before the full list, and cap full output at 50 entries
  with a `(... N more)` tail
  ([#286](https://github.com/MorePET/mat-vis/issues/286)).
- `VisAsset.textures` short-circuits to `{}` for scalar-only index
  entries (`available_tiers=[]`) so `to_threejs` / `to_gltf` produce a
  valid scalars-only material on `physicallybased` instead of raising
  `MaterialNotStagedError` from the texture-fetch path
  ([#288](https://github.com/MorePET/mat-vis/issues/288)).
  Texture-bearing sources still route through the strict resolver.

### Added

- `fetch_texture` emits one
  `log.info("Downloading {source}/{id}/{channel} @ {tier} ...")` per
  cache miss at the network boundary
  ([#287](https://github.com/MorePET/mat-vis/issues/287)). Cache hits
  stay silent; consumers wire their own progress UIs onto the logger.
  No tqdm dependency.

### Changed

- All four client package versions aligned to **0.6.4**. Only the
  Python client's behavior changed; the JS / Rust / shell clients ship
  a version bump for cross-client coherence.

### Notes

- The hotfix bundle does **not** address
  [#285](https://github.com/MorePET/mat-vis/issues/285) (gpuopen
  baking errors — defaulted PBR scalars on layered MaterialX
  materials). That requires baker work and a substrate re-bake,
  tracked at [#290](https://github.com/MorePET/mat-vis/issues/290).
- bernhard-42's question on `Vis` import path
  ([#282](https://github.com/MorePET/mat-vis/issues/282)) is upstream
  of mat-vis; tracked at
  [MorePET/mat#187](https://github.com/MorePET/mat/issues/187).

## mat-vis-client 0.6.3

ETag-aware manifest cache across all four reference clients, plus a
formal immutable-tag policy declaration. The previous disk cache was
never invalidated — once a manifest landed under `$MAT_VIS_CACHE`, no
client could pick up a re-published manifest at the same tag short of
clearing the cache by hand. With
[#258](https://github.com/MorePET/mat-vis/issues/258) the cache holds a
sibling `.manifest.etag` file and every client lifecycle issues one
`If-None-Match` conditional GET; HF responds 304 when the manifest is
unchanged (the steady state on an immutable release tag — see Notes
below) and the cached body is served without re-downloading.

### Changed

- All four reference clients (Python, JS, Rust, shell) replace the
  never-invalidated manifest disk cache with an ETag-aware conditional
  GET ([#258](https://github.com/MorePET/mat-vis/issues/258)). One HTTP
  round-trip per client lifecycle in the steady state (304 on immutable
  tags); the body is refetched only when the server's ETag actually
  moves. Defensive cold-start when the origin omits an ETag — body is
  cached, but no `.manifest.etag` is written, so the next lifecycle
  refetches unconditionally rather than risking a stale-etag deadlock.
  JS keeps an in-memory cache only in the browser (no IndexedDB
  dependency) and a filesystem cache under Node, matching the existing
  zero-deps posture.
- All four client package versions aligned to **0.6.3**.

### Notes

- **Release tags are immutable.** Once a CalVer tag is published (e.g.
  `v2026.04.2`), the data at that revision will not change. New
  upstream snapshots, fixes, or rebakes ship as a new CalVer tag, never
  as an in-place rewrite of an existing one. This contract is what lets
  the new ETag cache trust 304 responses on pinned tags — the manifest
  bytes simply cannot drift under a pinned tag in the first place.

## mat-vis-client 0.6.2

Out-of-the-box-usable defaults across all four reference clients. With no
`tag` argument, every client now resolves to the just-shipped per-file
CalVer release instead of HF's empty `main` baseline branch — `pip install
mat-vis-client && python -c "from mat_vis_client import index; print(len(index()))"`
works without any env-var override.

### Changed

- All four reference clients (Python, JS, Rust, shell) default to
  `tag="v2026.04.2"` instead of `"main"` when no tag is provided
  ([#243](https://github.com/MorePET/mat-vis/pull/243), fixes
  [#242](https://github.com/MorePET/mat-vis/issues/242)). The HF dataset's
  `main` branch is an empty baseline — every release lives on a CalVer
  branch — so the prior default 404'd on every fetch. Hoisted to a
  module-level `DEFAULT_TAG` constant in each client for one-line bumps
  next cycle. The standalone Python client's `_revision()` manifest
  fallback gets the same treatment.
- All four client package versions aligned to **0.6.2**: Python 0.6.1 →
  0.6.2 (patch — behaviour change, not API break), JS 0.6.0 → 0.6.2, Rust
  0.6.0 → 0.6.2, shell UA literal `0.6.0` → `0.6.2`. Keeps the four
  lockstep even though it's a 2-patch jump for JS/Rust.

## mat-vis-client 0.6.1

Single bug fix to make the Python client survive at production scale.

### Fixed

- `MatVisClient` now reads `release-manifest.json` directly instead of
  reconstructing it from the HF tree-listing API
  ([#239](https://github.com/MorePET/mat-vis/pull/239), fixes
  [#238](https://github.com/MorePET/mat-vis/issues/238)). The HF tree API
  caps responses at 1000 entries per page; the per-file substrate's
  per-source listings exceed that at 1k+ tier — silently returning a
  truncated manifest. The catalog walker is replaced by a single GET on
  the manifest emitted by the baker (#208/#209). No API surface change.

## mat-vis-client 0.6.0

**Substrate rewrite** — data hosting moved from GitHub Releases + Parquet
to **per-file commits on Hugging Face Datasets**
([ADR-0012](docs/decisions/0012-per-file-substrate-drop-tar.md),
[#183](https://github.com/MorePET/mat-vis/pull/183)). Each `(source, tier,
material, channel)` is one file at
`gerchowl/mat-vis/resolve/<tag>/<source>/<tier>/<material>/<channel>.png`;
clients fetch with a plain `GET`. The atomic unit is one HF commit per
batch of materials (configurable, bytes-aware), not one tar per tier. Peak
local disk drops from O(full tar) — the staging bake hit ~70 GB on
ambientcg 2k ([#179](https://github.com/MorePET/mat-vis/issues/179)) — to
O(one batch).

**ADR-0007's tar substrate was deleted before the first per-file release
shipped** ([#203](https://github.com/MorePET/mat-vis/pull/203)); no public
tag ever served tar archives. Pre-shipped scaffolding (#108–#119, #146,
#164) exists in commit history but never reached an end user.

Also ships the **v3 index schema** rollout — curated `mat_vis` block +
verbatim `upstream` mirror per
[ADR-0011](docs/decisions/0011-mat-vis-curated-plus-upstream-mirror.md)
([#166](https://github.com/MorePET/mat-vis/pull/166),
[#170](https://github.com/MorePET/mat-vis/pull/170)). Clean break: all
semantic fields move under `mat_vis.*`; consumers must update accessor
paths.

### Added

- **Per-file HF substrate** (ADR-0012,
  [#183](https://github.com/MorePET/mat-vis/pull/183)). Textured sources
  route through `bake_one_per_file` by default
  ([#194](https://github.com/MorePET/mat-vis/pull/194)). Pre-flight HF
  tree scan skips already-committed materials → resumable mid-bake by
  construction. Each batch = one `HfApi.create_commit` with up to
  `batch_size × channels` `CommitOperationAdd` entries; subdirectory-per-
  material layout fits HF's 10k-files-per-directory cap with headroom.
- **Per-file derive pipeline**
  ([#206](https://github.com/MorePET/mat-vis/pull/206)) — resize + KTX2
  stages read individual PNGs and write the resulting tier's PNGs back as
  a per-file commit. `release-manifest.json` is updated in the same
  commit ([#208](https://github.com/MorePET/mat-vis/pull/208),
  [#209](https://github.com/MorePET/mat-vis/pull/209)) so JS/shell/Rust
  clients see a coherent state.
- **`release-manifest.json` schema v3** — `{schema_version: 3, sources:
  {<src>: {catalog, materials_count, tiers: {<tier>: {complete: bool}}}}}`.
  No `tar` / `rowmap` keys; per-tier completeness is a single boolean.
- **First per-file CalVer**: `v2026.04.2` — 4 sources × 4 tiers (1k, 512,
  256, 128). Supersedes `v2026.04.1`, which was an interim tag never
  fully derived under the per-file substrate.
- **Reference clients ported to per-file fetch** — Python
  ([#196](https://github.com/MorePET/mat-vis/pull/196)) and JS / Rust /
  shell ([#200](https://github.com/MorePET/mat-vis/pull/200)). The shell
  client is now a one-line `curl` for a single texture.
- **`mat_vis` curated block** on every catalog entry (ADR-0011 Layer 1,
  [#166](https://github.com/MorePET/mat-vis/pull/166)): `name`,
  `category`, `tags`, `description`, `physical.{dimensions_m,
  max_resolution_px}`, `pbr.{color_rgb, roughness, metalness, ior,
  specular_f0, transmission, complex_ior}`,
  `attribution.{authors, license_spdx, source_url}`,
  `dates.{published, updated}`, `upstream_id`. Stable key set — missing
  upstream values are `null`, never absent.
- **`upstream` verbatim mirror** per source (Layer 2,
  [#170](https://github.com/MorePET/mat-vis/pull/170)): `{source,
  schema_version, fetched_at, raw}`. `raw` is the allowlisted upstream
  response. Explicitly **unstable** — not covered by semver. Stripped
  from every `index()` / `search()` return value; access via
  `client.upstream(source, material_id)` only.
- **CI schema-diff gate**: `scripts/check_upstream_schema_drift.py` runs
  on every bake, compares the candidate catalog's per-record key-set
  against the previous HF release. New keys warn; removed canonical keys
  or `mat_vis.*` presence regressions >5% fail the workflow.
- **Streaming bake/derive progress**
  ([#220](https://github.com/MorePET/mat-vis/pull/220), closes
  [#217](https://github.com/MorePET/mat-vis/issues/217)) — live
  `is/should/ETA` lines flushed per batch instead of one terminal log line
  per tier; CI logs are now usable while a 6-hour bake is running.
- **Bytes-aware batching**
  ([#229](https://github.com/MorePET/mat-vis/pull/229), closes
  [#228](https://github.com/MorePET/mat-vis/issues/228)) — `batch_size` is
  superseded by `first-of-N-or-bytes`: a batch closes at either the
  configured material count or a configured uncompressed-byte ceiling,
  whichever hits first. Keeps each commit under HF's per-commit headroom
  on heavy tiers.
- **Bounded HF 429 backoff**
  ([#227](https://github.com/MorePET/mat-vis/pull/227), closes
  [#225](https://github.com/MorePET/mat-vis/issues/225)) — bake retries on
  HF's commit-rate ceiling with bounded exponential backoff instead of
  failing the whole tier.
- **`mat-vis-baker audit-orphans`** subcommand
  ([#202](https://github.com/MorePET/mat-vis/pull/202)) — finds LFS blobs
  uploaded by a crashed mid-batch run that have no corresponding tree
  entry. Matches on `file_oid` SHA-256
  ([#222](https://github.com/MorePET/mat-vis/pull/222)) so the listing
  and tree comparison agree.
- **Matrix `workflow_dispatch`** for `bake.yml` + `derive.yml`
  ([#235](https://github.com/MorePET/mat-vis/pull/235), closes
  [#233](https://github.com/MorePET/mat-vis/issues/233)) — `sources=all`
  fans out one job per source under a single dispatch.
- **Within-run serialisation** via `matrix.max-parallel: 1`
  ([#236](https://github.com/MorePET/mat-vis/pull/236)) — keeps the four
  source jobs from racing the HF commit-rate budget; complements the
  per-bake retry/backoff in #227.
- `index-schema.json` bumped to v3 + `mat-vis-block-v3.json` sub-schema.
- **`MatVisClient.upstream(source, material_id, tier="1k")`** typed
  accessor. Returns `{}` when the entry carries no upstream block;
  raises `UnknownMaterialError` on unknown ids; accepts name-lookup
  (mat-vis#143).
- **`TestConcurrentBakesShareTag`**
  ([#231](https://github.com/MorePET/mat-vis/pull/231)) exercises real CAS
  retry on a shared tag via a `multiprocessing.Manager`-backed barrier
  plus observable `cas_retries` / `lock_409_retries` counters; arbitrary
  tier slugs are now plumbed through `bake_one` so the test isn't lying
  about what it's measuring.

### Changed

- `fetch_texture(source, mid, channel, tier)` is a plain `GET` on the
  per-file resolve URL. No HTTP Range, no rowmap lookup, no tar header
  arithmetic.
- `sources()`, `tiers()`, `categories()`, `materials()`, `channels()`,
  `index()` rewritten against the per-file shape. No per-category
  partitioning (category lives on each catalog entry).
- `tiers()` accepts an optional `source` to list just one source's tiers.
- `sources(tier=None)` returns all sources when `tier` is omitted; with
  `tier`, restricts to sources that published that tier.
- `categories()` derives from per-source catalogs, not filename parsing.
- gpuopen `mat_vis.attribution.license_spdx` now reflects upstream
  per-record `license` via `normalize_spdx`
  (`"MIT Public Domain" → "MIT"`); unknown strings fall back to
  `"NOASSERTION"`
  ([#174](https://github.com/MorePET/mat-vis/pull/174)).

### Fixed

- `client.search(source="physicallybased")` now returns scalar-only
  entries regardless of the `tier` filter — previously returned `[]`
  because physicallybased advertises no textures
  ([#173](https://github.com/MorePET/mat-vis/pull/173), closes
  [#167](https://github.com/MorePET/mat-vis/issues/167)). The tier filter
  treats missing/empty `available_tiers` as tier-independent; textured
  sources are still gated to the requested tier. Behavioural change: any
  caller that relied on the silent-empty behaviour will now see results.

### Removed

- **Tar substrate** — `src/mat_vis_baker/tar_writer.py` (99 LOC),
  `src/mat_vis_baker/merge_shards.py` (289 LOC), the
  `mat-vis-baker merge-shards` CLI subcommand, and the rowmap JSON format
  - tests are all deleted
  ([#203](https://github.com/MorePET/mat-vis/pull/203)). ADR-0007 is
  superseded by ADR-0012 in part. The original sharded-tar plan
  (ADR-0010) is likewise retired for the disk-pressure reason it existed.
  HTTP Range support is gone from all four clients.
- `rowmap()` and `rowmap_entry()` removed from `MatVisClient`. There is
  no rowmap under per-file; the URL is constructed directly from
  `(source, tier, material, channel)`.
- `COMPATIBLE_SCHEMA_VERSIONS` drops `1`; manifest schemas `2` and `3`
  are accepted. A cached v1 manifest raises with an upgrade hint.
  Consumers on the frozen `v2026.04.0` GitHub Release should stay on
  `mat-vis-client 0.5.x`.
- GitHub-specific URL machinery: `GITHUB_RELEASES`, `GITHUB_RAW`,
  `LATEST_MANIFEST_URL`, `_redirect_cache`, `_resolved_url`,
  `_cache_resolved`, the signed-URL stale-retry branch, and the
  `MAT_VIS_USE_HF` env flag.

### Upgrade notes

```python
# 0.5.x → 0.6.x: pin a data release explicitly. 0.6.2+ defaults to
# v2026.04.2 if no tag is passed; earlier 0.6.x will 404 against HF's
# empty "main" baseline and need an explicit tag.
from mat_vis_client import MatVisClient
client = MatVisClient(tag="v2026.04.2")
```

**Index schema v2 → v3 migration** (breaking; ADR-0011 / mat-vis#152):

```python
# Before (v2):
entry["category"]           # "metal"
entry["color_hex"]          # "#C0C0C0"
entry["roughness"]          # 0.3
entry["source_url"]         # "https://..."

# After (v3):
entry["mat_vis"]["category"]                 # "metal"
entry["mat_vis"]["pbr"]["color_rgb"]         # [r, g, b] float, 0..1 (not hex)
entry["mat_vis"]["pbr"]["roughness"]         # 0.3
entry["mat_vis"]["attribution"]["source_url"] # "https://..."

# New: verbatim upstream (unstable shape, explicit opt-in)
raw = client.upstream("ambientcg", "Bricks097")
raw["displayCategory"]      # upstream-shaped; source-specific; not semver-stable
```

Adapters (`to_threejs`, `to_gltf`, `export_mtlx`) still accept a flat
`scalars` dict. If you were building that dict by hand from
`entry["color_hex"]` / `entry["roughness"]`, rebuild it from the new
`entry["mat_vis"]["pbr"]` shape (convert `color_rgb` → hex with
`"#{:02X}{:02X}{:02X}".format(*[int(c*255) for c in rgb])`).

**Pin `tag="v2026.04.2"` or newer.** Earlier `v2026.04.x` catalogs were
emitted under the abandoned tar substrate or never finished a full
derive pass under per-file. Consumers that cannot re-pin should stay on
`mat-vis-client 0.5.x`.

The installable (`pip install mat-vis-client`) and the zero-deps
standalone (`clients/python/mat_vis_client_standalone.py`) expose the
same surface.

## mat-vis-client 0.4.1

Hotfix from the post-0.4.0 code review. No API changes.

### Fixed

- **Unified User-Agent** ([#70](https://github.com/MorePET/mat-vis/issues/70)) — the zero-install standalone mirror used to identify itself as `mat-vis-client-standalone/<v>`; the installable package used `mat-vis-client/<v>`. Split UA populations fragmented server-side observability and rate-limit buckets for what is operationally one client. Both now emit `mat-vis-client/<v> (Python)`.
- **Pre-publish pytest gate in pypi.yml** ([#73](https://github.com/MorePET/mat-vis/issues/73)) — `.github/workflows/pypi.yml` now runs the full test suite before `publish`. Previously `ci.yml` ran on main/dev but not on `client/v*` tags, so the drift tests added in 0.3.1 didn't gate releases. Explicit "if tests fail, don't publish" ordering.
- **Tag vs pyproject.version assertion in pypi.yml** ([#74](https://github.com/MorePET/mat-vis/issues/74)) — `publish` now fails fast if the pushed tag doesn't match the version in `clients/python/pyproject.toml`. Prevents the "wrong wheel on PyPI" failure mode where the two drift and the wheel is irreversibly published under the pyproject version.

### Tests (+1)

- `tests/test_version_sync.py::test_standalone_user_agent_matches_packaged` — pins the unified UA so neither the AST drift test nor the runtime version check can miss a string-literal divergence.

Total suite: **173 passed**, 0 skipped.

## mat-vis-client 0.4.0

Polish release following the post-0.3.1 review. DX-focused; no breaking API changes.

### Added

- **Friendly not-found errors** (item G) — missing tier / source / material / channel
  now raise `MatVisError` with an `"Available: [...]"` suggestion list instead of a
  bare `KeyError`. Catches the common typo case and points at the real options.
- **502 + 504 to the retry list** — `_get` was already retrying 429/503 and
  rate-limited 403; GitHub Releases' edge regularly returns 504 under load and
  pre-0.4.0 those propagated as hard errors. Now retried with the same backoff.
- **Per-client feature matrix** in `README.md` (item H) — table showing what
  Python/JS/Rust/shell/SQL clients actually support (search, prefetch, cache,
  adapters, rate-limit retry, MaterialX).
- **`clients/python/README.md`** — dedicated PyPI-facing README; the project
  page now renders proper docs instead of the one-line inline description.

### Changed

- **PyPI classifier** `Development Status :: 3 - Alpha` → `4 - Beta` (item M).
  API stability: `MatVisClient`, `MatVisError`, `RateLimitError`, `MtlxSource`
  and the module-level `search`/`prefetch` helpers are considered stable through
  the 0.x series.
- **`readme =` in pyproject.toml** — inline string swapped for
  `{file = "README.md", content-type = "text/markdown"}` (item N). This is what
  drives the rendered long description on PyPI.
- Python version classifiers expanded (3.10 / 3.11 / 3.12 / 3.13) to match the
  supported-versions matrix.

### Fixed

- **Live test regression** — `TestLiveFetchTexture::test_fetch_nonexistent_material_raises`
  updated to expect the new `MatVisError` (was asserting the now-replaced bare `KeyError`).

### Tests (+13)

- 5 `TestFriendlyNotFoundErrors` cases (unknown tier / source / material / channel
  across `fetch_texture` + `rowmap_entry`)
- 7 `TestRateLimitRetry` cases covering every retry branch in `_get`:
  429 / 502 / 503 / 504, 403+`X-RateLimit-Remaining:0`, 403+body, `URLError`,
  non-rate-limit 403 passes through
- 1 `TestMtlxOriginalFetchError` case pinning the silent-empty-cache-on-fetch-error
  behavior of `_fetch_mtlx_original_map` (item J)

Total suite: **173 passed**, 0 skipped.

## mat-vis-client 0.3.1

Hotfix release following the 0.3.0 post-release security + SSoT review.

### Added

- **`mat_vis_client.__version__`** — top-level version export, derived from installed package metadata (`importlib.metadata.version`). Same string drives the HTTP User-Agent and any user-side version comparisons.
- **Range-read safety cap** (`DEFAULT_MAX_FETCH_BYTES`, 500 MB default) — `fetch_texture` now rejects rowmap entries claiming lengths above the cap or with non-positive lengths, defending against malicious/corrupt rowmaps driving the client OOM. Override with `MAT_VIS_MAX_FETCH_SIZE`.
- **`tests/test_version_sync.py`** — CI fails if the standalone `__version__` literal drifts from `clients/python/pyproject.toml`.
- **`tests/test_standalone_drift.py`** — AST-based inventory comparison between packaged `client.py` and `mat_vis_client_standalone.py`; missing classes or public methods fail CI.
- **`scripts/sync-standalone-version.py`** — pre-commit hook rewrites the standalone's version literal from `pyproject.toml`.

### Changed

- **`USER_AGENT` is now derived from pyproject.toml** via `importlib.metadata` — no more manually-bumped string literal. Previously shipped wheels stamped `mat-vis-client/0.2 (Python)` in every HTTP request regardless of the actual installed version.
- **`BAKER_VERSION`** now reads from installed package metadata (baker pyproject bumped from `0.0.0` to `0.1.0`). Previously the baker stamped `0.1.0` into parquet metadata while claiming `0.0.0` in pip metadata.

### Security

- **jq-injection fix in `verify_upload_size`** — asset name used to be f-stringed into a `gh release view --jq` filter. Now fetches the asset list as JSON and filters in Python. Asset names are currently repo-controlled but filenames have crossed shell contexts before (see #61), so we treat them as untrusted on principle.
- **Resume marker path containment** — `progress_path()` resolves both sides and rejects paths that would escape the output directory. Defense in depth; filename is a constant today.

### Fixed (other clients)

- **Rust client**: `User-Agent` now derived from `Cargo.toml` via `concat!(..., env!("CARGO_PKG_VERSION"), ...)` — was a hardcoded `0.1` string regardless of crate version.
- **README search example** — `client.search("marble")` replaced with `client.search(category="stone", roughness_range=...)` (kwargs required; "marble" is a keyword, not a canonical category).

## mat-vis-client 0.3.0

### Added

- **Dotted `MtlxSource` façade** ([#63](https://github.com/MorePET/mat-vis/issues/63))
  - `client.mtlx(source, id, tier)` returns a lazy `MtlxSource` with `.xml` (string), `.export(path)` (writes PNGs + mtlx), and `.original` (upstream-author documents; gpuopen only, `None` elsewhere)
  - Façade over the existing three code paths — no behavior changes, only surface
- **`client.categories()`** — dynamic discovery of material categories from the release manifest (no hardcoded list)
- **`RateLimitError`** typed exception; auto-retry on 429/503/rate-limited 403 with `Retry-After` / `X-RateLimit-Reset` handling
- **Redirect URL cache** — first range read captures the signed `objects.githubusercontent.com` URL; subsequent reads skip the rate-limited `github.com` redirect (measured 22× speedup on cache hit)
- **Update check via `logging.getLogger("mat-vis-client")`** — suppressed when `sys.stderr` is not a TTY unless `MAT_VIS_UPDATE_CHECK=1` forces it on
- **Retry/backoff env vars**: `MAT_VIS_MAX_RETRIES`, `MAT_VIS_BACKOFF_BASE`, `MAT_VIS_RETRY_MAX_WAIT`

### Changed

- **Manifest requires `schema_version`** — legacy `version` fallback removed on client side. Users with stale cached manifests should run `mat-vis-client cache clear`.

### Deprecated

- **`MatVisClient.to_mtlx`, `fetch_mtlx_original`, `materialize_mtlx`** — use `client.mtlx(...)` / `.original` instead. Shims emit `DeprecationWarning` and delegate to the new API.

### Removed

- **Module-level `fetch()` convenience function** — swallowed exceptions silently. Use `MatVisClient().fetch_all_textures(...)` directly for explicit error handling.
- **Static `CATEGORIES` frozenset** — now lazy, populated from manifest discovery.

### Fixed

- **Rowmap scanner bug**: scanner used `data_page_offset` when pyarrow with `use_dictionary=False` actually stored binary data at `dictionary_page_offset`. New rowmaps are emitted inline by the writer via a sidecar dict — no more magic-byte heuristics. Existing release rowmaps were regenerated end-of-last-session.

### Security

- **Zip-slip (CWE-22) and decompression-bomb (CWE-409) defenses** added to baker fetchers (ambientcg, gpuopen). Doesn't affect client consumers; listed for completeness.

### Infrastructure (repo-internal, not in wheel)

- **`validate-release` CI gate now required** (no `continue-on-error`) — enforces tier/channel parity per release
- **Atomic chunk upload**: `.part` files, `os.replace`, `gh release upload` retry + size verification, `.bake-progress.json` resume marker
- **Shell-safety lint**: no f-string interpolation in `.dagger/ sh -c` blocks
