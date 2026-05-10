"""Standalone client: HF env-override coherence (refs #384).

Regression: the standalone's ``_build_manifest_from_tree`` used to
hardcode the dataset coordinate in the tree-listing URL, so
``MAT_VIS_HF_BASE`` redirected resolve URLs but left the manifest
discovery path pointing at prod ``gerchowl/mat-vis``. After #384 a
single ``MAT_VIS_HF_DATASET`` env var routes BOTH paths.

These tests intercept the constructed URL by re-importing the
standalone module under different env settings (the env is only read
at module load time) — no network calls.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch


_REPO_ROOT = Path(__file__).resolve().parents[3]
_STANDALONE = _REPO_ROOT / "clients" / "python" / "mat_vis_client_standalone.py"


def _load_standalone(unique_name: str):
    """Side-load a fresh copy of the standalone module.

    Each test gets its own module name so the env-var snapshot taken
    at import time is fresh — module caching would otherwise stick.
    """
    spec = importlib.util.spec_from_file_location(unique_name, _STANDALONE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _with_env(env: dict[str, str | None]):
    """Context-manager-ish helper: snapshot keys, set, yield, restore."""
    prev: dict[str, str | None] = {k: os.environ.get(k) for k in env}

    def _apply(values: dict[str, str | None]) -> None:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    _apply(env)
    return prev, _apply


def test_default_dataset_is_production():
    """No env vars set → both resolve and tree URLs point at prod."""
    prev, apply = _with_env({"MAT_VIS_HF_DATASET": None, "MAT_VIS_HF_BASE": None})
    try:
        std = _load_standalone("_standalone_hf_env_default")
        assert std.HF_DATASET == "gerchowl/mat-vis"
        assert "gerchowl/mat-vis/resolve" in std.HF_BASE
        assert std.HF_TREE_API == "https://huggingface.co/api/datasets/gerchowl/mat-vis/tree"
    finally:
        apply(prev)


def test_hf_dataset_env_routes_tree_url():
    """``MAT_VIS_HF_DATASET=gerchowl/mat-vis-tst`` → tree URL contains -tst."""
    prev, apply = _with_env({"MAT_VIS_HF_DATASET": "gerchowl/mat-vis-tst", "MAT_VIS_HF_BASE": None})
    try:
        std = _load_standalone("_standalone_hf_env_dataset")
        assert std.HF_DATASET == "gerchowl/mat-vis-tst"
        # Both paths route to the test dataset.
        assert "mat-vis-tst" in std.HF_BASE
        assert "mat-vis" in std.HF_BASE  # sanity (substring of mat-vis-tst too)
        assert std.HF_TREE_API == "https://huggingface.co/api/datasets/gerchowl/mat-vis-tst/tree"
        # The bug we're fixing: ensure the prod coord doesn't leak
        # into the tree URL.
        assert "datasets/gerchowl/mat-vis/tree" not in std.HF_TREE_API
    finally:
        apply(prev)


def test_hf_base_env_back_compat_still_works():
    """Legacy ``MAT_VIS_HF_BASE`` alone still overrides resolve URLs.

    Tree URL falls back to default (prod) when only HF_BASE is set —
    that's the documented legacy behavior. Callers wanting coherent
    routing should use ``MAT_VIS_HF_DATASET``.
    """
    prev, apply = _with_env(
        {
            "MAT_VIS_HF_DATASET": None,
            "MAT_VIS_HF_BASE": "https://example.test/datasets/foo/bar/resolve",
        }
    )
    try:
        std = _load_standalone("_standalone_hf_env_legacy_base")
        assert std.HF_BASE == "https://example.test/datasets/foo/bar/resolve"
        # Legacy behavior preserved: tree URL still derives from HF_DATASET.
        assert std.HF_DATASET == "gerchowl/mat-vis"
    finally:
        apply(prev)


def test_tree_url_used_in_manifest_build_honors_env():
    """End-to-end: ``_build_manifest_from_tree`` issues the env-routed URL."""
    prev, apply = _with_env({"MAT_VIS_HF_DATASET": "gerchowl/mat-vis-tst", "MAT_VIS_HF_BASE": None})
    try:
        std = _load_standalone("_standalone_hf_env_e2e")
        captured: dict[str, str] = {}

        def _fake_get_json(url: str):
            captured["url"] = url
            return []  # empty tree → empty sources dict

        with patch.object(std, "_get_json", _fake_get_json):
            client = std.MatVisClient(tag="v2026.04.2")
            client._build_manifest_from_tree()

        assert "url" in captured, "tree URL was never requested"
        assert "gerchowl/mat-vis-tst/tree" in captured["url"], (
            f"tree URL did not honor MAT_VIS_HF_DATASET: {captured['url']!r}"
        )
    finally:
        apply(prev)
