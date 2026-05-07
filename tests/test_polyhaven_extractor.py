"""Tests for the polyhaven extractor — Phase B curated fields (#152).

Polyhaven's ``/info/<slug>`` payload is the richest of the four sources:
description, physical dimensions (mm 2-tuple), max resolution (px),
per-author attribution dict, and a Unix-epoch publish date. Phase B
wires all of them into ``mat_vis.*``.

These tests drive each helper in isolation + an end-to-end ``_fetch_one``
against a realistic mocked upstream payload.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from mat_vis_baker.sources.polyhaven import (
    UPSTREAM_ALLOWLIST,
    _authors,
    _dimensions_m,
    _fetch_one,
    _max_resolution_px,
    _published_date,
)


# ── _dimensions_m ───────────────────────────────────────────────


def test_dimensions_m_converts_mm_two_tuple_to_m_triple() -> None:
    assert _dimensions_m([1000, 2000]) == [1.0, 2.0, None]


def test_dimensions_m_zero_axis_becomes_none() -> None:
    assert _dimensions_m([0, 1500]) == [None, 1.5, None]


def test_dimensions_m_both_zero_returns_none() -> None:
    assert _dimensions_m([0, 0]) is None


def test_dimensions_m_none_or_missing() -> None:
    assert _dimensions_m(None) is None
    assert _dimensions_m([]) is None
    assert _dimensions_m([500]) is None  # under-length


def test_dimensions_m_dict_shape_is_tolerated() -> None:
    """Some polyhaven entries upstream carry dict-shaped dimensions."""
    assert _dimensions_m({"x": 1000, "y": 500}) == [1.0, 0.5, None]


def test_dimensions_m_tolerates_bad_values() -> None:
    assert _dimensions_m(["bad", 1000]) == [None, 1.0, None]


# ── _max_resolution_px ──────────────────────────────────────────


def test_max_resolution_px_passes_through() -> None:
    assert _max_resolution_px([8192, 8192]) == [8192, 8192]


def test_max_resolution_px_rejects_garbage() -> None:
    assert _max_resolution_px(None) is None
    assert _max_resolution_px([1024]) is None
    assert _max_resolution_px(["bad", "worse"]) is None


# ── _authors ────────────────────────────────────────────────────


def test_authors_extracts_keys_from_dict() -> None:
    assert _authors({"authors": {"Rob Tuytel": "All", "Jarod Guest": "Scan"}}) == [
        "Rob Tuytel",
        "Jarod Guest",
    ]


def test_authors_missing_is_empty_list() -> None:
    assert _authors({}) == []
    assert _authors({"authors": None}) == []
    assert _authors({"authors": ["not-a-dict"]}) == []


# ── _published_date ─────────────────────────────────────────────


def test_published_date_converts_unix_epoch_to_iso_date() -> None:
    # 2023-06-15 00:00:00 UTC = 1686787200
    assert _published_date({"date_published": 1686787200}) == "2023-06-15"


def test_published_date_missing_is_none() -> None:
    assert _published_date({}) is None
    assert _published_date({"date_published": None}) is None


def test_published_date_bad_value_is_none() -> None:
    assert _published_date({"date_published": "not-a-timestamp"}) is None


# ── _fetch_one end-to-end ───────────────────────────────────────


def _meta(**overrides: object) -> dict:
    base: dict = {
        "name": "Wood Floor",
        "categories": ["wood"],
        "tags": ["wood", "floor", "planks"],
        "description": "Seamless wood floor texture.",
        "dimensions": [2000, 2000],
        "max_resolution": [8192, 8192],
        "authors": {"Rob Tuytel": "All"},
        "date_published": 1686787200,
    }
    base.update(overrides)
    return base


def _file_info(tier_key: str = "1k") -> dict:
    """Shape matching polyhaven ``/files/<slug>``."""
    return {
        "Diffuse": {tier_key: {"png": {"url": "https://example.com/diff.png"}}},
    }


def test_fetch_one_populates_phase_b_fields(tmp_path: Path) -> None:
    meta = _meta()
    file_info = _file_info()
    png_resp = MagicMock(content=b"\x89PNG\r\n\x1a\nfake")

    with (
        patch("mat_vis_baker.sources.polyhaven._fetch_files", return_value=file_info),
        patch("mat_vis_baker.sources.polyhaven.retry_request", return_value=png_resp),
    ):
        rec = _fetch_one("wood_floor", meta, "1k", tmp_path)

    assert rec.status == "ok"
    mv = rec.mat_vis
    assert mv.name == "Wood Floor"
    assert mv.category == "wood"
    assert mv.tags == ["wood", "floor", "planks"]
    assert mv.description == "Seamless wood floor texture."
    assert mv.physical.dimensions_m == [2.0, 2.0, None]
    assert mv.physical.max_resolution_px == [8192, 8192]
    assert mv.attribution.authors == ["Rob Tuytel"]
    assert mv.attribution.license_spdx == "CC0-1.0"
    assert mv.attribution.source_url == "https://polyhaven.com/a/wood_floor"
    assert mv.dates.published == "2023-06-15"
    assert mv.upstream_id == "wood_floor"


def test_fetch_one_tolerates_missing_optional_fields(tmp_path: Path) -> None:
    """polyhaven entries with sparse metadata still flow through cleanly."""
    meta = _meta(
        description=None,
        dimensions=None,
        max_resolution=None,
        authors=None,
        date_published=None,
    )
    png_resp = MagicMock(content=b"\x89PNG\r\n\x1a\nfake")
    with (
        patch("mat_vis_baker.sources.polyhaven._fetch_files", return_value=_file_info()),
        patch("mat_vis_baker.sources.polyhaven.retry_request", return_value=png_resp),
    ):
        rec = _fetch_one("wood_floor", meta, "1k", tmp_path)

    mv = rec.mat_vis
    assert mv.description is None
    assert mv.physical.dimensions_m is None
    assert mv.physical.max_resolution_px is None
    assert mv.attribution.authors == []
    assert mv.dates.published is None


# ── upstream mirror (Phase C, mat-vis#152) ──────────────────────


def test_fetch_one_populates_upstream_block(tmp_path: Path) -> None:
    meta = _meta()
    png_resp = MagicMock(content=b"\x89PNG\r\n\x1a\nfake")
    with (
        patch("mat_vis_baker.sources.polyhaven._fetch_files", return_value=_file_info()),
        patch("mat_vis_baker.sources.polyhaven.retry_request", return_value=png_resp),
    ):
        rec = _fetch_one("wood_floor", meta, "1k", tmp_path)

    assert rec.upstream is not None
    assert rec.upstream.source == "polyhaven"
    assert rec.upstream.schema_version == 1
    assert rec.upstream.fetched_at is not None
    raw = rec.upstream.raw or {}
    assert raw["name"] == "Wood Floor"
    assert raw["authors"] == {"Rob Tuytel": "All"}
    assert raw["tags"] == ["wood", "floor", "planks"]
    assert raw["dimensions"] == [2000, 2000]


def test_fetch_one_strips_non_allowlisted_keys(tmp_path: Path) -> None:
    """``files_hash`` + ``thumbnail_url`` must not leak into the mirror."""
    meta = _meta(files_hash={"Diffuse": "deadbeef"}, thumbnail_url="https://cdn/tn.png")
    png_resp = MagicMock(content=b"\x89PNG\r\n\x1a\nfake")
    with (
        patch("mat_vis_baker.sources.polyhaven._fetch_files", return_value=_file_info()),
        patch("mat_vis_baker.sources.polyhaven.retry_request", return_value=png_resp),
    ):
        rec = _fetch_one("wood_floor", meta, "1k", tmp_path)

    raw = (rec.upstream.raw if rec.upstream else {}) or {}
    assert "files_hash" not in raw
    assert "thumbnail_url" not in raw


def test_upstream_allowlist_locks_conservative_keyset() -> None:
    assert "name" in UPSTREAM_ALLOWLIST
    assert "authors" in UPSTREAM_ALLOWLIST
    assert "files_hash" not in UPSTREAM_ALLOWLIST
    assert "thumbnail_url" not in UPSTREAM_ALLOWLIST


# ── glTF-MR neutral-multiplier convention (mat-vis#290 follow-up) ──
#
# Polyhaven doesn't expose scalar PBR properties upstream — the only
# path that can populate ``pbr.*`` is the baker-side convention. These
# tests pin that wire-up so the substrate carries spec-aligned scalars
# whenever the matching texture is in the baked set, and stays
# all-None otherwise.


def _multi_channel_file_info(tier_key: str = "1k") -> dict:
    """polyhaven ``/files/<slug>`` shape with color + metalness + roughness."""
    return {
        "Diffuse": {tier_key: {"png": {"url": "https://example.com/diff.png"}}},
        "metal": {tier_key: {"png": {"url": "https://example.com/metal.png"}}},
        "rough": {tier_key: {"png": {"url": "https://example.com/rough.png"}}},
    }


def test_polyhaven_pbr_convention_applied(tmp_path: Path) -> None:
    """color + metalness + roughness textures → all three scalars filled."""
    meta = _meta()
    png_resp = MagicMock(content=b"\x89PNG\r\n\x1a\nfake")
    with (
        patch(
            "mat_vis_baker.sources.polyhaven._fetch_files",
            return_value=_multi_channel_file_info(),
        ),
        patch("mat_vis_baker.sources.polyhaven.retry_request", return_value=png_resp),
    ):
        rec = _fetch_one("wood_floor", meta, "1k", tmp_path)

    assert rec.status == "ok"
    assert set(rec.maps) >= {"color", "metalness", "roughness"}
    assert rec.mat_vis.pbr.color_rgb == [1.0, 1.0, 1.0]
    assert rec.mat_vis.pbr.metalness == 1.0
    assert rec.mat_vis.pbr.roughness == 1.0


def test_polyhaven_pbr_no_convention_when_no_texture(tmp_path: Path) -> None:
    """No matching texture in the baked set → pbr scalar stays None.

    Driven via the existing single-channel ``_file_info()`` helper —
    only ``Diffuse`` ships, so color_rgb fills but metalness / roughness
    must NOT.
    """
    meta = _meta()
    png_resp = MagicMock(content=b"\x89PNG\r\n\x1a\nfake")
    with (
        patch("mat_vis_baker.sources.polyhaven._fetch_files", return_value=_file_info()),
        patch("mat_vis_baker.sources.polyhaven.retry_request", return_value=png_resp),
    ):
        rec = _fetch_one("wood_floor", meta, "1k", tmp_path)

    assert rec.status == "ok"
    assert "color" in rec.maps
    assert "metalness" not in rec.maps
    assert "roughness" not in rec.maps
    assert rec.mat_vis.pbr.color_rgb == [1.0, 1.0, 1.0]
    assert rec.mat_vis.pbr.metalness is None
    assert rec.mat_vis.pbr.roughness is None


def test_polyhaven_pbr_all_none_on_failed_fetch(tmp_path: Path) -> None:
    """Failed-fetch path (textures empty) → pbr stays the default
    empty PBRBlock with everything None."""
    meta = _meta()
    with (
        patch("mat_vis_baker.sources.polyhaven._fetch_files", return_value={}),
    ):
        rec = _fetch_one("wood_floor", meta, "1k", tmp_path)

    assert rec.status == "failed"
    assert rec.mat_vis.pbr.color_rgb is None
    assert rec.mat_vis.pbr.metalness is None
    assert rec.mat_vis.pbr.roughness is None
