"""End-to-end test for the gpuopen `_fetch_one` mtlx-scalar wiring (#290).

The pre-fix baker constructed ``MatVisBlock`` without populating
``pbr=...``. The fix parses ``<standard_surface>`` scalars from the
.mtlx at fetch time. This test mocks ``retry_request`` so no network
is touched and asserts the PBRBlock fields end up populated on the
returned ``MaterialRecord``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from mat_vis_baker.sources.gpuopen import _fetch_one


_FAKE_MTLX = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="oak_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" value="0.45, 0.30, 0.20"/>
    <input name="metalness" type="float" value="0.0"/>
    <input name="specular_roughness" type="float" value="0.7"/>
    <input name="specular_IOR" type="float" value="1.5"/>
  </standard_surface>
  <surfacematerial name="oak" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="oak_shader"/>
  </surfacematerial>
</materialx>
"""


def _zip_with_mtlx_and_textures() -> bytes:
    """ZIP carrying the synthetic mtlx + a basecolor PNG."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("oak/material.mtlx", _FAKE_MTLX)
        zf.writestr("oak/oak_basecolor.png", b"\x89PNG\r\n\x1a\nfake")
    return buf.getvalue()


def _zip_with_mtlx_only() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("oak/material.mtlx", _FAKE_MTLX)
    return buf.getvalue()


def _mat() -> dict:
    return {
        "id": "oak-uuid",
        "title": "Oak Planks",
        "description": "Warm oak.",
        "author": "AMD",
        "license": "MIT Public Domain",
        "published_date": "2022-08-01T12:00:00Z",
        "updated_date": "2023-03-15T09:30:00Z",
        "_category_title": "Wood",
        "_tag_titles": ["wood"],
        "_packages_detail": [
            {
                "id": "pkg-1k-8b",
                "label": "1k 8b",
                "file_url": "https://example.com/oak_1k_8b.zip",
            }
        ],
    }


def test_fetch_one_populates_pbr_from_mtlx(tmp_path: Path) -> None:
    """The success path parses the .mtlx and PBRBlock fields are set."""
    mock_resp = MagicMock(content=_zip_with_mtlx_and_textures())
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "ok"
    pbr = rec.mat_vis.pbr
    assert pbr.metalness == 0.0
    assert pbr.roughness == 0.7
    assert pbr.ior == 1.5
    assert pbr.color_rgb == [0.45, 0.30, 0.20]


def test_fetch_one_pbr_populated_when_mtlx_only(tmp_path: Path) -> None:
    """No flat textures (needs_mtlx_bake path) still parses scalars."""
    mock_resp = MagicMock(content=_zip_with_mtlx_only())
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    assert rec.needs_mtlx_bake is True
    assert rec.mat_vis.pbr.roughness == 0.7
    assert rec.mat_vis.pbr.color_rgb == [0.45, 0.30, 0.20]


def test_fetch_one_failed_path_keeps_empty_pbr(tmp_path: Path) -> None:
    """Failed fetches keep the dataclass-default empty PBRBlock."""
    # Make retry_request raise → exception path → failed() shorthand.
    with patch(
        "mat_vis_baker.sources.gpuopen.retry_request",
        side_effect=RuntimeError("boom"),
    ):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "failed"
    pbr = rec.mat_vis.pbr
    assert pbr.metalness is None
    assert pbr.roughness is None
    assert pbr.ior is None
    assert pbr.color_rgb is None
