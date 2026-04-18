#!/usr/bin/env python3
"""Sync docs/specs/index-schema.json into the baker's packaged _spec dir.

Source of truth lives in docs/specs/. The BAKER needs the schema packaged
so importlib.resources works when the baker runs from an installed wheel
(Dagger containers) — the CLIENT does not need a copy because it
discovers taxonomy from the release manifest at runtime.

Runs as a pre-commit hook — exits non-zero if the copy drifted, so a CI
commit missing the sync fails fast.
"""

from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "docs" / "specs" / "index-schema.json"
TARGETS = [
    REPO / "src" / "mat_vis_baker" / "_spec" / "index-schema.json",
]


def main() -> int:
    if not SRC.exists():
        print(f"ERROR: missing source schema at {SRC}", file=sys.stderr)
        return 2

    drift = False
    for target in TARGETS:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or not filecmp.cmp(SRC, target, shallow=False):
            shutil.copy2(SRC, target)
            print(f"synced {target.relative_to(REPO)}")
            drift = True
        else:
            print(f"ok     {target.relative_to(REPO)}")

    if drift:
        print(
            "\nSpec copies were out of sync and have been updated. "
            "If running in pre-commit, stage the changes:\n  git add "
            + " ".join(str(t.relative_to(REPO)) for t in TARGETS),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
