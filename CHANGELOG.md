# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- **Downstream `promote-release.yml` workflow** ([#463](https://github.com/vig-os/devcontainer/issues/463))
  - Template at `.github/workflows/promote-release.yml`: validate draft release and release PR, publish release, merge to `main`, best-effort git RC tag cleanup
- **`mat-vis-client` 0.2.0**: dotted `MtlxSource` API ([#63](https://github.com/MorePET/mat-vis/issues/63))
  - `client.mtlx(src, id, tier)` returns a lazy `MtlxSource` with `.xml` (string) and `.export(path)` (writes PNGs + mtlx)
  - `MtlxSource.original` exposes upstream-author documents (gpuopen; `None` elsewhere) with the same accessors
  - Façade over the existing three code paths — no behavior changes, only surface

### Changed

### Deprecated

- **`mat-vis-client`**: `MatVisClient.to_mtlx`, `fetch_mtlx_original`, `materialize_mtlx` — use `client.mtlx(...)` / `.original` instead. Shims emit `DeprecationWarning` and delegate to the new API.

### Removed

- **`mat-vis-client`**: module-level `fetch()` helper — it swallowed errors and returned `{}`. Use `MatVisClient().fetch_all_textures(...)` which raises on failure.

### Fixed

### Security
