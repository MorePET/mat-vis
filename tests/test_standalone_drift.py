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
import importlib.util
import inspect
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "clients" / "python" / "src" / "mat_vis_client" / "client.py"
STANDALONE = REPO / "clients" / "python" / "mat_vis_client_standalone.py"

# (class_name, method_name) pairs whose drift is a *pre-existing* divergence
# documented as non-goals in mat-vis#171. The per-operation ``tag=`` kwarg on
# ``fetch_texture`` / ``fetch_all_textures`` / ``mtlx`` / ``prefetch`` is part
# of the ``at()`` family (standalone doesn't forward per-op tags to a pinned
# client); the issue explicitly calls out ``at()`` and that family as
# non-goals. ``MtlxSource.*`` is entry-listed the same way (no actual drift at
# time of writing, but we allow the whole class to be pre-existing-divergent).
#
# Do NOT expand this list without citing a follow-up issue.
_SIGNATURE_DRIFT_ALLOWLIST: set[tuple[str, str]] = {
    ("MatVisClient", "at"),  # pre-existing divergence per mat-vis#171 non-goals
    ("MatVisClient", "fetch_texture"),  # per-op tag= — at()-family, non-goal
    ("MatVisClient", "fetch_all_textures"),  # per-op tag= — at()-family, non-goal
    ("MatVisClient", "mtlx"),  # per-op tag= — at()-family, non-goal
    ("MatVisClient", "prefetch"),  # per-op tag= — at()-family, non-goal
}
# Entire classes that are pre-existing-divergent (mat-vis#171 non-goals).
_SIGNATURE_DRIFT_ALLOWLIST_CLASSES: set[str] = {
    "MtlxSource",
}


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


def _load_module_from_file(path: Path, name: str):
    """Import a Python file as a module object by file location.

    Neither the packaged client (``clients/python/src/mat_vis_client/``)
    nor the single-file standalone are on ``sys.path`` for this repo's
    test root, so we side-load both by path. Keeps the drift test
    self-contained — no optional install step required.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_standalone_module():
    return _load_module_from_file(STANDALONE, "_mat_vis_standalone_for_drift")


def _load_packaged_module():
    # The packaged client imports its own ``mat_vis_client.schema`` submodule,
    # so the package root must be on ``sys.path`` even if the test environment
    # didn't install the client. Import via the real package name rather than
    # a side-loaded alias so relative imports resolve.
    src_root = PACKAGE.parent.parent  # clients/python/src
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))
    import importlib

    return importlib.import_module("mat_vis_client.client")


def _sig_params(fn) -> dict[str, inspect._ParameterKind]:
    """Return {name: kind} for every parameter of ``fn``.

    Intentionally ignores type annotations and defaults — we only care
    about the *shape* of the call surface (what kwargs the caller can
    legitimately pass). This is what catches mat-vis#171: signature drift
    that the AST name-only test misses.
    """
    sig = inspect.signature(fn)
    return {name: p.kind for name, p in sig.parameters.items()}


def test_standalone_method_signatures_match_package():
    """For every (class, public method) pair present in both, compare
    ``inspect.signature`` parameters (names + kinds).

    Skips:
      - private methods (``_``-prefixed), except ``__init__``
      - inherited methods (only compare methods actually defined on the class)
      - pairs listed in ``_SIGNATURE_DRIFT_ALLOWLIST`` / ``_CLASSES``
        (pre-existing divergences documented as mat-vis#171 non-goals)

    Any other drift fails loudly with a per-method diff.
    """
    pkg_mod = _load_packaged_module()
    std_mod = _load_standalone_module()

    mismatches: list[str] = []
    for cls_name in dir(pkg_mod):
        pkg_cls = getattr(pkg_mod, cls_name)
        if not inspect.isclass(pkg_cls):
            continue
        if pkg_cls.__module__ != pkg_mod.__name__:
            continue  # re-exports from stdlib etc.
        if cls_name in _SIGNATURE_DRIFT_ALLOWLIST_CLASSES:
            continue
        std_cls = getattr(std_mod, cls_name, None)
        if std_cls is None:
            continue  # covered by test_standalone_exposes_same_classes

        for method_name, pkg_m in inspect.getmembers(pkg_cls, predicate=inspect.isfunction):
            if method_name.startswith("_") and method_name != "__init__":
                continue
            # Only compare methods actually defined on this class (not inherited).
            if pkg_m.__qualname__.split(".")[0] != cls_name:
                continue
            if (cls_name, method_name) in _SIGNATURE_DRIFT_ALLOWLIST:
                continue
            std_m = getattr(std_cls, method_name, None)
            if std_m is None:
                continue  # covered by the class-surface test

            try:
                pkg_params = _sig_params(pkg_m)
                std_params = _sig_params(std_m)
            except (ValueError, TypeError):
                continue  # e.g. built-in wrapper with no introspectable sig

            if pkg_params != std_params:
                pkg_extra = set(pkg_params) - set(std_params)
                std_extra = set(std_params) - set(pkg_params)
                kind_diff = {
                    n: (pkg_params[n], std_params[n])
                    for n in set(pkg_params) & set(std_params)
                    if pkg_params[n] != std_params[n]
                }
                mismatches.append(
                    f"{cls_name}.{method_name}: "
                    f"missing_in_standalone={sorted(pkg_extra)} "
                    f"extra_in_standalone={sorted(std_extra)} "
                    f"kind_mismatch={kind_diff}"
                )

    assert not mismatches, (
        "standalone ↔ package method signature drift (see mat-vis#171):\n  "
        + "\n  ".join(mismatches)
    )
