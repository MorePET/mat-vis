"""Tests for mat-vis#398: physicallybased ``transmissionDepth`` →
``PBRBlock.thickness`` mapping with the transmission>0 gate.

Pre-fix, the fetcher emitted ``pbr.thickness = None`` for every entry
even though ``transmissionDepth`` is in the upstream allowlist. Without
thickness, the glTF adapter emits no ``KHR_materials_volume.thicknessFactor``
and transmissive entries (Blood, Coffee — the only two with depth set
upstream today) render as thin sheets.

Convention mirrors the MTLX-parse path
(``_mtlx_scalars.py:660-674``): thickness is only meaningful when the
material is transmissive (``transmission > 0``); opaque entries drop the
value so the adapter doesn't ship a no-op volume extension.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mat_vis_baker.sources.physicallybased import _thickness, fetch


# ── unit-level helper coverage ──────────────────────────────────────


def test_thickness_transmissive_keeps_value() -> None:
    """transmission>0 + valid depth → float passthrough."""
    assert _thickness(0.08, 1.0) == 0.08


def test_thickness_opaque_drops_value() -> None:
    """transmission==0 → None even if depth authored upstream."""
    assert _thickness(0.08, 0.0) is None


def test_thickness_missing_transmission_drops_value() -> None:
    """transmission missing → treat as opaque (None)."""
    assert _thickness(0.08, None) is None


def test_thickness_missing_depth_returns_none() -> None:
    """No depth → None regardless of transmission."""
    assert _thickness(None, 1.0) is None
    assert _thickness(None, None) is None


def test_thickness_zero_or_negative_depth_drops() -> None:
    """Non-positive depth is dead authoring scaffold; drop it."""
    assert _thickness(0.0, 1.0) is None
    assert _thickness(-0.5, 1.0) is None


def test_thickness_non_numeric_raw_returns_none() -> None:
    assert _thickness("not a float", 1.0) is None


def test_thickness_non_numeric_transmission_returns_none() -> None:
    assert _thickness(0.08, "bogus") is None


# ── end-to-end fetcher coverage ─────────────────────────────────────


def test_fetch_maps_transmission_depth_to_thickness() -> None:
    """Mocked physicallybased.info response covering the three cases the
    issue calls out: transmissive→thickness set, opaque→thickness None,
    missing field→thickness None."""
    fake_api = [
        {
            "name": "Blood",
            "category": "liquid",
            "color": [0.5, 0.0, 0.0],
            "ior": 1.301,
            "roughness": 0.0,
            "transmission": 1.0,
            "transmissionDepth": 0.08,
            "tags": [],
        },
        {
            "name": "Aluminum",
            "category": "metal",
            "color": [0.9, 0.9, 0.9],
            "ior": 1.39,
            "metalness": 1.0,
            "roughness": 0.0,
            # opaque + authored depth must drop
            "transmission": 0.0,
            "transmissionDepth": 0.5,
            "tags": [],
        },
        {
            "name": "Banana",
            "category": "organic",
            "color": [1.0, 0.8, 0.2],
            "ior": 1.45,
            # no transmission, no depth — both None
            "tags": [],
        },
    ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = fake_api

    with patch(
        "mat_vis_baker.sources.physicallybased.retry_request",
        return_value=mock_resp,
    ):
        records = fetch()

    by_name = {r.mat_vis.name: r for r in records}

    blood = by_name["Blood"].mat_vis.pbr
    assert blood.transmission == 1.0
    assert blood.thickness == 0.08, (
        "transmissive material must carry upstream transmissionDepth "
        "through to PBRBlock.thickness (mat-vis#398)"
    )

    aluminum = by_name["Aluminum"].mat_vis.pbr
    assert aluminum.transmission == 0.0
    assert aluminum.thickness is None, (
        "opaque material must drop thickness even when upstream authored "
        "a depth value — KHR_materials_volume is rendering dead-code for "
        "transmission=0 (mirrors _mtlx_scalars.py:660-674)"
    )

    banana = by_name["Banana"].mat_vis.pbr
    assert banana.transmission is None
    assert banana.thickness is None
