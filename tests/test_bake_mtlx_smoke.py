"""MaterialX baker smoke test — exercises the real ``TextureBaker`` API path.

This is the ONLY test that drives :func:`mat_vis_baker.bake._bake_mtlx`
end-to-end. It exists because the MaterialX bake path has repeatedly broken at
the *API boundary* — ``TextureBaker.create()`` gained a required ``BaseType``
third arg (#442/#443), and ``BaseType`` moved to the ``PyMaterialXRender``
submodule (#444/#445). Every one of those regressions was caught only at CI
*bake* runtime, one round-trip at a time, because a green ``pytest`` says
nothing about calls that no test makes. This closes that gap.

Two skip tiers keep it honest without penalising the slim path:

* **``requires_mtlx``** — MaterialX + its render submodules must import. In the
  slim CI image (no ``[materialx]`` extra) every test here skips cleanly, so the
  default ``test-all`` job is unaffected. The API-contract test (below) runs
  whenever MaterialX imports — no GPU/display needed.
* **``requires_gl``** — the actual bake needs a live GLX/X11 context.
  ``TextureBaker`` calls ``glXChooseVisual``/``XOpenDisplay``; EGL/SwiftShader
  alone is insufficient (see ``/falsify`` on #438). The heavy Dagger image must
  run ``Xvfb`` (``DISPLAY=:99``) first. Absent a display we skip the render
  tests rather than fail flakily.

Wired into CI via the ``test-materialx`` Dagger function (see
``.dagger/src/mat_vis_ci/main.py``) and the ``materialx-test`` job in
``ci.yml``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from mat_vis_baker.common import MaterialRecord


def _materialx_importable() -> bool:
    try:
        import MaterialX  # noqa: F401
        from MaterialX import PyMaterialXRender  # noqa: F401
        from MaterialX import PyMaterialXRenderGlsl  # noqa: F401
    except Exception:
        return False
    return True


MATERIALX = _materialx_importable()
HAS_DISPLAY = bool(os.environ.get("DISPLAY"))

requires_mtlx = pytest.mark.skipif(
    not MATERIALX,
    reason="MaterialX not installed — heavy [materialx] image / Dagger test-materialx only",
)
requires_gl = pytest.mark.skipif(
    not HAS_DISPLAY,
    reason="no DISPLAY — TextureBaker needs a live GLX context (Xvfb :99)",
)


# ── self-contained material fixture ────────────────────────────────────

# A minimal, hermetic gpuopen-shaped MaterialX document. Mirrors the
# production shape (a nodegraph feeding a standard_surface exposed via
# <surfacematerial>) but depends on nothing outside its own directory:
# base_color reads a PNG we generate at fixture time, roughness is a
# procedural left-right ramp. Constant-only inputs can be folded to
# uniforms and skipped by the baker, so at least one input is a real
# texture (base_color) to guarantee TextureBaker emits a PNG.
_SMOKE_MTLX = """<?xml version="1.0" encoding="utf-8"?>
<materialx version="1.38">
  <nodegraph name="NG_smoke">
    <image name="base_img" type="color3">
      <input name="file" type="filename" value="smoke_color.png" />
    </image>
    <ramplr name="rough_ramp" type="float">
      <input name="valuel" type="float" value="0.1" />
      <input name="valuer" type="float" value="0.9" />
    </ramplr>
    <output name="color_out" type="color3" nodename="base_img" />
    <output name="rough_out" type="float" nodename="rough_ramp" />
  </nodegraph>
  <standard_surface name="SR_smoke" type="surfaceshader">
    <input name="base_color" type="color3" output="color_out" nodegraph="NG_smoke" />
    <input name="specular_roughness" type="float" output="rough_out" nodegraph="NG_smoke" />
  </standard_surface>
  <surfacematerial name="Smoke_Material" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="SR_smoke" />
  </surfacematerial>
</materialx>
"""


@pytest.fixture
def smoke_mtlx(tmp_path: Path) -> Path:
    """Write the hermetic smoke material + its backing PNG to a tmpdir.

    Returns the path to the ``.mtlx`` file. ``_bake_mtlx`` appends the
    file's parent to the MaterialX search path, so the relative
    ``smoke_color.png`` reference resolves.
    """
    mat_dir = tmp_path / "smoke_material"
    mat_dir.mkdir()
    # A small non-uniform gradient so the baked output is a genuine texture.
    img = Image.new("RGB", (8, 8))
    img.putdata([(x * 32, y * 32, 128) for y in range(8) for x in range(8)])
    img.save(mat_dir / "smoke_color.png", "PNG")

    mtlx_path = mat_dir / "smoke.mtlx"
    mtlx_path.write_text(_SMOKE_MTLX)
    return mtlx_path


# ── API-contract tests (no GL context needed) ──────────────────────────


@requires_mtlx
def test_basetype_lives_in_render_submodule() -> None:
    """Guards #444/#445: ``bake.py`` imports ``BaseType`` from
    ``PyMaterialXRender``. If a MaterialX release ever moves it, this fails
    at pytest time instead of at CI bake time."""
    from MaterialX import PyMaterialXRender as mx_base_render

    assert hasattr(mx_base_render, "BaseType"), (
        "BaseType missing from PyMaterialXRender — bake.py import site is stale"
    )
    assert hasattr(mx_base_render.BaseType, "UINT8"), (
        "BaseType.UINT8 missing — bake.py passes it as the TextureBaker LDR base type"
    )


@requires_mtlx
def test_texturebaker_create_rejects_two_arg_call() -> None:
    """Guards #442/#443: ``TextureBaker.create()`` requires a third
    ``BaseType`` arg. The pre-fix two-arg call must not be a valid overload
    — that regression shipped twice and was only caught at bake runtime.

    This check is display-free: pybind resolves the (missing) overload and
    raises ``TypeError`` *before* the C++ body runs, so no GLX context is
    touched. The valid three-arg construction opens a display and is
    exercised by ``test_bake_mtlx_produces_flat_pngs`` under ``requires_gl``.
    """
    from MaterialX import PyMaterialXRenderGlsl as mx_render

    with pytest.raises(TypeError):
        mx_render.TextureBaker.create(64, 64)


# ── end-to-end bake (needs Xvfb / GLX) ─────────────────────────────────


@requires_mtlx
@requires_gl
def test_bake_mtlx_produces_flat_pngs(smoke_mtlx: Path) -> None:
    """The whole ``_bake_mtlx`` path: load doc + stdlib, create the baker
    with the correct signature, bake all materials, map outputs to canonical
    channels. Asserts real PNG bytes land on disk."""
    from mat_vis_baker.bake import _bake_mtlx

    out_dir = smoke_mtlx.parent / "baked"
    baked = _bake_mtlx(smoke_mtlx, out_dir, resolution_px=64)

    assert baked, "TextureBaker produced no channels — bake API path is broken"
    # base_color -> 'color' via bake.py's channel_map.
    assert "color" in baked, f"expected a color channel, got {sorted(baked)}"
    for channel, path in baked.items():
        assert path.exists(), f"{channel}: baked path missing"
        assert path.stat().st_size > 0, f"{channel}: empty PNG"
        with Image.open(path) as im:
            assert im.format == "PNG", f"{channel}: not a PNG ({im.format})"


@requires_mtlx
@requires_gl
def test_bake_material_routes_mtlx_records(smoke_mtlx: Path, tmp_path: Path) -> None:
    """Higher-level guard: a ``needs_mtlx_bake`` record flows through
    ``bake_material`` — the branch that swallows ``ImportError``/
    ``NotImplementedError`` and flips ``status='failed'``. A green result
    here proves the record wiring (``maps`` populated, status preserved),
    not just the low-level bake."""
    from mat_vis_baker.bake import bake_material

    record = MaterialRecord(
        id="Smoke_Material",
        source="gpuopen",
        needs_mtlx_bake=True,
        texture_paths={"_mtlx": smoke_mtlx},
    )
    out = bake_material(record, tmp_path / "out", tier="1k")

    assert out.status != "failed", "mtlx bake failed through bake_material()"
    assert "color" in out.maps, f"expected color in maps, got {out.maps}"
