"""Tests for the physicallybased.info extractor (#151).

v2026.04.0 shipped ``physicallybased.json`` with ``tags: []`` on every
entry even though upstream (``https://api.physicallybased.info/materials``)
provides a per-material ``tags`` list. The fetcher simply never passed
``tags=`` through to ``MaterialRecord``. This test locks in the fix:

- populated tags flow through, lowercased, stripped, de-duplicated
- the upstream ``[""]`` sentinel that several entries carry collapses
  to an empty list (not a list containing an empty string)
- a missing/None/non-list ``tags`` field is tolerated
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mat_vis_baker.sources.physicallybased import _normalize_tags, fetch


def test_normalize_tags_populated() -> None:
    assert _normalize_tags(["aluminium", "mirror"]) == ["aluminium", "mirror"]


def test_normalize_tags_drops_empty_sentinel() -> None:
    """Upstream uses ``[""]`` for ~3 entries (e.g. Banana, Brass). Drop it."""
    assert _normalize_tags([""]) == []


def test_normalize_tags_strips_lowercases_dedupes_preserves_order() -> None:
    assert _normalize_tags(["  Car Paint ", "coat", "CAR PAINT", "Coat", ""]) == [
        "car paint",
        "coat",
    ]


def test_normalize_tags_rejects_non_list() -> None:
    assert _normalize_tags(None) == []
    assert _normalize_tags("not a list") == []
    assert _normalize_tags(42) == []


def test_normalize_tags_skips_non_string_elements() -> None:
    assert _normalize_tags(["good", 7, None, "also good"]) == ["good", "also good"]


def test_fetch_populates_tags_from_upstream() -> None:
    """End-to-end: the API response shape upstream actually returns today
    (list-of-dicts with ``tags`` as list-of-strings) produces MaterialRecords
    whose ``tags`` field is populated + normalized."""
    fake_api = [
        {
            "name": "Aluminum",
            "category": "metal",
            "color": [0.9, 0.9, 0.9],
            "ior": 1.39,
            "metalness": 1.0,
            "roughness": 0.0,
            "tags": ["aluminium", "mirror"],
        },
        {
            "name": "Banana",
            "category": "organic",
            "color": [1.0, 0.8, 0.2],
            "ior": 1.45,
            "tags": [""],  # upstream sentinel for "no tags"
        },
        {
            "name": "Car Paint",
            "category": "plastic",
            "color": [0.5, 0.0, 0.0],
            "ior": 1.5,
            "tags": ["acrylic", "Coat", "car paint", "LACQUER", "acrylic"],
        },
    ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = fake_api

    with patch("mat_vis_baker.sources.physicallybased.retry_request", return_value=mock_resp):
        records = fetch()

    assert len(records) == 3
    by_name = {r.name: r for r in records}

    assert by_name["Aluminum"].tags == ["aluminium", "mirror"]
    # [""] collapses to [] — not a singleton-empty-string list
    assert by_name["Banana"].tags == []
    # case-folded, de-duplicated, order preserved
    assert by_name["Car Paint"].tags == ["acrylic", "coat", "car paint", "lacquer"]
