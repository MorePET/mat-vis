"""Pytest fixtures for the layered failure-isolation suite (mat-vis#361).

Suite goal: when a thumb render looks wrong, identify *which layer* of
the bake stack introduced the divergence by progressively assembling
the pipeline. See ``README.md`` and ``test_layered_isolation.py``.

Fixtures provided here:

- ``mat_vis_client`` — ``MatVisClient(repo='gerchowl/mat-vis-tst',
  tag='v2026.04.99-tst-full-369')`` with a tmp cache. Uses the
  ``repo=`` / ``tag=`` constructor kwargs landed in #391.
- ``playwright_browser`` — module-scoped headless Chromium with the
  same ``--use-gl=swiftshader`` flag the prod orchestrator uses
  (``bake/preview/run.py``). Reusing one browser across all five
  layer renders cuts wall-time ~40%.
- ``file_server`` — module-scoped local HTTP server that hosts
  ``thumb_render.html`` + ``shader_ball.glb`` + per-test spec JSONs.
  One server, one tmp dir for the whole module run.
- ``layered_tmpdir`` — module-scoped tmp dir served by ``file_server``;
  spec-json writers in ``render_helpers.py`` drop their files here.
- ``output_dir`` — output root for diff images / per-pair RMS, sibling
  of this conftest. Tests parametrize subpaths off it.

Gating: the full suite is skipped unless ``MAT_VIS_VISUAL=1`` is set,
matching the existing pymat convention for Playwright-bound visual
suites — keeps default CI fast and avoids surprise browser downloads.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PREVIEW_DIR = REPO_ROOT / "bake" / "preview"
THUMB_HTML = PREVIEW_DIR / "thumb_render.html"
SHADER_BALL = PREVIEW_DIR / "assets" / "shader_ball.glb"
OUTPUT_DIR = Path(__file__).parent / "output"

# Test substrate (CalVer test release, full 4-source coverage). The
# new constructor kwargs from #391 route here without env munging,
# but env vars still work for a one-off ``MAT_VIS_HF_BASE=...
# uv run pytest`` override.
TST_REPO = "gerchowl/mat-vis-tst"
TST_TAG = "v2026.04.99-tst-full-369"

# Default-skip unless MAT_VIS_VISUAL=1. Visual suite needs Playwright +
# a network hit on HF + a Chromium download. CI default stays fast.
SKIP_VISUAL = os.environ.get("MAT_VIS_VISUAL", "0") != "1"
SKIP_REASON = "MAT_VIS_VISUAL=1 not set (Playwright + HF substrate visual suite)"


def pytest_collection_modifyitems(config, items):
    """Apply the ``MAT_VIS_VISUAL`` gate at collection time.

    Marks every item in this directory as skipped when the env var is
    off. Done here (rather than per-class ``@pytest.mark.skipif``)
    because the suite is small and the gate is total — saves a marker
    on every new test added later.
    """
    skip_marker = pytest.mark.skip(reason=SKIP_REASON)
    for item in items:
        # Only gate items defined under this layered/ directory.
        if Path(item.fspath).is_relative_to(Path(__file__).parent):
            if SKIP_VISUAL:
                item.add_marker(skip_marker)


def _free_port() -> int:
    """Bind to port 0 to claim a free ephemeral port, then release.

    Avoids hard-coding 8773-style ports that collide with the
    orchestrator (``bake/preview/run.py``) when both run on the same
    box during dev.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def layered_tmpdir():
    """Module-scoped tmp dir used by both ``file_server`` and the
    spec-writer helpers in ``render_helpers``. One dir for the whole
    module so the spec JSONs and the renderer asset live under the
    same HTTP root."""
    with tempfile.TemporaryDirectory(prefix="mat-vis-layered-") as tmp:
        tmpdir = Path(tmp)
        # Stage the renderer + GLB so ``http://server/thumb_render.html``
        # and ``http://server/shader_ball.glb`` resolve.
        shutil.copy(THUMB_HTML, tmpdir / "thumb_render.html")
        shutil.copy(SHADER_BALL, tmpdir / "shader_ball.glb")
        yield tmpdir


@pytest.fixture(scope="module")
def file_server(layered_tmpdir):
    """Serve ``layered_tmpdir`` over HTTP for the renderer to fetch
    spec JSONs + the shader-ball GLB. Module-scoped: one server per
    pytest module run."""

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(layered_tmpdir), **kwargs)

        def log_message(self, *args):
            # Silence default request-log noise; pytest is already verbose.
            pass

    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def playwright_browser():
    """Headless Chromium with SwiftShader software GL, matching the
    flag set used by the prod orchestrator (``run.py``).

    Module-scoped so all 5 layer renders for all parametrized
    materials share one browser. ``new_page()`` per render keeps tabs
    isolated.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover — env-gated
        pytest.skip(
            "playwright not installed (install: uv pip install playwright && playwright install chromium)"
        )

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--use-gl=swiftshader"])
    try:
        yield browser
    finally:
        browser.close()
        pw.stop()


@pytest.fixture
def mat_vis_client(tmp_path):
    """Fresh ``MatVisClient`` pointed at the test substrate, with a
    function-scoped tmp cache.

    Per-test cache scope is deliberate: the cache invalidates between
    tests so a layer's behavior never depends on a previous test's
    fetches. ``~/.cache/mat-vis`` is also untouched.

    Uses the ``repo=`` / ``tag=`` kwargs from #391 — no env munging.
    """
    from mat_vis_client import MatVisClient

    return MatVisClient(repo=TST_REPO, tag=TST_TAG, cache_dir=tmp_path)


@pytest.fixture(scope="session", autouse=True)
def _ensure_output_dir():
    """Create the per-suite output dir once per session so render
    helpers can drop diff images + RMS reports without each test
    needing to mkdir."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    yield
