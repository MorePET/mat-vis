# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- **Downstream `promote-release.yml` workflow** ([#463](https://github.com/vig-os/devcontainer/issues/463))
  - Template at `.github/workflows/promote-release.yml`: validate draft release and release PR, publish release, merge to `main`, best-effort git RC tag cleanup

### Changed

### Deprecated

### Removed

### Fixed

### Security

## mat-vis-client 0.6.0

Substrate migration. Data hosting moved from GitHub Releases + Parquet to
Hugging Face Datasets + tar archives ([ADR-0007](docs/decisions/0007-substrate-move-to-hf-datasets-and-tar-container.md)).
Structural fix for four recurring bug classes (#79, #82, #98, #99) via
atomic multi-file commits + immutable revisions.

Also ships the **v3 index-schema** rollout — curated `mat_vis` block +
verbatim `upstream` mirror per [ADR-0011](docs/decisions/0011-mat-vis-curated-plus-upstream-mirror.md)
(mat-vis#152). Clean break: all semantic fields move under `mat_vis.*`;
consumers must update accessor paths. A v3 client pointed at a v2 catalog
raises `MatVisError` early with an upgrade hint, not silent empty results.

### Added

- Reads from Hugging Face Datasets (`gerchowl/mat-vis`) by default.
  `MAT_VIS_HF_BASE` overrides the resolve URL for mirrors / tests.
- `release-manifest.json` schema v2 (`docs/specs/release-manifest-schema-v2.json`):
  per-source entries with `catalog` + `materials_count` + optional `tiers`
  map keyed by tier to `{tar, rowmap}`. Scalar sources omit `tiers`.
- First tagged release on the new substrate: `v2026.04.1` —
  4 sources (ambientcg 1965, polyhaven 753, gpuopen 2254, physicallybased 86).
- **`mat_vis` curated block** on every catalog entry (ADR-0011 Layer 1):
  `name`, `category`, `tags`, `description`, `physical.{dimensions_m, max_resolution_px}`,
  `pbr.{color_rgb, roughness, metalness, ior, specular_f0, transmission, complex_ior}`,
  `attribution.{authors, license_spdx, source_url}`, `dates.{published, updated}`,
  `upstream_id`. Stable key set — missing upstream values are `null`, never absent.
- **`upstream` verbatim mirror** per source (Layer 2): `{source, schema_version,
  fetched_at, raw}`. `raw` is the allowlisted upstream response. Explicitly
  **unstable** — not covered by semver. Stripped from every `index()` /
  `search()` return value; access via `client.upstream(source, material_id)`
  only.
- `MatVisClient.upstream(source, material_id, tier="1k")` typed accessor.
  Returns `{}` when the entry carries no upstream block (pre-rebake tags);
  raises `UnknownMaterialError` on unknown ids; accepts name-lookup per
  mat-vis#143.
- CI schema-diff gate: `scripts/check_upstream_schema_drift.py` runs on
  every bake, compares the candidate catalog's per-record key-set against
  the previous HF release. New keys warn; removed canonical keys or
  `mat_vis.*` presence regressions >5% fail the workflow.
- `index-schema.json` bumped to v3 + `mat-vis-block-v3.json` sub-schema.

### Changed

- `fetch_texture(source, mid, channel, tier)` range-reads the tar directly.
  Offsets point at the first byte past the 512-byte tar header; length is
  the PNG/KTX2 payload size.
- `sources()`, `tiers()`, `categories()`, `rowmap()`, `materials()`,
  `channels()`, `index()`, `rowmap_entry()` rewritten against the v2 shape.
  One rowmap per `(source, tier)`; no per-category partitioning (ADR-0007
  drops that dimension — category lives on each catalog entry instead).
- `tiers()` accepts an optional `source` to list just one source's tiers.
- `sources(tier=None)` returns all sources when `tier` is omitted; with
  `tier`, restricts to sources that published that tier.
- `categories()` derives from per-source catalogs, not filename parsing.

### Fixed

- `client.search(source="physicallybased")` now returns scalar-only entries
  regardless of the `tier` filter — previously returned `[]` because
  physicallybased advertises no textures
  ([#167](https://github.com/MorePET/mat-vis/issues/167)). The tier filter
  treats missing/empty `available_tiers` as tier-independent; textured
  sources are still gated to the requested tier. Behavioural change: any
  caller that relied on the silent-empty behavior will now see results.

### Removed

- `COMPATIBLE_SCHEMA_VERSIONS` drops `1`; only `2` is accepted. A cached
  v1 manifest raises with an upgrade hint. Consumers on the frozen
  `v2026.04.0` GitHub Release should stay on `mat-vis-client 0.5.x`.
- GitHub-specific URL machinery: `GITHUB_RELEASES`, `GITHUB_RAW`,
  `LATEST_MANIFEST_URL`, `_redirect_cache`, `_resolved_url`,
  `_cache_resolved`, the signed-URL stale-retry branch, and the
  `MAT_VIS_USE_HF` env flag (Phase 2 scaffolding).
- `rowmap_entry()` returns `{offset, length, tar_file}` — the `parquet_file`
  key is gone.

### Upgrade notes

```python
# 0.5.x → 0.6.0: passing tag is now effectively required (no "latest"
# alias on HF). Pin the data release explicitly.
client = MatVisClient(tag="v2026.04.1")
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

**Pin `tag="v2026.04.1"` or newer.** The earlier `v2026.04.0` catalogs
predate the v3 schema; running 0.6.0 against that tag raises with an
upgrade hint rather than silently returning empty results. Consumers that
cannot re-pin should stay on `mat-vis-client 0.5.x`.

The installable (`pip install mat-vis-client==0.6.0`) and the zero-deps
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
