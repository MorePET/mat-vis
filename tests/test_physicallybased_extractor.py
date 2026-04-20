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

from mat_vis_baker.sources.physicallybased import (
    _complex_ior,
    _normalize_tags,
    _specular_f0,
    _transmission,
    fetch,
)


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
    by_name = {r.mat_vis.name: r for r in records}

    assert by_name["Aluminum"].mat_vis.tags == ["aluminium", "mirror"]
    # [""] collapses to [] — not a singleton-empty-string list
    assert by_name["Banana"].mat_vis.tags == []
    # case-folded, de-duplicated, order preserved
    assert by_name["Car Paint"].mat_vis.tags == ["acrylic", "coat", "car paint", "lacquer"]


# ── Phase B helpers (#152) ──────────────────────────────────────


def test_specular_f0_extracts_rgb_triple() -> None:
    assert _specular_f0([0.04, 0.04, 0.04]) == [0.04, 0.04, 0.04]


def test_specular_f0_rejects_garbage() -> None:
    assert _specular_f0(None) is None
    assert _specular_f0([0.04, 0.04]) is None  # under-length
    assert _specular_f0(["bad", "worse", "meh"]) is None


def test_transmission_float_passthrough() -> None:
    assert _transmission(0.95) == 0.95
    assert _transmission(0) == 0.0
    assert _transmission(None) is None
    assert _transmission("not-a-float") is None


def test_complex_ior_passes_six_float_through() -> None:
    raw = [1.39, 7.61, 0.96, 6.55, 0.62, 5.25]
    assert _complex_ior(raw) == raw


def test_complex_ior_passes_arbitrary_length_in_phase_b() -> None:
    """Phase B doesn't strip — Phase C's allowlist can debate that later."""
    assert _complex_ior([1.0, 2.0]) == [1.0, 2.0]


def test_complex_ior_missing_is_none() -> None:
    assert _complex_ior(None) is None
    assert _complex_ior([]) is None


def test_complex_ior_rejects_non_floats() -> None:
    assert _complex_ior([1.0, "bad", 3.0]) is None


# ── end-to-end Phase B fields on the record ─────────────────────


def test_fetch_populates_phase_b_pbr_fields() -> None:
    """Every Phase B pbr.* field flows through from realistic upstream."""
    fake_api = [
        {
            "name": "Aluminum",
            "category": "metal",
            "description": "Polished aluminum with characteristic blueish sheen.",
            "color": [0.912, 0.914, 0.920],
            "ior": 1.39,
            "metalness": 1.0,
            "roughness": 0.0,
            "specularColor": [0.912, 0.914, 0.920],
            "transmission": 0.0,
            "complexIor": [1.39, 7.61, 0.96, 6.55, 0.62, 5.25],
            "tags": ["aluminium", "mirror"],
        },
        {
            "name": "Glass",
            "category": "glass",
            "description": "Crown optical glass.",
            "color": [1.0, 1.0, 1.0],
            "ior": 1.52,
            "metalness": 0.0,
            "roughness": 0.0,
            "specularColor": [0.04, 0.04, 0.04],
            "transmission": 1.0,
            "tags": ["glass", "transparent"],
            # no complexIor upstream on dielectrics
        },
        {
            "name": "Plaster",
            "category": "plaster",
            "color": [0.93, 0.93, 0.93],
            "ior": 1.5,
            # no description, no specularColor, no transmission, no complexIor
            "tags": [],
        },
    ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = fake_api

    with patch("mat_vis_baker.sources.physicallybased.retry_request", return_value=mock_resp):
        records = fetch()

    by_name = {r.mat_vis.name: r for r in records}

    al = by_name["Aluminum"].mat_vis
    assert al.description == "Polished aluminum with characteristic blueish sheen."
    assert al.pbr.specular_f0 == [0.912, 0.914, 0.920]
    assert al.pbr.transmission == 0.0
    assert al.pbr.complex_ior == [1.39, 7.61, 0.96, 6.55, 0.62, 5.25]

    glass = by_name["Glass"].mat_vis
    assert glass.pbr.specular_f0 == [0.04, 0.04, 0.04]
    assert glass.pbr.transmission == 1.0
    # absent upstream → None (not KeyError, not default-to-0)
    assert glass.pbr.complex_ior is None

    plaster = by_name["Plaster"].mat_vis
    assert plaster.description is None
    assert plaster.pbr.specular_f0 is None
    assert plaster.pbr.transmission is None
    assert plaster.pbr.complex_ior is None
    # existing fields still populated
    assert plaster.pbr.ior == 1.5
