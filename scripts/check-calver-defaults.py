#!/usr/bin/env python3
"""Block hardcoded calver defaults in source (issue #109).

The only legitimate place for a calver default is a workflow input on
`bake.yml` / `derive-ktx2.yml` — the string a human bumps per release.
Argparse defaults, module constants, and dataclass defaults that stamp
a calver into source will silently re-ship last month's tag the next
time somebody forgets the `--release-tag` flag.

Python files are parsed with ``ast`` so docstrings and comments are
not scanned (documentation is not SSoT). YAML / TOML / workflow files
fall back to line-based regex.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SCAN_DIRS = ("src", "scripts", ".dagger/src")

# Workflows where an input default is the one legitimate place for a
# calver string. Everything else under .github/workflows/ is scanned.
WORKFLOW_DEFAULT_ALLOWED = {
    Path(".github/workflows/bake.yml"),
    Path(".github/workflows/derive.yml"),
    # release-validate.yml (#263 phase C) — validator dispatch input
    # for the post-bake / drift-monitor gate. Same legitimate-default
    # exception as bake.yml: humans bump it per release.
    Path(".github/workflows/release-validate.yml"),
}

CALVER_RE = re.compile(r"v20\d{2}\.\d{2}\.\d+(-[A-Za-z0-9.]+)?")


def _is_calver(value: object) -> bool:
    return isinstance(value, str) and CALVER_RE.fullmatch(value) is not None


def _python_hits(path: Path) -> list[tuple[int, str]]:
    """Use AST so docstrings/comments are ignored — only real code counts."""
    hits: list[tuple[int, str]] = []
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return hits

    lines = path.read_text().splitlines()

    def _hit(lineno: int, note: str) -> None:
        line = lines[lineno - 1].rstrip() if 1 <= lineno <= len(lines) else ""
        hits.append((lineno, f"{note}: {line}"))

    for node in ast.walk(tree):
        # `release_tag = "v2026..."` or `release_tag: str = "v2026..."`
        if isinstance(node, ast.Assign):
            if not any(
                isinstance(t, ast.Name) and "release_tag" in t.id.lower() for t in node.targets
            ):
                continue
            if isinstance(node.value, ast.Constant) and _is_calver(node.value.value):
                _hit(node.lineno, "release_tag = <calver>")
        elif isinstance(node, ast.AnnAssign):
            if not (isinstance(node.target, ast.Name) and "release_tag" in node.target.id.lower()):
                continue
            if node.value and isinstance(node.value, ast.Constant) and _is_calver(node.value.value):
                _hit(node.lineno, "release_tag: ... = <calver>")
        # Function signature defaults for params named `release_tag`
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            defaults = list(args.defaults)
            positional = args.args[-len(defaults) :] if defaults else []
            for arg, default in zip(positional, defaults):
                if (
                    "release_tag" in arg.arg.lower()
                    and isinstance(default, ast.Constant)
                    and _is_calver(default.value)
                ):
                    _hit(default.lineno, f"default {arg.arg}=<calver>")
            kw_only = args.kwonlyargs
            kw_defaults = args.kw_defaults
            for arg, default in zip(kw_only, kw_defaults):
                if (
                    "release_tag" in arg.arg.lower()
                    and isinstance(default, ast.Constant)
                    and _is_calver(default.value)
                ):
                    _hit(default.lineno, f"kw default {arg.arg}=<calver>")
        # argparse add_argument('--release-tag', default='v...')
        elif isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id
            if func_name != "add_argument":
                continue
            positional_targets = " ".join(
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            )
            if "release" not in positional_targets.lower():
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "default"
                    and isinstance(kw.value, ast.Constant)
                    and _is_calver(kw.value.value)
                ):
                    _hit(kw.value.lineno, "add_argument(default=<calver>)")

    return hits


def _yaml_hits(path: Path) -> list[tuple[int, str]]:
    """Line-based for YAML: flag `default: v...` where a calver appears."""
    hits: list[tuple[int, str]] = []
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return hits
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "default" not in stripped:
            continue
        if CALVER_RE.search(stripped):
            hits.append((lineno, f"yaml default: {line.rstrip()}"))
    return hits


def main() -> int:
    failures: list[str] = []

    for sub in SCAN_DIRS:
        base = REPO / sub
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            for lineno, note in _python_hits(path):
                failures.append(f"{path.relative_to(REPO)}:{lineno}: {note}")

    wf_dir = REPO / ".github" / "workflows"
    if wf_dir.exists():
        for path in wf_dir.glob("*.yml"):
            rel = path.relative_to(REPO)
            if rel in WORKFLOW_DEFAULT_ALLOWED:
                continue
            for lineno, note in _yaml_hits(path):
                failures.append(f"{rel}:{lineno}: {note}")

    if failures:
        sys.stderr.write(
            "hardcoded calver default detected — use a required "
            "--release-tag arg instead. See issue #109 for context.\n\n"
        )
        for f in failures:
            sys.stderr.write(f"  {f}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
