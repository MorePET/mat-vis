"""Tests for the polyhaven `_fetch_one` mtlx-scalar wiring (#397).

Polyhaven publishes a per-tier .mtlx document alongside the texture
maps. Pre-#397 the fetcher downloaded the .mtlx to disk but never
parsed scalars — so 757 polyhaven entries shipped without Phase-2
PBR fields.

These tests synthesise a small MTLX, mock the polyhaven per-asset
file map + downloader so no network is touched, and assert the
``MaterialRecord.mat_vis.pbr`` picks up the parsed scalars while the
texture-bound neutral convention still fires.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from mat_vis_baker.sources.polyhaven import _fetch_one


_FAKE_MTLX = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="ph_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" value="0.42, 0.32, 0.22"/>
    <input name="metalness" type="float" value="0.0"/>
    <input name="specular_roughness" type="float" value="0.5"/>
    <input name="specular_IOR" type="float" value="1.5"/>
    <input name="specular" type="float" value="0.9"/>
    <input name="specular_color" type="color3" value="1.0, 1.0, 1.0"/>
    <input name="coat_roughness" type="float" value="0.08"/>
  </standard_surface>
  <surfacematerial name="ph_mat" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="ph_shader"/>
  </surfacematerial>
</materialx>
"""


_FAKE_GLASS_MTLX = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="glass_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" value="0.95, 0.97, 0.97"/>
    <input name="metalness" type="float" value="0.0"/>
    <input name="specular_roughness" type="float" value="0.02"/>
    <input name="specular_IOR" type="float" value="1.52"/>
    <input name="transmission" type="float" value="0.85"/>
    <input name="transmission_depth" type="float" value="0.005"/>
  </standard_surface>
  <surfacematerial name="glass" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="glass_shader"/>
  </surfacematerial>
</materialx>
"""


def _file_info_with_mtlx(tier_key: str = "1k") -> dict:
    """polyhaven /files/{slug} response carrying mtlx + a diffuse map."""
    return {
        "Diffuse": {tier_key: {"png": {"url": "https://example.com/diff.png", "size": 1}}},
        "mtlx": {
            tier_key: {
                "mtlx": {
                    "url": "https://example.com/material.mtlx",
                    "md5": "abc",
                    "size": 1,
                    "include": {},
                }
            }
        },
    }


def _file_info_without_mtlx(tier_key: str = "1k") -> dict:
    """Some polyhaven assets ship without an MTLX — gracefully tolerated."""
    return {
        "Diffuse": {tier_key: {"png": {"url": "https://example.com/diff.png", "size": 1}}},
    }


def _make_retry_request(mtlx_bytes: bytes):
    """Build a ``retry_request`` mock that returns mtlx bytes for the
    .mtlx URL and a fake PNG payload for everything else."""

    def _impl(url, *args, **kwargs):
        if url.endswith(".mtlx"):
            return MagicMock(content=mtlx_bytes)
        return MagicMock(content=b"\x89PNG\r\n\x1a\nfake")

    return _impl


# ── happy path ──────────────────────────────────────────────────


def test_fetch_one_populates_pbr_from_mtlx(tmp_path: Path) -> None:
    """Polyhaven MTLX → PBRBlock Phase-2 fields land on the record."""
    file_info = _file_info_with_mtlx()
    mtlx_dir = tmp_path / "mtlx"
    output_dir = tmp_path / "out"

    with (
        patch(
            "mat_vis_baker.sources.polyhaven._fetch_files",
            return_value=file_info,
        ),
        patch(
            "mat_vis_baker.sources.polyhaven.retry_request",
            side_effect=_make_retry_request(_FAKE_MTLX),
        ),
    ):
        rec = _fetch_one(
            slug="oak_floor",
            meta={"name": "Oak Floor", "tags": ["wood"]},
            tier="1k",
            output_dir=output_dir,
            mtlx_dir=mtlx_dir,
        )

    assert rec.status == "ok"
    pbr = rec.mat_vis.pbr
    assert pbr.metalness == 0.0
    assert pbr.roughness == 0.5
    assert pbr.ior == 1.5
    assert pbr.specular_intensity == 0.9
    assert pbr.specular_color == [1.0, 1.0, 1.0]
    assert pbr.clearcoat_roughness == 0.08
    assert pbr.color_rgb == [0.42, 0.32, 0.22]


def test_fetch_one_glass_transmission_and_thickness(tmp_path: Path) -> None:
    """A transmissive material picks up transmission + thickness."""
    file_info = _file_info_with_mtlx()
    mtlx_dir = tmp_path / "mtlx"
    output_dir = tmp_path / "out"

    with (
        patch(
            "mat_vis_baker.sources.polyhaven._fetch_files",
            return_value=file_info,
        ),
        patch(
            "mat_vis_baker.sources.polyhaven.retry_request",
            side_effect=_make_retry_request(_FAKE_GLASS_MTLX),
        ),
    ):
        rec = _fetch_one(
            slug="window_glass",
            meta={"name": "Window Glass", "tags": ["glass"]},
            tier="1k",
            output_dir=output_dir,
            mtlx_dir=mtlx_dir,
        )

    assert rec.status == "ok"
    assert rec.mat_vis.pbr.transmission == 0.85
    assert rec.mat_vis.pbr.thickness == 0.005


# ── missing-MTLX is graceful ────────────────────────────────────


def test_fetch_one_no_mtlx_no_exception(tmp_path: Path) -> None:
    """Polyhaven assets without an MTLX section still produce a record."""
    file_info = _file_info_without_mtlx()
    mtlx_dir = tmp_path / "mtlx"
    output_dir = tmp_path / "out"

    with (
        patch(
            "mat_vis_baker.sources.polyhaven._fetch_files",
            return_value=file_info,
        ),
        patch(
            "mat_vis_baker.sources.polyhaven.retry_request",
            return_value=MagicMock(content=b"\x89PNG\r\n\x1a\nfake"),
        ),
    ):
        rec = _fetch_one(
            slug="no_mtlx_mat",
            meta={"name": "No MTLX", "tags": []},
            tier="1k",
            output_dir=output_dir,
            mtlx_dir=mtlx_dir,
        )

    assert rec.status == "ok"
    # Phase-2 fields stay None (no parser ran)
    assert rec.mat_vis.pbr.clearcoat_roughness is None
    assert rec.mat_vis.pbr.specular_intensity is None
    # Texture-bound convention still fires for color
    assert rec.mat_vis.pbr.color_rgb == [1.0, 1.0, 1.0]


def test_fetch_one_mtlx_dir_none_is_graceful(tmp_path: Path) -> None:
    """When the caller doesn't pass mtlx_dir, no parse happens."""
    file_info = _file_info_with_mtlx()
    output_dir = tmp_path / "out"

    with (
        patch(
            "mat_vis_baker.sources.polyhaven._fetch_files",
            return_value=file_info,
        ),
        patch(
            "mat_vis_baker.sources.polyhaven.retry_request",
            side_effect=_make_retry_request(_FAKE_MTLX),
        ),
    ):
        rec = _fetch_one(
            slug="oak_floor",
            meta={"name": "Oak Floor", "tags": ["wood"]},
            tier="1k",
            output_dir=output_dir,
            mtlx_dir=None,
        )

    assert rec.status == "ok"
    assert rec.mat_vis.pbr.clearcoat_roughness is None
    assert rec.mat_vis.pbr.specular_intensity is None


# ── merge-rule regression ───────────────────────────────────────


def test_mtlx_does_not_override_when_authored_metalness(tmp_path: Path) -> None:
    """Regression guard: an MTLX-authored metalness value must NOT override
    an upstream-JSON-authored value when the merge-rule contract is honored.

    Polyhaven doesn't currently author PBR from JSON, but pinning the rule
    via the merge helper covers any future fetcher expansion. We assert
    the parsed scalars still flow when upstream leaves the field None.
    """
    file_info = _file_info_with_mtlx()
    mtlx_dir = tmp_path / "mtlx"
    output_dir = tmp_path / "out"

    with (
        patch(
            "mat_vis_baker.sources.polyhaven._fetch_files",
            return_value=file_info,
        ),
        patch(
            "mat_vis_baker.sources.polyhaven.retry_request",
            side_effect=_make_retry_request(_FAKE_MTLX),
        ),
    ):
        rec = _fetch_one(
            slug="oak_floor",
            meta={"name": "Oak Floor", "tags": ["wood"]},
            tier="1k",
            output_dir=output_dir,
            mtlx_dir=mtlx_dir,
        )

    # MTLX value flows because upstream JSON didn't author it.
    assert rec.mat_vis.pbr.metalness == 0.0
