"""Unified search API (#85 item 5).

Goal: one canonical signature. Module-level ``search()`` forwards to
``MatVisClient.search()``; the scalar-shorthand form is handled by the
method itself, not by a divergent module-level function.

This kills the two-surface problem where the same operation had two
different parameter conventions depending on where you called it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import mat_vis_client


MOCK_INDEX = [
    {
        "material_id": "Metal032",
        "source": "ambientcg",
        "category": "metal",
        "roughness": 0.3,
        "metalness": 1.0,
        "available_tiers": ["1k", "2k"],
    },
    {
        "material_id": "Metal050A",
        "source": "ambientcg",
        "category": "metal",
        "roughness": 0.5,
        "metalness": 1.0,
        "available_tiers": ["1k"],
    },
    {
        "material_id": "Wood002",
        "source": "ambientcg",
        "category": "wood",
        "roughness": 0.7,
        "metalness": 0.0,
        "available_tiers": ["1k"],
    },
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


def test_method_search_score_option_sorts_by_distance():
    """search(roughness=0.3, score=True) sorts by |r - 0.3|."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    with patch.object(c, "sources", return_value=["ambientcg"]):
        with patch.object(c, "index", return_value=MOCK_INDEX):
            with patch.object(c, "categories", return_value=frozenset(["metal", "wood"])):
                results = c.search(category="metal", roughness=0.3, score=True)
    # Metal032 (0.3, diff 0) comes before Metal050A (0.5, diff 0.2)
    ids = [r["material_id"] for r in results]
    assert ids[0] == "Metal032"
    for r in results:
        assert "score" in r


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
                method_results = client2.search(category="metal", roughness=0.3, score=True)

    # Module-level is equivalent to method-level with score=True
    assert [r["material_id"] for r in mod_results] == [r["material_id"] for r in method_results]
