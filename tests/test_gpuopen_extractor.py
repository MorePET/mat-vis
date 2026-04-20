"""Tests for the gpuopen extractor — Phase B curated fields (#152).

gpuopen's /api/materials/<uuid>/ response carries ``description``
(often null), a single ``author`` string (usually ``"AMD"``),
``published_date`` / ``updated_date`` ISO datetimes, and a package
label like ``"1k 8b"`` — we derive ``max_resolution_px`` from the tier
the bake is running (not from the label, since a single bake is tied
to one tier).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from mat_vis_baker.sources.gpuopen import (
    _authors,
    _fetch_one,
    _iso_date,
    _max_resolution_px,
)


# ── helpers ─────────────────────────────────────────────────────


def _fake_zip_bytes() -> bytes:
    """Tiny ZIP with a ``.mtlx`` + one basecolor PNG — enough to flow through."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("oak/material.mtlx", b"<materialx/>")
        zf.writestr("oak/oak_basecolor.png", b"\x89PNG\r\n\x1a\nfake")
    return buf.getvalue()


def _mat(**overrides: object) -> dict:
    base: dict = {
        "id": "abcd-1234",
        "title": "Oak Planks",
        "description": "Warm oak planks.",
        "author": "AMD",
        "published_date": "2022-08-01T12:00:00Z",
        "updated_date": "2023-03-15T09:30:00Z",
        "_category_title": "Wood",
        "_tag_titles": ["wood", "planks"],
        "_packages_detail": [
            {
                "id": "pkg-1k-8b",
                "label": "1k 8b",
                "file_url": "https://example.com/oak_1k_8b.zip",
            }
        ],
    }
    base.update(overrides)
    return base


# ── _authors ────────────────────────────────────────────────────


def test_authors_wraps_single_string() -> None:
    assert _authors({"author": "AMD"}) == ["AMD"]


def test_authors_strips_whitespace() -> None:
    assert _authors({"author": "  AMD  "}) == ["AMD"]


def test_authors_missing_is_empty() -> None:
    assert _authors({}) == []
    assert _authors({"author": None}) == []
    assert _authors({"author": ""}) == []
    assert _authors({"author": "   "}) == []


# ── _iso_date ───────────────────────────────────────────────────


def test_iso_date_truncates_datetime_to_date() -> None:
    assert _iso_date("2022-08-01T12:00:00Z") == "2022-08-01"


def test_iso_date_preserves_date_only() -> None:
    assert _iso_date("2022-08-01") == "2022-08-01"


def test_iso_date_missing_is_none() -> None:
    assert _iso_date(None) is None
    assert _iso_date("") is None
    assert _iso_date(42) is None


# ── _max_resolution_px ──────────────────────────────────────────


def test_max_resolution_px_from_tier() -> None:
    assert _max_resolution_px("1k") == [1024, 1024]
    assert _max_resolution_px("4k") == [4096, 4096]


def test_max_resolution_px_unknown_tier_is_none() -> None:
    assert _max_resolution_px("huge") is None


# ── _fetch_one end-to-end ───────────────────────────────────────


def test_fetch_one_populates_phase_b_fields(tmp_path: Path) -> None:
    mat = _mat()
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(mat, "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "ok"
    mv = rec.mat_vis
    assert mv.name == "Oak Planks"
    assert mv.category == "wood"
    assert mv.tags == ["wood", "planks"]
    assert mv.description == "Warm oak planks."
    assert mv.physical.max_resolution_px == [1024, 1024]
    # upstream doesn't provide physical dimensions; stays null
    assert mv.physical.dimensions_m is None
    assert mv.attribution.authors == ["AMD"]
    assert mv.attribution.license_spdx == "MIT"
    assert mv.attribution.source_url == (
        "https://matlib.gpuopen.com/main/materials/all?material=abcd-1234"
    )
    assert mv.dates.published == "2022-08-01"
    assert mv.dates.updated == "2023-03-15"
    assert mv.upstream_id == "abcd-1234"


def test_fetch_one_handles_null_description(tmp_path: Path) -> None:
    """gpuopen frequently returns description: null upstream."""
    mat = _mat(description=None)
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(mat, "1k", tmp_path, mtlx_dir=None)
    assert rec.mat_vis.description is None


def test_fetch_one_handles_missing_dates(tmp_path: Path) -> None:
    mat = _mat(published_date=None, updated_date=None)
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(mat, "1k", tmp_path, mtlx_dir=None)
    assert rec.mat_vis.dates.published is None
    assert rec.mat_vis.dates.updated is None


def test_fetch_one_failed_record_still_has_stable_shape(tmp_path: Path) -> None:
    """A material with no matching package returns a failed record whose
    mat_vis block still carries every Phase B field we CAN derive from
    the material dict (resolution still falls out of the tier)."""
    mat = _mat(_packages_detail=[])  # no packages → immediate failure
    rec = _fetch_one(mat, "2k", tmp_path, mtlx_dir=None)
    assert rec.status == "failed"
    mv = rec.mat_vis
    # metadata we know from the /materials/ endpoint still populates
    assert mv.name == "Oak Planks"
    assert mv.description == "Warm oak planks."
    assert mv.attribution.authors == ["AMD"]
    assert mv.physical.max_resolution_px == [2048, 2048]
    assert mv.dates.published == "2022-08-01"
