---
type: issue
state: open
created: 2026-04-16T17:27:36Z
updated: 2026-04-16T17:27:36Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/4
comments: 0
labels: milestone
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:31.076Z
---

# [Issue 4]: [M0: CI green + baker container on GHCR](https://github.com/MorePET/mat-vis/issues/4)

## Goal

Scaffold is committed, CI passes, baker container image is
published to `ghcr.io/morepet/mat-vis-baker:latest`.

## Tasks

- [ ] `ruff check src/ tests/` passes
- [ ] `pytest tests/ -v` passes (smoke test)
- [ ] `podman build -t ghcr.io/morepet/mat-vis-baker:latest .`
      succeeds locally
- [ ] Push image to GHCR:
      `podman push ghcr.io/morepet/mat-vis-baker:latest`
- [ ] CI workflow (`.github/workflows/ci.yml`) passes on a PR
      to `dev`
- [ ] Add a workflow to auto-build and push the baker container
      on changes to `Containerfile` or `pyproject.toml`

## Test cases

```bash
# Local smoke test
pip install -e '.[baker,dev]'
ruff check src/ tests/
pytest tests/ -v

# Container build
podman build -t ghcr.io/morepet/mat-vis-baker:latest .
podman run --rm ghcr.io/morepet/mat-vis-baker:latest \
  python -c "import pyarrow; import materialx; print('ok')"
```

## Gate

All checks green. Container on GHCR. Devcontainer works.
