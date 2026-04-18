#!/usr/bin/env python3
"""Lint guard for #61: no f-string shell interpolation in Dagger modules.

Scans ``.dagger/src/**/*.py`` for ``with_exec(["sh", "-c", f"..."])``
patterns and fails with a non-zero exit code on any match. User-supplied
values must be passed via ``with_env_variable`` + ``$VAR`` references in
the shell, or (preferred) via argv-only ``with_exec`` calls.

Usage:
    python3 scripts/check-dagger-shell-safety.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAGGER_DIR = ROOT / ".dagger" / "src"

# Any "sh" followed by "-c" followed by an f-prefix string is a violation.
# re.DOTALL so the pattern can span lines (pyproject-formatted argument lists).
PATTERN = re.compile(
    r'"sh"\s*,\s*"-c"\s*,\s*(f"""|f\'\'\'|f"|f\')',
    re.DOTALL,
)


def main() -> int:
    if not DAGGER_DIR.exists():
        print(f"skip: {DAGGER_DIR} not present", file=sys.stderr)
        return 0

    violations: list[tuple[Path, int]] = []
    for path in DAGGER_DIR.rglob("*.py"):
        text = path.read_text()
        for match in PATTERN.finditer(text):
            line = text[: match.start()].count("\n") + 1
            violations.append((path, line))

    if violations:
        print(
            "FAIL: f-string interpolation into 'sh -c' detected — "
            "pass user values via with_env_variable or argv-only with_exec:",
            file=sys.stderr,
        )
        for p, ln in violations:
            print(f"  {p.relative_to(ROOT)}:{ln}", file=sys.stderr)
        return 1

    print("OK: no f-string shell interpolation in Dagger modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
