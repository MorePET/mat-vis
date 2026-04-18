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
