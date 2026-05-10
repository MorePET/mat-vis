"""Unified search API (#85 item 5).

Goal: one canonical signature. Module-level ``search()`` forwards to
``MatVisClient.search()``; the scalar-shorthand form is handled by the
method itself, not by a divergent module-level function.

This kills the two-surface problem where the same operation had two
different parameter conventions depending on where you called it.
"""

from __future__ import annotations

import importlib.util as _importlib_util
from pathlib import Path as _Path
from unittest.mock import patch

import pytest

import mat_vis_client


def _entry(
    mid: str,
    cat: str,
    r: float,
    m: float,
    tiers: list[str],
    *,
    source: str = "ambientcg",
    omit_tiers: bool = False,
) -> dict:
    """Build a v3-shaped index entry (ADR-0011 / mat-vis#152).

    When ``omit_tiers`` is True, ``available_tiers`` is left out of the
    dict entirely (simulates legacy/scalar-only shape). Otherwise the
    key is always present, possibly with an empty list.
    """
    entry = {
        "material_id": mid,
        "source": source,
        "mat_vis": {
            "name": mid,
            "category": cat,
            "tags": [],
            "description": None,
            "physical": {"dimensions_m": None, "max_resolution_px": None},
            "pbr": {
                "color_rgb": None,
                "roughness": r,
                "metalness": m,
                "ior": None,
                "specular_f0": None,
                "transmission": None,
                "complex_ior": None,
            },
            "attribution": {
                "authors": [],
                "license_spdx": "CC0-1.0",
                "source_url": "",
            },
            "dates": {"published": None, "updated": None},
            "upstream_id": mid,
        },
        "available_tiers": tiers,
    }
    if omit_tiers:
        entry.pop("available_tiers", None)
    return entry


MOCK_INDEX = [
    _entry("Metal032", "metal", 0.3, 1.0, ["1k", "2k"]),
    _entry("Metal050A", "metal", 0.5, 1.0, ["1k"]),
    _entry("Wood002", "wood", 0.7, 0.0, ["1k"]),
]


def _fresh_singleton():
    """Reset the module-level singleton so each test starts clean."""
    mat_vis_client._client = None


# ── Method search accepts scalar shorthand ─────────────────────


def test_method_search_accepts_scalar_roughness():
    """client.search(roughness=0.3) should widen into a range automatically."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    with patch.object(c, "sources", return_value=["ambientcg"]):
        with patch.object(c, "index", return_value=MOCK_INDEX):
            with patch.object(c, "categories", return_value=frozenset(["metal", "wood"])):
                results = c.search(category="metal", roughness=0.3)
    # Only materials within roughness ± 0.2 of 0.3 → [0.1, 0.5]
    ids = [r["material_id"] for r in results]
    assert "Metal032" in ids  # 0.3 → center
    assert "Metal050A" in ids  # 0.5 → boundary, inclusive


def test_method_search_accepts_scalar_metalness():
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    with patch.object(c, "sources", return_value=["ambientcg"]):
        with patch.object(c, "index", return_value=MOCK_INDEX):
            with patch.object(c, "categories", return_value=frozenset(["metal", "wood"])):
                results = c.search(metalness=1.0)
    ids = {r["material_id"] for r in results}
    assert "Metal032" in ids
    assert "Wood002" not in ids


def test_method_search_distance_option_sorts_by_distance():
    """search(roughness=0.3, distance=True) sorts by |r - 0.3| (#359 rename)."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    with patch.object(c, "sources", return_value=["ambientcg"]):
        with patch.object(c, "index", return_value=MOCK_INDEX):
            with patch.object(c, "categories", return_value=frozenset(["metal", "wood"])):
                results = c.search(category="metal", roughness=0.3, distance=True)
    # Metal032 (0.3, diff 0) comes before Metal050A (0.5, diff 0.2).
    # Results are now list[Match] (#359), but the dict-subclass means
    # ``r["material_id"]`` (the fixture key) still works.
    ids = [r["material_id"] for r in results]
    assert ids[0] == "Metal032"
    for r in results:
        assert "distance" in r


def test_method_search_rejects_both_scalar_and_range():
    """Passing both roughness and roughness_range is ambiguous; raise."""
    from mat_vis_client import MatVisClient, MatVisError

    c = MatVisClient()
    with pytest.raises(MatVisError, match="roughness"):
        c.search(roughness=0.3, roughness_range=(0.1, 0.5))


# ── Module-level forwards to the same method ───────────────────


def test_module_search_forwards_to_client_method():
    """search() (module) returns the same results as client.search() for
    equivalent input — no divergent implementation."""
    _fresh_singleton()
    from mat_vis_client import search as module_search
    from mat_vis_client import get_client

    client = get_client()
    with patch.object(client, "sources", return_value=["ambientcg"]):
        with patch.object(client, "index", return_value=MOCK_INDEX):
            with patch.object(client, "categories", return_value=frozenset(["metal", "wood"])):
                mod_results = module_search(category="metal", roughness=0.3)
                _fresh_singleton()

    client2 = get_client()
    with patch.object(client2, "sources", return_value=["ambientcg"]):
        with patch.object(client2, "index", return_value=MOCK_INDEX):
            with patch.object(client2, "categories", return_value=frozenset(["metal", "wood"])):
                method_results = client2.search(category="metal", roughness=0.3, distance=True)

    # Module-level is equivalent to method-level with distance=True (#359 rename).
    assert [r["material_id"] for r in mod_results] == [r["material_id"] for r in method_results]


# ── Tier filter: scalar-only entries are tier-independent (#167) ───


@pytest.mark.parametrize("tier", ["1k", "4k", "nonsense"])
def test_search_scalar_only_no_tier_key_matches_any_tier(tier):
    """Entry without an ``available_tiers`` key is treated as
    tier-independent — it passes any tier filter (#167)."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    index = [_entry("Iron", "metal", 0.4, 1.0, [], source="physicallybased", omit_tiers=True)]
    # Explicit source → search doesn't call self.sources(tier).
    with patch.object(c, "index", return_value=index):
        with patch.object(c, "categories", return_value=frozenset(["metal"])):
            results = c.search(source="physicallybased", tier=tier)
    assert [r["material_id"] for r in results] == ["Iron"]


@pytest.mark.parametrize("tier", ["1k", "4k", "nonsense"])
def test_search_scalar_only_empty_tiers_matches_any_tier(tier):
    """Entry with ``available_tiers=[]`` (the physicallybased shape) is
    treated as tier-independent — it passes any tier filter (#167)."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    index = [_entry("Copper", "metal", 0.35, 1.0, [], source="physicallybased")]
    with patch.object(c, "index", return_value=index):
        with patch.object(c, "categories", return_value=frozenset(["metal"])):
            results = c.search(source="physicallybased", tier=tier)
    assert [r["material_id"] for r in results] == ["Copper"]


def test_search_textured_entry_excluded_on_tier_miss():
    """Regression guard: a textured entry with a non-empty
    ``available_tiers`` list is still excluded when its tiers don't
    cover the requested tier (ambientcg/polyhaven/gpuopen)."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    index = [_entry("Rock001", "stone", 0.9, 0.0, ["1k", "2k"])]
    with patch.object(c, "index", return_value=index):
        with patch.object(c, "categories", return_value=frozenset(["stone"])):
            results = c.search(source="ambientcg", tier="4k")
    assert results == []


def test_search_mixed_cross_source_returns_scalar_and_textured():
    """With ``tier="1k"`` across sources, both scalar-only
    (physicallybased) and textured entries covering 1k are returned."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    acg = _entry("Metal032", "metal", 0.3, 1.0, ["1k", "2k"], source="ambientcg")
    pb = _entry("Iron", "metal", 0.4, 1.0, [], source="physicallybased")
    polyh_miss = _entry("gold_foil", "metal", 0.2, 1.0, ["2k", "4k"], source="polyhaven")

    def _fake_index(src: str):
        return {
            "ambientcg": [acg],
            "physicallybased": [pb],
            "polyhaven": [polyh_miss],
        }[src]

    with patch.object(c, "sources", return_value=["ambientcg", "physicallybased", "polyhaven"]):
        with patch.object(c, "index", side_effect=_fake_index):
            with patch.object(c, "categories", return_value=frozenset(["metal"])):
                results = c.search(category="metal", tier="1k")
    ids = {r["material_id"] for r in results}
    assert ids == {"Metal032", "Iron"}  # polyhaven's 2k/4k-only entry excluded


def test_search_issue_167_repro_physicallybased_metals():
    """End-to-end repro of mat-vis#167:
    ``search(source="physicallybased", metalness=1.0, tier="1k")`` used
    to return ``[]`` because physicallybased advertises no textures.
    Now returns its metal entries."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    index = [
        _entry("Iron", "metal", 0.4, 1.0, [], source="physicallybased"),
        _entry("Gold", "metal", 0.2, 1.0, [], source="physicallybased"),
        _entry("Oak", "wood", 0.7, 0.0, [], source="physicallybased"),
    ]
    with patch.object(c, "index", return_value=index):
        with patch.object(c, "categories", return_value=frozenset(["metal", "wood"])):
            results = c.search(source="physicallybased", metalness=1.0, tier="1k")
    ids = {r["material_id"] for r in results}
    assert ids == {"Iron", "Gold"}


# ── Standalone search parity (mat-vis#171) ────────────────────────
#
# The single-file standalone at ``clients/python/mat_vis_client_standalone.py``
# used to carry a minimal ``search()`` signature (no scalar shorthand, no
# score/limit/tag). These tests exercise the ported kwargs so behavioral
# regressions in the standalone get caught alongside the signature-drift
# test in ``tests/test_standalone_drift.py``.


def _load_standalone():
    """Side-load the standalone module by file path (not on sys.path)."""
    import sys as _sys

    repo_root = _Path(__file__).resolve().parents[3]
    path = repo_root / "clients" / "python" / "mat_vis_client_standalone.py"
    name = "_mat_vis_standalone_for_search_tests"
    spec = _importlib_util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = _importlib_util.module_from_spec(spec)
    # Register before exec so @dataclass-decorated classes can resolve
    # ``cls.__module__`` during type-annotation introspection.
    _sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_standalone_search_scalar_roughness_widens():
    """Standalone: ``search(roughness=0.3)`` widens into ± _SCALAR_WIDEN."""
    std = _load_standalone()
    c = std.MatVisClient()
    with patch.object(c, "sources", return_value=["ambientcg"]):
        with patch.object(c, "index", return_value=MOCK_INDEX):
            with patch.object(c, "categories", return_value=frozenset(["metal", "wood"])):
                results = c.search(category="metal", roughness=0.3)
    ids = [r["material_id"] for r in results]
    # Metal032 (0.3) and Metal050A (0.5, boundary inclusive) are within 0.3 ± 0.2.
    assert "Metal032" in ids
    assert "Metal050A" in ids


def test_standalone_search_distance_sorts_ascending_by_distance():
    """Standalone: ``search(roughness=0.3, distance=True)`` attaches + sorts (#359)."""
    std = _load_standalone()
    c = std.MatVisClient()
    with patch.object(c, "sources", return_value=["ambientcg"]):
        with patch.object(c, "index", return_value=MOCK_INDEX):
            with patch.object(c, "categories", return_value=frozenset(["metal", "wood"])):
                results = c.search(category="metal", roughness=0.3, distance=True)
    ids = [r["material_id"] for r in results]
    assert ids[0] == "Metal032"  # diff 0 comes first
    for r in results:
        assert "distance" in r


def test_standalone_search_limit_truncates():
    """Standalone: ``search(limit=N)`` truncates result list."""
    std = _load_standalone()
    c = std.MatVisClient()
    with patch.object(c, "sources", return_value=["ambientcg"]):
        with patch.object(c, "index", return_value=MOCK_INDEX):
            with patch.object(c, "categories", return_value=frozenset(["metal", "wood"])):
                results = c.search(limit=1)
    assert len(results) == 1


def test_standalone_search_release_kwarg_accepted():
    """Standalone: ``search(release=...)`` dispatches to a pinned client (#359 rename)."""
    std = _load_standalone()
    c = std.MatVisClient()

    class _FakePinned:
        calls: list[dict] = []

        def search(self, category=None, **kwargs):
            _FakePinned.calls.append({"category": category, **kwargs})
            return []

    with patch.object(c, "at", return_value=_FakePinned()):
        out = c.search(category="metal", release="v2026.04.1")
    assert out == []
    assert _FakePinned.calls and _FakePinned.calls[0]["category"] == "metal"


def test_standalone_search_rejects_both_scalar_and_range():
    """Standalone mirrors the packaged guard: can't pass both shorthand + range."""
    std = _load_standalone()
    c = std.MatVisClient()
    with pytest.raises(std.MatVisError, match="roughness"):
        c.search(roughness=0.3, roughness_range=(0.1, 0.5))


def test_standalone_module_level_search_forwards_with_distance_and_limit():
    """Standalone's module-level ``search()`` applies distance=True + default limit=20 (#359)."""
    std = _load_standalone()
    # Reset the standalone's own singleton so the patch targets a fresh client.
    std._client = None
    client = std.get_client()
    with patch.object(client, "sources", return_value=["ambientcg"]):
        with patch.object(client, "index", return_value=MOCK_INDEX):
            with patch.object(client, "categories", return_value=frozenset(["metal", "wood"])):
                mod_results = std.search(category="metal", roughness=0.3)
    # distance=True is applied, so results carry a 'distance' field and are sorted.
    assert mod_results
    assert "distance" in mod_results[0]
    assert mod_results[0]["material_id"] == "Metal032"
