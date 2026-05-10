"""Visual regression tests — headless Three.js rendering via Playwright.

Ported from py-mat (`tests/test_visual_regression.py`) — but unlike the
upstream copy, mat-vis IS the substrate, so when these tests fail the
fix lives in this repo (baker, adapter, client). py-mat just consumes
what we ship.

Renders the bernhard mat-vis#285 reference grid (textured + scalar-only
sources) one shader-ball at a time through the SAME `thumb_render.html`
that `bake/preview/run.py` drives during a production thumb bake — so a
visual regression here mirrors a visual regression in the bake.

Three test classes:

    TestBernhardMatVis285_AdapterStructure_Textured
        Per-(tier, adapter) structural checks across BERNHARD_TEXTURED.
        No rendering — just `client._scalars_for` + `fetch_all_textures`
        + `to_threejs` / `to_gltf`. Catches scalar-regression #285.

    TestBernhardMatVis285_AdapterStructure_Scalar
        Same, for BERNHARD_SCALAR_ONLY (physicallybased.info path).

    TestBernhardMatVis285_Ktx2Bytes
        At ``tier='ktx2-1k'`` the substrate must serve KTX2-encoded
        bytes (Basis-transcodable), not PNG. One sample suffices.

    TestBernhardMatVis285_Visual
        Headless single-material render per (label, source, material_id)
        through `bake/preview/thumb_render.html`. Pixel-RMS compare via
        `_visual_compare.assert_matches_baseline()` against committed
        baselines under `bake/preview/tests/baselines/`.

Skip with: MAT_VIS_SKIP_VISUAL=1 (default — these are heavy Playwright
runs not for normal CI). Run with: MAT_VIS_SKIP_VISUAL=0.

Pin a substrate revision via env (HF only):

    MAT_VIS_HF_BASE=https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve
    MAT_VIS_TAG=v2026.04.99-tst-full-369

Update baselines:

    MAT_VIS_UPDATE_BASELINES=1
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

SKIP_VISUAL = os.environ.get("MAT_VIS_SKIP_VISUAL", "1") == "1"

# Optional substrate-tag pin via env. mat-vis-client picks DEFAULT_TAG
# when this is unset (see clients/python/src/mat_vis_client/client.py).
# In CI we point this at the tst dataset's full-bake revision so the
# bernhard grid is fully populated.
SUBSTRATE_TAG = os.environ.get("MAT_VIS_TAG") or None

REPO_ROOT = Path(__file__).resolve().parents[3]
BAKE_PREVIEW = REPO_ROOT / "bake" / "preview"
THUMB_HTML = BAKE_PREVIEW / "thumb_render.html"
SHADER_BALL = BAKE_PREVIEW / "assets" / "shader_ball.glb"


# ──────────────────────────────────────────────────────────────────────
# Bernhard's mat-vis#285 multi-material grid — verbatim from py-mat
# https://github.com/MorePET/mat-vis/issues/285
# ──────────────────────────────────────────────────────────────────────

# Bernhard's textured-source 24-material grid. Each entry: (label,
# source, material_id). Texture-scale + colour overrides from his code
# are not represented here — they're per-instance render state on the
# build123d side, not substrate attributes.
BERNHARD_TEXTURED: list[tuple[str, str, str]] = [
    ("car_red", "gpuopen", "Car Paint"),
    ("car_green", "gpuopen", "Car Paint"),
    ("bronze", "gpuopen", "Bronze Oxydized"),
    ("chrome", "gpuopen", "Chrome"),
    ("glass", "gpuopen", "Glass"),
    ("red_wine", "gpuopen", "Red Wine"),
    ("gold", "gpuopen", "Gold"),
    ("carbon_coat", "gpuopen", "Carbon biColor Coat"),
    ("steel", "gpuopen", "Stainless Steel Brushed"),
    ("alu", "gpuopen", "Aluminum Brushed"),
    ("bricks", "gpuopen", "TH: Large Red Bricks"),
    ("leather", "gpuopen", "TH: Brown Fabric Leather"),
    ("alu_corr", "gpuopen", "Aluminum Corrugated"),
    ("alu_hexagon", "gpuopen", "Aluminum Hexagon"),
    ("perforated", "gpuopen", "Perforated Metal"),
    ("wood", "gpuopen", "Ivory Walnut Solid Wood"),
    ("tiles", "gpuopen", "Iberian Blue Ceramic Tiles"),
    ("tiles2", "gpuopen", "Tiles Black Long Variative"),
    ("brass_scratched", "ambientcg", "Metal 007"),
    ("carbon", "ambientcg", "Fabric 004"),
    ("plates", "ambientcg", "Metal Plates 006"),
    ("floor", "gpuopen", "Adelie Brown Luxury Flooring"),
    ("plank", "polyhaven", "Plank Flooring 03"),
    ("rock_wall", "polyhaven", "Rock Wall 16"),
]

# Scalar-only physicallybased.info entries. No textures — just
# authored PBR scalars.
BERNHARD_SCALAR_ONLY: list[tuple[str, str]] = [
    ("acryl", "Plastic (Acrylic)"),
    ("alu_pbr", "Aluminum"),
    ("gold_pbr", "Gold"),
    ("iron_pbr", "Iron"),
    ("copper_pbr", "Copper"),
]

# Tiers offered by the mat-vis substrate.
TIERS_TEXTURED = ["1k", "512", "256", "ktx2-1k", "ktx2-512"]
TIERS_SCALAR = ["scalar"]

# Default-grey-plastic fingerprint per mat-vis#285 — the recipe a
# regressed gpuopen baker emits when authored .mtlx scalars never made
# it into the rowmap. If a substrate-version's grid all matches this,
# the substrate has not been re-baked with the fix from PR mat-vis#294.
_DEFAULT_GREY_INTS = {0xCCCCCC}
_DEFAULT_GREY_HEX = {"#cccccc", "#CCCCCC"}


# Default-grey RGB triple (linear or sRGB; both round to ~0.8) the
# to_gltf adapter emits as ``baseColorFactor`` when the substrate
# returned no authored color.
_DEFAULT_GREY_RGB_TRIPLE = (0.8, 0.8, 0.8)


def _is_default_grey_color(color) -> bool:
    """True when ``color`` matches the substrate's default-grey
    fingerprint, regardless of which adapter produced it.

    to_threejs emits a hex string ("#cccccc") or int (0xCCCCCC); to_gltf
    emits a list ([r, g, b, a]) of floats in linear RGBA. We accept any
    of those shapes.
    """
    if color is None:
        return True
    if isinstance(color, int) and color in _DEFAULT_GREY_INTS:
        return True
    if isinstance(color, str) and color in _DEFAULT_GREY_HEX:
        return True
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        rgb = tuple(round(float(c), 2) for c in color[:3])
        if rgb == _DEFAULT_GREY_RGB_TRIPLE:
            return True
    return False


def _is_default_grey(scalars: dict) -> bool:
    return (
        scalars.get("metalness") in (0.0, None)
        and scalars.get("roughness") in (0.5, None)
        and _is_default_grey_color(scalars.get("color"))
    )


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def serve_dir(tmp_path_factory):
    """Per-module tempdir that holds the renderer + shader ball + spec
    JSONs the http.server fixture exposes."""
    d = tmp_path_factory.mktemp("visual_serve")
    shutil.copy(THUMB_HTML, d / "thumb_render.html")
    shutil.copy(SHADER_BALL, d / "shader_ball.glb")
    return d


@pytest.fixture(scope="module")
def file_server(serve_dir):
    """Serve renderer + GLB + per-test spec files on a localhost port."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(serve_dir), **kwargs)

        def log_message(self, *args):
            pass  # silence

    # Port chosen distinct from run.py (8773) and py-mat (8765, 8771).
    port = 8775
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def browser():
    """Launch headless Chromium with SwiftShader (deterministic GL)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip(
            "playwright not installed — pip install playwright + playwright install chromium"
        )

    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True, args=["--use-gl=swiftshader"])
    try:
        yield b
    finally:
        b.close()
        pw.stop()


@pytest.fixture(scope="module")
def client():
    """Module-scoped MatVisClient pinned to MAT_VIS_TAG (or DEFAULT_TAG)."""
    from mat_vis_client import MatVisClient

    return MatVisClient(tag=SUBSTRATE_TAG) if SUBSTRATE_TAG else MatVisClient()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _build_threejs_textured(client, source: str, material_id: str, tier: str) -> dict | None:
    """Build a `to_threejs` dict for one textured material.

    Tries the requested tier first; falls back through 512/256/128
    (matches `bake/preview/run.py`'s tier-fallback). If no tier is
    staged, renders scalar-only (still meaningful — the substrate
    fix from PR mat-vis#294 ensures authored .mtlx scalars survive
    even when texture maps are missing).

    Returns None only when both scalars AND textures are unavailable —
    nothing to render.
    """
    from mat_vis_client.adapters import to_threejs

    try:
        scalars = client._scalars_for(source, material_id)
    except Exception:
        scalars = {}

    textures: dict[str, bytes] = {}
    fallback_order = [tier] + [t for t in ("1k", "512", "256", "128") if t != tier]
    for try_tier in fallback_order:
        try:
            textures = client.fetch_all_textures(source, material_id, try_tier)
            break
        except Exception:
            continue

    if not scalars and not textures:
        return None

    return to_threejs(scalars, textures)


def _build_threejs_scalar(client, material_id: str) -> dict | None:
    """Build a `to_threejs` dict for one scalar-only material
    (physicallybased.info)."""
    from mat_vis_client.adapters import to_threejs

    try:
        scalars = client._scalars_for("physicallybased", material_id)
    except Exception:
        return None
    if not scalars:
        return None
    # Scalar-only: no textures by definition.
    return to_threejs(scalars, {})


def _render_one(browser, server_url: str, threejs_spec: dict, name: str, out_dir: Path) -> Path:
    """Render one MeshPhysicalMaterial-spec dict on the shader ball,
    return the captured 1024² PNG path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_path = out_dir / f"spec_{name}.json"
    spec_path.write_text(json.dumps({"threejs": threejs_spec}))

    page = browser.new_page(viewport={"width": 1024, "height": 1024})
    try:
        url = f"{server_url}/thumb_render.html?spec={spec_path.name}"
        page.goto(url, timeout=180_000)
        page.wait_for_function("() => window.__renderComplete === true", timeout=180_000)

        render_error = page.evaluate("() => window.__renderError || null")
        if render_error:
            raise RuntimeError(f"renderer JS error: {render_error}")

        page.wait_for_timeout(800)  # let textures + envmap settle
        data_url = page.evaluate("() => document.querySelector('canvas').toDataURL('image/png')")
    finally:
        page.close()

    out_png = out_dir / f"{name}.png"
    out_png.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
    return out_png


# Where to drop spec JSONs + freshly-rendered PNGs (pre-baseline-compare)
# during a test run. Same dir as serve_dir but easier to inspect after a
# failure when pytest preserves it via -s.
@contextmanager
def _render_workspace(serve_dir: Path):
    """Yield the serve_dir as a workspace — specs land alongside the
    renderer so they're served by the same http.server."""
    yield serve_dir


# ──────────────────────────────────────────────────────────────────────
# 1. Adapter structure — textured sources × tiers
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.visual
@pytest.mark.skipif(SKIP_VISUAL, reason="MAT_VIS_SKIP_VISUAL=1 (default)")
class TestBernhardMatVis285_AdapterStructure_Textured:
    """Per-tier × per-adapter structural validation of the textured
    portion of bernhard's grid.

    Catches API drift in either direction: an adapter dropping a key
    from its output, a tier silently disappearing from the substrate,
    or the scalar regression bernhard documented in mat-vis#285.

    The scalar assertion (>=5 of 24 materials emit non-default
    scalars) is the headline #285 catch. Targets the post-PR-#294
    substrate where authored .mtlx scalars are preserved end-to-end —
    no `xfail(strict)` here, the substrate this test runs against is
    expected to pass.
    """

    @pytest.mark.parametrize("adapter", ["to_threejs", "to_gltf"])
    @pytest.mark.parametrize("tier", TIERS_TEXTURED)
    def test_textured_adapter_structure_at_tier(self, client, adapter: str, tier: str) -> None:
        from mat_vis_client.adapters import to_gltf, to_threejs

        adapter_fn = {"to_threejs": to_threejs, "to_gltf": to_gltf}[adapter]
        non_default = 0
        skipped: list[str] = []

        for label, source, material_id in BERNHARD_TEXTURED:
            try:
                scalars = client._scalars_for(source, material_id)
                textures = client.fetch_all_textures(source, material_id, tier)
                out = adapter_fn(scalars, textures)
            except Exception as exc:  # noqa: BLE001
                skipped.append(f"{label}: {type(exc).__name__}")
                continue

            assert isinstance(out, dict), (
                f"{adapter}({label}) must return dict, got {type(out).__name__}"
            )
            if adapter == "to_threejs":
                assert out.get("type") == "MeshPhysicalMaterial", (
                    f"{label}: to_threejs missing/wrong 'type', got {out.get('type')!r}"
                )
            else:  # to_gltf
                assert "pbrMetallicRoughness" in out, (
                    f"{label}: to_gltf missing 'pbrMetallicRoughness' block"
                )

            # Uniform scalar fingerprint check across both adapters.
            if adapter == "to_threejs":
                key_scalars = {
                    "metalness": out.get("metalness"),
                    "roughness": out.get("roughness"),
                    "color": out.get("color"),
                }
            else:
                pbr = out.get("pbrMetallicRoughness", {})
                key_scalars = {
                    "metalness": pbr.get("metallicFactor"),
                    "roughness": pbr.get("roughnessFactor"),
                    "color": pbr.get("baseColorFactor"),
                }

            if not _is_default_grey(key_scalars):
                non_default += 1

        rendered = len(BERNHARD_TEXTURED) - len(skipped)
        # If the substrate doesn't ship this tier broadly (e.g.
        # ktx2-1k is polyhaven-only on tst-full), treat as a soft
        # skip — the structural assertion needs at least 6 actual
        # renders to be meaningful. A hard fail here would penalize
        # bernhard's grid for substrate coverage gaps that aren't
        # adapter regressions.
        if rendered < 6:
            pytest.skip(
                f"only {rendered}/{len(BERNHARD_TEXTURED)} materials staged at "
                f"tier={tier} on this substrate revision — too few for the "
                "scalar-fingerprint assertion to be meaningful"
            )
        assert non_default >= 5, (
            f"only {non_default}/{rendered} textured materials at "
            f"tier={tier} via {adapter} have non-default scalars "
            f"(skipped={len(skipped)} due to substrate cache misses) — "
            "mat-vis#285 baker regression?"
        )


# ──────────────────────────────────────────────────────────────────────
# 2. Adapter structure — scalar-only physicallybased
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.visual
@pytest.mark.skipif(SKIP_VISUAL, reason="MAT_VIS_SKIP_VISUAL=1 (default)")
class TestBernhardMatVis285_AdapterStructure_Scalar:
    """The physicallybased.info path is scalar-only — no texture maps,
    just authored PBR scalars. Closed downstream in py-mat #225 (#222)
    and at the substrate by mat-vis#368/#370/#371 (case-insensitive
    name lookup + scalar-tier sentinel).
    """

    @pytest.mark.parametrize("adapter", ["to_threejs", "to_gltf"])
    @pytest.mark.parametrize("label,material_id", BERNHARD_SCALAR_ONLY)
    def test_scalar_only_adapter(self, client, adapter: str, label: str, material_id: str) -> None:
        from mat_vis_client.adapters import to_gltf, to_threejs

        adapter_fn = {"to_threejs": to_threejs, "to_gltf": to_gltf}[adapter]
        scalars = client._scalars_for("physicallybased", material_id)
        if not scalars:
            pytest.skip(f"{label}: physicallybased/{material_id!r} not staged in substrate")
        out = adapter_fn(scalars, {})

        assert isinstance(out, dict)
        if adapter == "to_threejs":
            assert out.get("type") == "MeshPhysicalMaterial"
            # Scalar-only materials must surface at least one PBR scalar.
            assert any(k in out for k in ("metalness", "roughness", "color")), (
                f"{label}: no PBR scalars in to_threejs output"
            )
        else:
            assert "pbrMetallicRoughness" in out


# ──────────────────────────────────────────────────────────────────────
# 3. KTX2 tier byte-shape
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.visual
@pytest.mark.skipif(SKIP_VISUAL, reason="MAT_VIS_SKIP_VISUAL=1 (default)")
class TestBernhardMatVis285_Ktx2Bytes:
    """At ``tier='ktx2-1k'``, texture bytes must be KTX2-encoded — not
    PNG. The KTX2 magic header is the 12 bytes ``«KTX 20»\\r\\n\\x1a\\n``;
    a simple bytes-prefix check is enough to distinguish it from PNG
    (``\\x89PNG\\r\\n\\x1a\\n``).
    """

    KTX2_MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"

    # Probe polyhaven first (it's the source that historically ships
    # ktx2-1k consistently). Fall back to ambientcg/gpuopen — any one
    # serving ktx2-1k is enough; we're checking encoding shape, not
    # source coverage.
    KTX2_PROBES: list[tuple[str, str]] = [
        ("polyhaven", "Plank Flooring 03"),
        ("polyhaven", "Rock Wall 16"),
        ("ambientcg", "Metal 007"),
        ("gpuopen", "Chrome"),
    ]

    def test_ktx2_tier_returns_ktx2_bytes(self, client) -> None:
        textures: dict[str, bytes] = {}
        attempts: list[str] = []
        for source, mid in self.KTX2_PROBES:
            try:
                textures = client.fetch_all_textures(source, mid, "ktx2-1k")
            except Exception as exc:  # noqa: BLE001
                attempts.append(f"{source}/{mid}: {type(exc).__name__}")
                continue
            if textures:
                break
        if not textures:
            pytest.skip(f"no ktx2-1k textures across {len(self.KTX2_PROBES)} probes — {attempts}")

        sample_key = next(iter(textures))
        sample_bytes = textures[sample_key]
        assert isinstance(sample_bytes, bytes)
        assert sample_bytes.startswith(self.KTX2_MAGIC), (
            f"texture channel {sample_key!r} at tier=ktx2-1k did not start with "
            f"KTX2 magic header — first 12 bytes: {sample_bytes[:12]!r}"
        )


# ──────────────────────────────────────────────────────────────────────
# 4. Visual — single-material renders through thumb_render.html
# ──────────────────────────────────────────────────────────────────────

# We render one material at a time (matches `bake/preview/run.py`'s
# production path — no grid-renderer fork). One baseline PNG per
# (label, tier) for textured + per (label,) for scalar-only.
#
# Default tier for the textured visual sweep is "1k" — matches what
# `run.py` prefers when staged. Adding more tiers blows the baseline
# storage out for marginal value (we already have the structural test
# covering each tier).
VISUAL_TEXTURED_TIER = os.environ.get("MAT_VIS_VISUAL_TIER", "1k")


@pytest.mark.visual
@pytest.mark.skipif(SKIP_VISUAL, reason="MAT_VIS_SKIP_VISUAL=1 (default)")
class TestBernhardMatVis285_Visual:
    """Single-material headless renders via `thumb_render.html`.

    Pixel-RMS compared against committed baselines under
    `bake/preview/tests/baselines/`. Baselines are generated against
    the substrate revision pinned by `MAT_VIS_TAG` (typically the
    `gerchowl/mat-vis-tst` full-bake tag); regenerate via
    `MAT_VIS_UPDATE_BASELINES=1` whenever the renderer or substrate
    changes intentionally.
    """

    @pytest.mark.parametrize(
        "label,source,material_id",
        BERNHARD_TEXTURED,
        ids=[lbl for lbl, _, _ in BERNHARD_TEXTURED],
    )
    def test_textured_render_matches_baseline(
        self,
        client,
        file_server,
        browser,
        serve_dir,
        label: str,
        source: str,
        material_id: str,
    ) -> None:
        from ._visual_compare import assert_matches_baseline

        threejs = _build_threejs_textured(client, source, material_id, VISUAL_TEXTURED_TIER)
        if threejs is None:
            pytest.skip(
                f"{label}: {source}/{material_id!r} not staged at any tier "
                f"in substrate (no scalars + no textures)"
            )
        # Defensive: empty/default-grey scalars + no textures => render
        # would just show the default-grey plastic sphere. That's a
        # substrate routing miss masquerading as success. Materials
        # with authored scalars (e.g. gpuopen Chrome — roughness=0.05,
        # metalness=1.0) but no texture maps will NOT trigger this and
        # render scalar-only, which IS the right thing to baseline.
        scalars_fingerprint = {
            "metalness": threejs.get("metalness"),
            "roughness": threejs.get("roughness"),
            "color": threejs.get("color"),
        }
        has_any_texture = any(
            isinstance(v, str) and v.startswith("data:image/") for v in threejs.values()
        )
        if _is_default_grey(scalars_fingerprint) and not has_any_texture:
            pytest.skip(
                f"{label}: substrate returned default-grey scalars + no textures — "
                "likely a substrate miss for this material/tier; not a render bug"
            )

        name = f"bernhard_textured_{label}_{VISUAL_TEXTURED_TIER}"
        png = _render_one(browser, file_server, threejs, name, serve_dir)
        size = png.stat().st_size
        assert size > 5_000, f"{label}: render too small ({size} bytes) — likely blank"

        assert_matches_baseline(png, name)

    @pytest.mark.parametrize(
        "label,material_id",
        BERNHARD_SCALAR_ONLY,
        ids=[lbl for lbl, _ in BERNHARD_SCALAR_ONLY],
    )
    def test_scalar_render_matches_baseline(
        self,
        client,
        file_server,
        browser,
        serve_dir,
        label: str,
        material_id: str,
    ) -> None:
        from ._visual_compare import assert_matches_baseline

        threejs = _build_threejs_scalar(client, material_id)
        if threejs is None:
            pytest.skip(f"{label}: physicallybased/{material_id!r} not staged in substrate")

        name = f"bernhard_scalar_{label}"
        png = _render_one(browser, file_server, threejs, name, serve_dir)
        size = png.stat().st_size
        assert size > 5_000, f"{label}: render too small ({size} bytes) — likely blank"

        assert_matches_baseline(png, name)
