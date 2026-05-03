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
import logging
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from mat_vis_baker.sources.gpuopen import (
    UPSTREAM_ALLOWLIST,
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
        # Matches live upstream as of 2026-04 (mat-vis#168 probe: 454/454
        # gpuopen materials carry this exact string). normalize_spdx
        # maps it to "MIT".
        "license": "MIT Public Domain",
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


def test_iso_date_rejects_malformed_separators() -> None:
    """Phase B review follow-up (#152): ``raw[:10]`` used to pass
    ``"2022/08/01"`` through verbatim. The regex guard rejects anything
    that isn't strict YYYY-MM-DD."""
    assert _iso_date("2022/08/01") is None
    assert _iso_date("2022.08.01") is None
    assert _iso_date("not-a-date") is None
    assert _iso_date("22-08-01-ignored") is None  # slice would be "22-08-01-", not ISO
    assert _iso_date("20220801T12") is None  # no separators


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


# ── upstream mirror (Phase C, mat-vis#152) ──────────────────────


def test_fetch_one_populates_upstream_block(tmp_path: Path) -> None:
    mat = _mat(
        license="MIT",
        material_type="standard_surface",
        status="published",
        created_date="2022-01-01T00:00:00Z",
        mtlx_filename="oak.mtlx",
        mtlx_material_name="M_Oak",
    )
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(mat, "1k", tmp_path, mtlx_dir=None)

    assert rec.upstream is not None
    assert rec.upstream.source == "gpuopen"
    raw = rec.upstream.raw or {}
    assert raw["id"] == "abcd-1234"
    assert raw["title"] == "Oak Planks"
    assert raw["license"] == "MIT"
    assert raw["mtlx_filename"] == "oak.mtlx"


def test_fetch_one_strips_fetcher_internals_and_packages(tmp_path: Path) -> None:
    """``_category_title`` / ``_tag_titles`` / ``_packages_detail`` are
    fetcher-internal, and ``packages`` / ``renders`` are too heavy to
    mirror. None of them should leak into ``upstream.raw``."""
    mat = _mat(
        packages=["uuid-1", "uuid-2"],
        renders=["render-a"],
        renders_order=["render-a"],
        viewer_package="uuid-viewer",
        favorite=False,
        notification_status="none",
    )
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(mat, "1k", tmp_path, mtlx_dir=None)
    raw = (rec.upstream.raw if rec.upstream else {}) or {}
    for dropped in (
        "_category_title",
        "_tag_titles",
        "_packages_detail",
        "packages",
        "renders",
        "renders_order",
        "viewer_package",
        "favorite",
        "notification_status",
    ):
        assert dropped not in raw, dropped


# ── license_spdx routing through normalize_spdx (mat-vis#168) ───


def test_fetch_one_routes_license_through_normalize_spdx(tmp_path: Path) -> None:
    """Upstream ``license="MIT Public Domain"`` maps to SPDX ``"MIT"``
    via ``normalize_spdx`` — not a hardcoded constant (mat-vis#168)."""
    mat = _mat(license="MIT Public Domain")
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(mat, "1k", tmp_path, mtlx_dir=None)
    assert rec.mat_vis.attribution.license_spdx == "MIT"


def test_fetch_one_unknown_license_falls_back_to_noassertion(tmp_path: Path, caplog) -> None:
    """Drifted / new upstream license strings surface as ``"NOASSERTION"``
    with a warning rather than failing the bake."""
    mat = _mat(license="Something Weird")
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with caplog.at_level(logging.WARNING, logger="mat-vis-baker"):
        with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
            rec = _fetch_one(mat, "1k", tmp_path, mtlx_dir=None)
    assert rec.mat_vis.attribution.license_spdx == "NOASSERTION"
    assert "unknown upstream license" in caplog.text


def test_fetch_one_missing_license_falls_back_to_noassertion(tmp_path: Path) -> None:
    """A record with no ``license`` key still produces a schema-valid
    SPDX identifier (``"NOASSERTION"``)."""
    mat = _mat()
    mat.pop("license", None)
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(mat, "1k", tmp_path, mtlx_dir=None)
    assert rec.mat_vis.attribution.license_spdx == "NOASSERTION"


def test_upstream_allowlist_includes_mtlx_anchor() -> None:
    """``mtlx_filename`` and ``mtlx_material_name`` anchor our MaterialX
    republish back to the upstream source record. They must be allowlisted."""
    assert "mtlx_filename" in UPSTREAM_ALLOWLIST
    assert "mtlx_material_name" in UPSTREAM_ALLOWLIST
    assert "packages" not in UPSTREAM_ALLOWLIST
    assert "renders" not in UPSTREAM_ALLOWLIST
