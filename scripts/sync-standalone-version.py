#!/usr/bin/env python3
"""Sync standalone client's __version__ from clients/python/pyproject.toml.

The standalone script (``clients/python/mat_vis_client_standalone.py``) is a
single-file mirror of the packaged client. It has no ``importlib.metadata``
hook into a pyproject, so its version literal has to be rewritten whenever
the package version changes. This script is the canonical rewriter —
invoked by pre-commit on any edit to either file.

Fails (exit 1) if:
  - pyproject.toml has no project.version
  - the standalone file has no __version__ = "..." line to patch

Succeeds silently on no-op; prints what changed on update.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "clients" / "python" / "pyproject.toml"
STANDALONE = REPO / "clients" / "python" / "mat_vis_client_standalone.py"

VERSION_LINE_RE = re.compile(r'^__version__ = "[^"]*"$', re.MULTILINE)


def main() -> int:
    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    version = data.get("project", {}).get("version")
    if not version:
        print(f"error: no project.version in {PYPROJECT}", file=sys.stderr)
        return 1

    text = STANDALONE.read_text()
    new_line = f'__version__ = "{version}"'
    match = VERSION_LINE_RE.search(text)
    if not match:
        print(f"error: no __version__ line to patch in {STANDALONE}", file=sys.stderr)
        return 1

    if match.group(0) == new_line:
        return 0

    patched = text[: match.start()] + new_line + text[match.end() :]
    STANDALONE.write_text(patched)
    print(f"sync-standalone-version: {match.group(0)} → {new_line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
