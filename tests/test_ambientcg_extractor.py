"""Tests for the ambientcg extractor — Phase B curated fields (#152).

Phase B populates every ``mat_vis.*`` curated field the upstream API can
provide, with deterministic fallback to ``None`` when a field is missing
or carries the "unknown" sentinel (``dimension* = 0``).

These tests lock the extraction in by driving the real upstream payload
shape from ``ambientcg.com/api/v2/full_json`` against a mocked ZIP
download, and asserting every Phase B field shows up on the resulting
``MaterialRecord.mat_vis``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from mat_vis_baker.sources.ambientcg import (
    UPSTREAM_ALLOWLIST,
    _dimensions_m,
    _fetch_one,
    _max_resolution_px,
)


# ── helpers ─────────────────────────────────────────────────────


def _fake_zip_bytes() -> bytes:
    """One-PNG ZIP with an ambientcg-shaped ``*_Color.png`` member."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Bricks097_1K-PNG/Bricks097_1K-PNG_Color.png", b"\x89PNG\r\n\x1a\nfake")
    return buf.getvalue()


def _entry(**overrides: object) -> dict:
    """Baseline ambientcg API entry; overrides mutate individual fields."""
    base: dict = {
        "assetId": "Bricks097",
        "displayName": "Bricks 097",
        "displayCategory": "Ceramic/Brick",
        "tags": ["brick", "red"],
        "description": "A red brick wall texture.",
        "dimensionX": 500,  # mm
        "dimensionY": 500,
        "dimensionZ": 20,
        "releaseDate": "2024-11-22T00:00:00Z",
        "downloadFolders": {
            "default": {
                "downloadFiletypeCategories": {
                    "zip": {
                        "downloads": [
                            {
                                "attribute": "1K-PNG",
                                "fullDownloadPath": "https://example.com/Bricks097_1K-PNG.zip",
                            }
                        ]
                    }
                }
            }
        },
    }
    base.update(overrides)
    return base


# ── _dimensions_m ───────────────────────────────────────────────


def test_dimensions_m_converts_mm_to_m() -> None:
    assert _dimensions_m({"dimensionX": 500, "dimensionY": 250, "dimensionZ": 20}) == [
        0.5,
        0.25,
        0.02,
    ]


def test_dimensions_m_zero_becomes_none_per_axis() -> None:
    assert _dimensions_m({"dimensionX": 0, "dimensionY": 500, "dimensionZ": 0}) == [
        None,
        0.5,
        None,
    ]


def test_dimensions_m_all_zero_returns_none() -> None:
    """Harmonized with polyhaven (Phase C, #152 review): no measurable
    axis → ``None`` at top level, not ``[None, None, None]``. Callers
    get a single sentinel regardless of source."""
    assert _dimensions_m({"dimensionX": 0, "dimensionY": 0, "dimensionZ": 0}) is None


def test_dimensions_m_all_missing_returns_none() -> None:
    assert _dimensions_m({"assetId": "x"}) is None


def test_dimensions_m_tolerates_bad_values() -> None:
    assert _dimensions_m({"dimensionX": "nope", "dimensionY": None, "dimensionZ": 100}) == [
        None,
        None,
        0.1,
    ]


# ── _max_resolution_px ──────────────────────────────────────────


def test_max_resolution_px_derived_from_tier() -> None:
    assert _max_resolution_px("1k") == [1024, 1024]
    assert _max_resolution_px("4k") == [4096, 4096]
    assert _max_resolution_px("128") == [128, 128]


def test_max_resolution_px_unknown_tier_is_none() -> None:
    assert _max_resolution_px("99k") is None


# ── _fetch_one end-to-end ───────────────────────────────────────


def test_fetch_one_populates_phase_b_fields(tmp_path: Path) -> None:
    entry = _entry()
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "ok"
    mv = rec.mat_vis
    assert mv.name == "Bricks 097"
    assert mv.category == "ceramic"
    assert mv.tags == ["brick", "red"]
    assert mv.description == "A red brick wall texture."
    assert mv.physical.dimensions_m == [0.5, 0.5, 0.02]
    assert mv.physical.max_resolution_px == [1024, 1024]
    assert mv.attribution.license_spdx == "CC0-1.0"
    assert mv.attribution.source_url == "https://ambientcg.com/a/Bricks097"
    assert mv.dates.published == "2024-11-22"
    assert mv.dates.updated == "2024-11-22"
    assert mv.upstream_id == "Bricks097"


def test_fetch_one_handles_missing_description(tmp_path: Path) -> None:
    entry = _entry(description=None)
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)
    assert rec.mat_vis.description is None


def test_fetch_one_handles_missing_dimensions(tmp_path: Path) -> None:
    entry = _entry()
    for k in ("dimensionX", "dimensionY", "dimensionZ"):
        entry.pop(k, None)
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(entry, "2k", tmp_path, mtlx_dir=None)
    assert rec.mat_vis.physical.dimensions_m is None
    assert rec.mat_vis.physical.max_resolution_px == [2048, 2048]


def test_fetch_one_handles_zero_dimensions(tmp_path: Path) -> None:
    """Upstream leaves 0 for "not measured" on synthetic / abstract textures.

    Harmonized with polyhaven (Phase C, #152 review): zero across the board
    maps to ``None`` top-level, not ``[None, None, None]``."""
    entry = _entry(dimensionX=0, dimensionY=0, dimensionZ=0)
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)
    assert rec.mat_vis.physical.dimensions_m is None


def test_fetch_one_builds_stable_mat_vis_shape(tmp_path: Path) -> None:
    """Even a minimal entry flows through with all nested blocks present."""
    entry = _entry(description=None, tags=[])
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)

    # every sub-block is a real dataclass instance, not None
    assert rec.mat_vis.physical is not None
    assert rec.mat_vis.pbr is not None
    assert rec.mat_vis.attribution is not None
    assert rec.mat_vis.dates is not None
    # ambientcg doesn't expose scalar PBR properties upstream, but the
    # baker-side glTF-MR neutral-multiplier convention (mat-vis#290
    # follow-up) populates color_rgb=[1,1,1] because _fake_zip_bytes()
    # ships a *_Color.png. roughness/metalness stay None — no matching
    # texture in this fixture.
    assert rec.mat_vis.pbr.color_rgb == [1.0, 1.0, 1.0]
    assert rec.mat_vis.pbr.roughness is None
    assert rec.mat_vis.pbr.metalness is None


# ── upstream mirror (Phase C, mat-vis#152) ──────────────────────


def test_fetch_one_populates_upstream_block(tmp_path: Path) -> None:
    """``upstream.raw`` carries allowlisted upstream keys verbatim."""
    entry = _entry()
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)

    assert rec.upstream is not None
    assert rec.upstream.source == "ambientcg"
    assert rec.upstream.schema_version == 1
    assert rec.upstream.fetched_at is not None
    assert rec.upstream.fetched_at.endswith("Z")
    raw = rec.upstream.raw
    assert raw is not None
    # allowlisted keys that _entry() populates should pass through
    assert raw["assetId"] == "Bricks097"
    assert raw["displayName"] == "Bricks 097"
    assert raw["displayCategory"] == "Ceramic/Brick"
    assert raw["tags"] == ["brick", "red"]
    assert raw["dimensionX"] == 500


def test_fetch_one_strips_non_allowlisted_keys(tmp_path: Path) -> None:
    """``downloadFolders`` is explicitly out of the allowlist — the tree
    is large, bake-internal, and would churn with every upstream URL
    rotation. It must be absent from ``upstream.raw``."""
    entry = _entry()
    mock_resp = MagicMock(content=_fake_zip_bytes())
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)

    assert rec.upstream is not None
    raw = rec.upstream.raw or {}
    assert "downloadFolders" not in raw
    # Also a handful of other known-dropped keys
    for dropped in (
        "previewLinks",
        "previewImage",
        "variations",
        "basedOnThis",
        "nextVariationAssetId",
    ):
        assert dropped not in raw


def test_fetch_one_sets_upstream_even_on_failure(tmp_path: Path) -> None:
    """``_fetch_one`` must attach ``upstream`` on the failed-download path too —
    the schema-diff gate compares allowlist drift per-record, including
    failures, so the block is a stable contract regardless of ``status``."""
    entry = _entry()
    with patch(
        "mat_vis_baker.sources.ambientcg.retry_request",
        side_effect=RuntimeError("boom"),
    ):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "failed"
    assert rec.upstream is not None
    assert rec.upstream.source == "ambientcg"
    assert (rec.upstream.raw or {}).get("assetId") == "Bricks097"


def test_upstream_allowlist_locks_conservative_keyset() -> None:
    """If we bump the allowlist, the test must change too — this is the
    deliberate friction that makes ``Phase C`` allowlist edits reviewable."""
    assert "assetId" in UPSTREAM_ALLOWLIST
    assert "dimensionX" in UPSTREAM_ALLOWLIST
    assert "downloadFolders" not in UPSTREAM_ALLOWLIST
    assert "previewLinks" not in UPSTREAM_ALLOWLIST


# ── glTF-MR neutral-multiplier convention (mat-vis#290 follow-up) ──
#
# ambientcg doesn't expose scalar PBR properties upstream — the only
# path that can populate ``pbr.*`` is the baker-side convention. These
# tests pin that wire-up so the substrate carries spec-aligned scalars
# whenever the matching texture is in the baked set, and stays
# all-None otherwise.


def _multi_channel_zip_bytes() -> bytes:
    """ambientcg-shaped ZIP carrying Color + Metalness + Roughness PNGs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for suffix in ("Color", "Metalness", "Roughness"):
            zf.writestr(
                f"Bricks097_1K-PNG/Bricks097_1K-PNG_{suffix}.png",
                b"\x89PNG\r\n\x1a\nfake",
            )
    return buf.getvalue()


def _normal_only_zip_bytes() -> bytes:
    """ambientcg-shaped ZIP carrying ONLY a normal map — no PBR scalar texture."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Bricks097_1K-PNG/Bricks097_1K-PNG_NormalGL.png",
            b"\x89PNG\r\n\x1a\nfake",
        )
    return buf.getvalue()


def test_ambientcg_pbr_convention_applied(tmp_path: Path) -> None:
    """color + metalness + roughness textures → all three scalars filled."""
    entry = _entry()
    mock_resp = MagicMock(content=_multi_channel_zip_bytes())
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "ok"
    assert set(rec.maps) >= {"color", "metalness", "roughness"}
    assert rec.mat_vis.pbr.color_rgb == [1.0, 1.0, 1.0]
    assert rec.mat_vis.pbr.metalness == 1.0
    assert rec.mat_vis.pbr.roughness == 1.0


def test_ambientcg_pbr_no_convention_when_no_pbr_texture(tmp_path: Path) -> None:
    """Only a normal map shipped → all PBR scalars stay None."""
    entry = _entry()
    mock_resp = MagicMock(content=_normal_only_zip_bytes())
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "ok"
    assert "normal" in rec.maps
    assert "color" not in rec.maps
    assert rec.mat_vis.pbr.color_rgb is None
    assert rec.mat_vis.pbr.metalness is None
    assert rec.mat_vis.pbr.roughness is None


def test_ambientcg_pbr_all_none_on_failed_fetch(tmp_path: Path) -> None:
    """Failed-fetch path (textures empty) → pbr stays the default
    empty PBRBlock with everything None."""
    entry = _entry()
    with patch(
        "mat_vis_baker.sources.ambientcg.retry_request",
        side_effect=RuntimeError("boom"),
    ):
        rec = _fetch_one(entry, "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "failed"
    assert rec.mat_vis.pbr.color_rgb is None
    assert rec.mat_vis.pbr.metalness is None
    assert rec.mat_vis.pbr.roughness is None
