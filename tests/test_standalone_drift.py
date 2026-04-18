"""Catch drift between the installable client and its single-file mirror.

The packaged client at ``clients/python/src/mat_vis_client/client.py`` and the
zero-install standalone at ``clients/python/mat_vis_client_standalone.py``
must expose the same surface — same classes, same methods, same public
free functions. Implementation bodies differ in a few well-known ways
(imports, version lookup, adapter helpers not bundled), so we compare the
*symbol inventory*, not the source text.

If you deliberately add/remove a symbol in one file, update the other
file in the same change. There is no free lunch — having two files
means CI has to enforce they agree.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "clients" / "python" / "src" / "mat_vis_client" / "client.py"
STANDALONE = REPO / "clients" / "python" / "mat_vis_client_standalone.py"


def _inventory(path: Path) -> dict[str, set[str]]:
    """Return {ClassName: {method_name, ...}} plus one entry under key ``"__module__"``
    for top-level functions and class names."""
    tree = ast.parse(path.read_text())
    inv: dict[str, set[str]] = {"__module__": set()}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            inv["__module__"].add(node.name)
            methods: set[str] = set()
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(item.name)
            inv[node.name] = methods
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inv["__module__"].add(node.name)
    return inv


def _private(name: str) -> bool:
    return name.startswith("_") and name not in ("__init__", "__enter__", "__exit__")


def test_standalone_exposes_same_classes():
    pkg = _inventory(PACKAGE)
    std = _inventory(STANDALONE)

    pkg_classes = {c for c in pkg if c != "__module__"}
    std_classes = {c for c in std if c != "__module__"}
    missing = pkg_classes - std_classes
    extra = std_classes - pkg_classes
    assert not missing, f"standalone is missing classes present in package: {sorted(missing)}"
    assert not extra, f"standalone defines classes not in package: {sorted(extra)}"


def test_standalone_classes_have_same_public_methods():
    pkg = _inventory(PACKAGE)
    std = _inventory(STANDALONE)
    mismatches: list[str] = []
    for cls in pkg:
        if cls == "__module__":
            continue
        pkg_pub = {m for m in pkg[cls] if not _private(m)}
        std_pub = {m for m in std.get(cls, set()) if not _private(m)}
        if pkg_pub != std_pub:
            missing = pkg_pub - std_pub
            extra = std_pub - pkg_pub
            mismatches.append(f"{cls}: missing={sorted(missing)} extra={sorted(extra)}")
    assert not mismatches, "standalone ↔ package class surface drift:\n  " + "\n  ".join(mismatches)


def test_standalone_exposes_same_public_module_functions():
    pkg = _inventory(PACKAGE)["__module__"]
    std = _inventory(STANDALONE)["__module__"]
    pkg_pub_fns = {n for n in pkg if not _private(n) and n[0].islower()}
    std_pub_fns = {n for n in std if not _private(n) and n[0].islower()}
    missing = pkg_pub_fns - std_pub_fns
    # Extras are tolerated — standalone may bundle helpers that the package
    # lazy-imports via mat_vis_client.adapters.
    assert not missing, (
        f"standalone missing public module functions from package: {sorted(missing)}"
    )
