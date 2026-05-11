"""Tests for the ambientcg `_fetch_one` mtlx-scalar wiring (#397).

ambientcg ZIPs ship a .mtlx alongside the flat texture PNGs. Pre-#397
the fetcher extracted the .mtlx to disk but never parsed scalars — so
2,706 entries across ambientcg+polyhaven were emitting None for the
Phase-2 PBR fields (``clearcoat_roughness``, ``specular_color`` etc).

These tests mirror ``test_gpuopen_baker``'s synthesised-MTLX fixtures
and cover:
  - happy path: MTLX scalars land on the PBRBlock
  - missing-MTLX path: graceful, no exception, no fields lost
  - merge rule: an upstream-JSON-authored field is NOT overridden by
    MTLX (today no JSON path authors PBR, but the regression guard
    pins the additive-merge contract for future expansion)
  - texture-bound neutral convention still fires alongside parse
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from mat_vis_baker.common import PBRBlock, merge_mtlx_pbr_additive
from mat_vis_baker.sources.ambientcg import _fetch_one


_FAKE_MTLX = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="acg_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" value="0.55, 0.35, 0.25"/>
    <input name="metalness" type="float" value="0.0"/>
    <input name="specular_roughness" type="float" value="0.6"/>
    <input name="specular_IOR" type="float" value="1.45"/>
    <input name="specular" type="float" value="0.8"/>
    <input name="specular_color" type="color3" value="0.95, 0.92, 0.90"/>
    <input name="coat_roughness" type="float" value="0.12"/>
    <input name="transmission" type="float" value="0.0"/>
    <input name="transmission_dispersion" type="float" value="0.02"/>
  </standard_surface>
  <surfacematerial name="acg_mat" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="acg_shader"/>
  </surfacematerial>
</materialx>
"""


_FAKE_GLASS_MTLX = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="glass_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" value="0.9, 0.95, 0.95"/>
    <input name="metalness" type="float" value="0.0"/>
    <input name="specular_roughness" type="float" value="0.05"/>
    <input name="specular_IOR" type="float" value="1.52"/>
    <input name="transmission" type="float" value="1.0"/>
    <input name="transmission_depth" type="float" value="0.01"/>
  </standard_surface>
  <surfacematerial name="glass" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="glass_shader"/>
  </surfacematerial>
</materialx>
"""


def _zip_with_mtlx_and_color(mtlx_bytes: bytes = _FAKE_MTLX) -> bytes:
    """ambientcg-shaped ZIP: ``Bricks097_1K-PNG/...`` with mtlx + Color PNG."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Bricks097_1K-PNG/Bricks097.mtlx", mtlx_bytes)
        zf.writestr(
            "Bricks097_1K-PNG/Bricks097_1K-PNG_Color.png",
            b"\x89PNG\r\n\x1a\nfake",
        )
    return buf.getvalue()


def _zip_without_mtlx() -> bytes:
    """Older-format ambientcg ZIP with no .mtlx — must not break the fetch."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Bricks097_1K-PNG/Bricks097_1K-PNG_Color.png",
            b"\x89PNG\r\n\x1a\nfake",
        )
    return buf.getvalue()


def _entry() -> dict:
    return {
        "assetId": "Bricks097",
        "displayName": "Bricks 097",
        "displayCategory": "Ceramic/Brick",
        "tags": ["brick"],
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


# ── happy path ──────────────────────────────────────────────────


def test_fetch_one_populates_pbr_from_mtlx(tmp_path: Path) -> None:
    """The success path parses the .mtlx and PBRBlock fields are set."""
    mock_resp = MagicMock(content=_zip_with_mtlx_and_color())
    mtlx_dir = tmp_path / "mtlx"
    output_dir = tmp_path / "out"
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(_entry(), "1k", output_dir, mtlx_dir=mtlx_dir)

    assert rec.status == "ok"
    pbr = rec.mat_vis.pbr
    assert pbr.metalness == 0.0
    assert pbr.roughness == 0.6
    assert pbr.ior == 1.45
    assert pbr.specular_intensity == 0.8
    assert pbr.specular_color == [0.95, 0.92, 0.90]
    assert pbr.clearcoat_roughness == 0.12
    assert pbr.dispersion == 0.02
    # base * base_color
    assert pbr.color_rgb == [0.55, 0.35, 0.25]


def test_fetch_one_glass_transmission_and_thickness(tmp_path: Path) -> None:
    """Transmission>0 material picks up transmission AND thickness."""
    mock_resp = MagicMock(content=_zip_with_mtlx_and_color(_FAKE_GLASS_MTLX))
    mtlx_dir = tmp_path / "mtlx"
    output_dir = tmp_path / "out"
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(_entry(), "1k", output_dir, mtlx_dir=mtlx_dir)

    assert rec.status == "ok"
    assert rec.mat_vis.pbr.transmission == 1.0
    # thickness is gated on transmission > 0 — see _mtlx_scalars
    assert rec.mat_vis.pbr.thickness == 0.01


# ── missing-MTLX is graceful ────────────────────────────────────


def test_fetch_one_no_mtlx_no_exception(tmp_path: Path) -> None:
    """Old-format ZIPs without .mtlx still produce a usable record."""
    mock_resp = MagicMock(content=_zip_without_mtlx())
    mtlx_dir = tmp_path / "mtlx"
    output_dir = tmp_path / "out"
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(_entry(), "1k", output_dir, mtlx_dir=mtlx_dir)

    assert rec.status == "ok"
    # Phase-2 fields stay None (parser never ran)
    assert rec.mat_vis.pbr.clearcoat_roughness is None
    assert rec.mat_vis.pbr.specular_intensity is None
    # color texture is bound → convention fills [1, 1, 1]
    assert rec.mat_vis.pbr.color_rgb == [1.0, 1.0, 1.0]


def test_fetch_one_mtlx_dir_none_is_graceful(tmp_path: Path) -> None:
    """Caller can pass mtlx_dir=None — fetcher must not attempt parse."""
    mock_resp = MagicMock(content=_zip_with_mtlx_and_color())
    output_dir = tmp_path / "out"
    with patch("mat_vis_baker.sources.ambientcg.retry_request", return_value=mock_resp):
        rec = _fetch_one(_entry(), "1k", output_dir, mtlx_dir=None)

    assert rec.status == "ok"
    # Parser not invoked → Phase-2 fields stay None
    assert rec.mat_vis.pbr.clearcoat_roughness is None
    assert rec.mat_vis.pbr.specular_intensity is None


# ── merge-rule regression ───────────────────────────────────────


def test_merge_helper_does_not_override_upstream(tmp_path: Path) -> None:
    """The additive-merge helper preserves upstream-authored values.

    Regression guard for the issue's merge-order rule: MTLX is additive
    for fields the upstream JSON couldn't carry. We test the helper
    directly because no ambientcg upstream path authors base PBR today
    — but a future contributor wiring upstream JSON PBR into ambientcg
    must not be able to silently regress the rule.
    """
    upstream = PBRBlock(
        color_rgb=[0.1, 0.2, 0.3],  # JSON-authored
        roughness=0.9,
        metalness=0.0,
        ior=1.4,
    )
    parsed = PBRBlock(
        color_rgb=[0.5, 0.5, 0.5],  # MTLX disagrees — must NOT override
        roughness=0.6,
        metalness=1.0,
        ior=1.5,
        clearcoat_roughness=0.1,
        specular_intensity=0.8,
    )
    merge_mtlx_pbr_additive(upstream, parsed)

    # Upstream wins for fields it authored
    assert upstream.color_rgb == [0.1, 0.2, 0.3]
    assert upstream.roughness == 0.9
    assert upstream.metalness == 0.0
    assert upstream.ior == 1.4
    # MTLX fills the Phase-2 fields the JSON couldn't carry
    assert upstream.clearcoat_roughness == 0.1
    assert upstream.specular_intensity == 0.8


def test_merge_helper_fills_none_gaps() -> None:
    """When upstream is None, MTLX values flow through."""
    upstream = PBRBlock()
    parsed = PBRBlock(
        color_rgb=[0.5, 0.4, 0.3],
        roughness=0.7,
        clearcoat_roughness=0.15,
    )
    merge_mtlx_pbr_additive(upstream, parsed)
    assert upstream.color_rgb == [0.5, 0.4, 0.3]
    assert upstream.roughness == 0.7
    assert upstream.clearcoat_roughness == 0.15
