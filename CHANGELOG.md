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
