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


# ── glTF-MR neutral-multiplier convention (baker-side, #290 follow-up) ──
#
# When a PBRBlock scalar field is left None by the parser (signaling
# "this input is texture-bound") AND the matching texture actually
# shipped, the baker writes the glTF-MR neutral multiplier into the
# substrate so every consumer (adapters in any language, plus search-
# side `pbr.metalness` filters) inherits the convention for free.
#
# Note on threshold: the OLD adapter helper gated the color convention
# on ``metalness >= 0.9``. At the baker we just check "color_rgb is
# None AND a color texture is bound" — color=[1,1,1] is the right
# multiplier for ANY texture-controlled-only color (metallic AND
# dielectric, since glTF-MR multiplies the same way regardless of
# metalness). The metallic/dielectric distinction is implicit in the
# PBRBlock's own ``metalness`` value; the convention is independent.


_MTLX_BASE_COLOR_TEXTURE_BOUND = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng">
    <image name="bc_img" type="color3">
      <input name="file" type="filename" value="oak_basecolor.png"/>
    </image>
    <output name="bc_out" type="color3" nodename="bc_img"/>
  </nodegraph>
  <standard_surface name="oak_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" nodegraph="ng" output="bc_out"/>
    <input name="metalness" type="float" value="1.0"/>
    <input name="specular_roughness" type="float" value="0.5"/>
  </standard_surface>
  <surfacematerial name="oak" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="oak_shader"/>
  </surfacematerial>
</materialx>
"""


_MTLX_METALNESS_TEXTURE_BOUND = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng">
    <image name="m_img" type="float">
      <input name="file" type="filename" value="oak_metalness.png"/>
    </image>
    <output name="m_out" type="float" nodename="m_img"/>
  </nodegraph>
  <standard_surface name="oak_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" value="0.5, 0.5, 0.5"/>
    <input name="metalness" type="float" nodegraph="ng" output="m_out"/>
    <input name="specular_roughness" type="float" value="0.5"/>
  </standard_surface>
  <surfacematerial name="oak" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="oak_shader"/>
  </surfacematerial>
</materialx>
"""


_MTLX_AUTHORED_COLOR_AND_METAL = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="oak_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" value="0.89, 0.89, 0.89"/>
    <input name="metalness" type="float" value="0.5"/>
    <input name="specular_roughness" type="float" value="0.5"/>
  </standard_surface>
  <surfacematerial name="oak" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="oak_shader"/>
  </surfacematerial>
</materialx>
"""


_MTLX_TEXTURE_BOUND_NO_TEXTURE = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng">
    <image name="bc_img" type="color3">
      <input name="file" type="filename" value="oak_basecolor.png"/>
    </image>
    <output name="bc_out" type="color3" nodename="bc_img"/>
    <image name="m_img" type="float">
      <input name="file" type="filename" value="oak_metalness.png"/>
    </image>
    <output name="m_out" type="float" nodename="m_img"/>
  </nodegraph>
  <standard_surface name="oak_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" nodegraph="ng" output="bc_out"/>
    <input name="metalness" type="float" nodegraph="ng" output="m_out"/>
    <input name="specular_roughness" type="float" value="0.5"/>
  </standard_surface>
  <surfacematerial name="oak" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="oak_shader"/>
  </surfacematerial>
</materialx>
"""


def _zip_with(mtlx_bytes: bytes, *texture_basenames: str) -> bytes:
    """Build a ZIP carrying the given mtlx + texture stubs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("oak/material.mtlx", mtlx_bytes)
        for tex in texture_basenames:
            zf.writestr(f"oak/{tex}", b"\x89PNG\r\n\x1a\nfake")
    return buf.getvalue()


def test_metallic_colormap_neutralizes_color_when_texture_bound(tmp_path: Path) -> None:
    """color_rgb is None (texture-bound) + color texture in baked records → [1,1,1]."""
    mock_resp = MagicMock(content=_zip_with(_MTLX_BASE_COLOR_TEXTURE_BOUND, "oak_basecolor.png"))
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "ok"
    assert "color" in rec.maps
    assert rec.mat_vis.pbr.color_rgb == [1.0, 1.0, 1.0]


def test_metalness_neutralized_when_metalnessMap_bound(tmp_path: Path) -> None:
    """metalness is None (texture-bound) + metalness texture shipped → 1.0."""
    mock_resp = MagicMock(content=_zip_with(_MTLX_METALNESS_TEXTURE_BOUND, "oak_metalness.png"))
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "ok"
    assert "metalness" in rec.maps
    assert rec.mat_vis.pbr.metalness == 1.0


def test_authored_color_preserved_with_colormap(tmp_path: Path) -> None:
    """Authored base_color + a color texture → authored value, NOT overridden."""
    mock_resp = MagicMock(
        content=_zip_with(_MTLX_AUTHORED_COLOR_AND_METAL, "oak_basecolor.png"),
    )
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    assert rec.mat_vis.pbr.color_rgb == [0.89, 0.89, 0.89]


def test_authored_metalness_preserved_with_metalnessMap(tmp_path: Path) -> None:
    """Authored metalness + a metalness texture → authored value, NOT overridden.

    No metalness texture is needed for this test — just confirm a parsed
    scalar isn't clobbered. We synthesize a ZIP with a basecolor (so the
    fetch path is "ok") but no metalness texture; the convention stays
    inert because parsed_pbr.metalness is already 0.5.
    """
    mock_resp = MagicMock(
        content=_zip_with(_MTLX_AUTHORED_COLOR_AND_METAL, "oak_basecolor.png"),
    )
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    assert rec.mat_vis.pbr.metalness == 0.5


def test_no_texture_no_convention(tmp_path: Path) -> None:
    """Texture-bound (parser → None) + NO matching texture → field stays None.

    The convention must NOT fire when the texture is missing: writing
    [1,1,1] / 1.0 with no map present would lie about the substrate.
    """
    # mtlx says both base_color and metalness are texture-bound, but
    # the ZIP ships NO image files → textures dict is empty.
    mock_resp = MagicMock(content=_zip_with(_MTLX_TEXTURE_BOUND_NO_TEXTURE))
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    # No textures shipped → fall through to the failed-fetch shorthand
    # because _extract_from_zip returns no textures + an mtlx, the code
    # takes the `needs_mtlx_bake` branch which DOES build a record.
    assert rec.mat_vis.pbr.color_rgb is None
    assert rec.mat_vis.pbr.metalness is None


def test_dielectric_metallic_threshold(tmp_path: Path) -> None:
    """No threshold at the baker — convention fires regardless of metalness.

    The OLD adapter logic gated the color convention on ``metalness
    >= 0.9``. At the baker we just check "color_rgb is None AND a
    color texture is bound" because color=[1,1,1] is the right
    multiplier for any texture-controlled-only color: glTF-MR
    multiplies baseColorFactor × baseColorTexture identically for
    metallic and dielectric materials, so a default-grey scalar would
    double-tint the texture in EITHER case.

    This dielectric (metalness=0.0) material with a texture-bound
    color and a shipped colorMap therefore still gets color_rgb
    neutralized to [1,1,1].
    """
    dielectric = _MTLX_BASE_COLOR_TEXTURE_BOUND.replace(
        b'<input name="metalness" type="float" value="1.0"/>',
        b'<input name="metalness" type="float" value="0.0"/>',
    )
    assert b'value="0.0"' in dielectric, "fixture rewrite failed"

    mock_resp = MagicMock(content=_zip_with(dielectric, "oak_basecolor.png"))
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    assert rec.mat_vis.pbr.metalness == 0.0  # parser preserved authored value
    assert rec.mat_vis.pbr.color_rgb == [1.0, 1.0, 1.0]  # convention fired anyway


# ── roughness symmetry (extends the convention to the third PBR slot) ──
#
# The original baker-side change covered color + metalness only. The
# helper ``apply_pbr_neutral_multiplier_conventions`` now extends it to
# roughness for symmetry: glTF-MR's ``roughnessFactor`` defaults to 1.0,
# and the substrate index needs the explicit value for query
# correctness — ``client.search()`` over ``pbr.roughness`` should match
# materials with a bound roughnessMap.


_MTLX_ROUGHNESS_TEXTURE_BOUND = b"""<?xml version="1.0"?>
<materialx version="1.38">
  <nodegraph name="ng">
    <image name="r_img" type="float">
      <input name="file" type="filename" value="oak_roughness.png"/>
    </image>
    <output name="r_out" type="float" nodename="r_img"/>
  </nodegraph>
  <standard_surface name="oak_shader" type="surfaceshader">
    <input name="base" type="float" value="1.0"/>
    <input name="base_color" type="color3" value="0.5, 0.5, 0.5"/>
    <input name="metalness" type="float" value="0.0"/>
    <input name="specular_roughness" type="float" nodegraph="ng" output="r_out"/>
  </standard_surface>
  <surfacematerial name="oak" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="oak_shader"/>
  </surfacematerial>
</materialx>
"""


def test_roughness_neutralized_when_roughnessMap_bound(tmp_path: Path) -> None:
    """roughness is None (texture-bound) + roughness texture shipped → 1.0."""
    mock_resp = MagicMock(
        content=_zip_with(_MTLX_ROUGHNESS_TEXTURE_BOUND, "oak_roughness.png"),
    )
    with patch("mat_vis_baker.sources.gpuopen.retry_request", return_value=mock_resp):
        rec = _fetch_one(_mat(), "1k", tmp_path, mtlx_dir=None)

    assert rec.status == "ok"
    assert "roughness" in rec.maps
    assert rec.mat_vis.pbr.roughness == 1.0
