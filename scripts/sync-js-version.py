#!/usr/bin/env python3
"""Sync the JS client's ``VERSION`` literal from ``clients/js/package.json``.

The JS client ships as a single zero-deps ``.mjs`` file. It can't import
package.json at runtime without bundler-specific JSON-attribute syntax,
so the version is mirrored into a top-level ``VERSION`` constant and
kept in sync by this script (invoked via pre-commit).

Mirrors the pattern of ``scripts/sync-standalone-version.py`` for the
Python standalone client.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE_JSON = REPO / "clients" / "js" / "package.json"
CLIENT_MJS = REPO / "clients" / "js" / "mat-vis-client.mjs"

VERSION_LINE_RE = re.compile(r"^export const VERSION = '[^']*';$", re.MULTILINE)


def main() -> int:
    data = json.loads(PACKAGE_JSON.read_text())
    version = data.get("version")
    if not version:
        print(f"error: no version in {PACKAGE_JSON}", file=sys.stderr)
        return 1

    text = CLIENT_MJS.read_text()
    new_line = f"export const VERSION = '{version}';"
    match = VERSION_LINE_RE.search(text)
    if not match:
        print(f"error: no VERSION line to patch in {CLIENT_MJS}", file=sys.stderr)
        return 1

    if match.group(0) == new_line:
        return 0

    patched = text[: match.start()] + new_line + text[match.end() :]
    CLIENT_MJS.write_text(patched)
    print(f"sync-js-version: {match.group(0)} → {new_line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
