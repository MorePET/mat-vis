"""Bake the "blank" reference thumbs used for CI failure detection.

Produces two PNGs under bake/preview/assets/:

- blank_default.png       — Three.js MeshPhysicalMaterial with no params
                            (white color, metalness=0, roughness=1).
                            Catches: the renderer ran with no material
                            spec — a logic error in the orchestrator
                            that produced an empty `to_threejs()` payload.

- blank_default_grey.png  — pymat _PBR_DEFAULTS fingerprint
                            (color=#CCCCCC, metalness=0.0, roughness=0.5,
                            ior=1.5). Catches: pymat defaults leaked
                            through because the substrate catalog was
                            stale (mat-vis#285 fingerprint — bernhard's
                            original repro). If a baked thumb matches
                            this, the substrate's mat_vis.pbr block was
                            empty for that material and pymat's render
                            floor smashed through.

Run once. PNGs are committed as test fixtures + CI gate inputs.

Usage:
    uv run python bake/preview/utils/bake_blanks.py
    # → writes bake/preview/assets/blank_default.png
    # → writes bake/preview/assets/blank_default_grey.png

Requires Playwright (install via `uv pip install playwright tomlkit pillow`
+ `python -m playwright install chromium`).
"""

from __future__ import annotations

import base64
import http.server
import io
import json
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[3]
ASSETS = REPO_ROOT / "bake" / "preview" / "assets"
THUMB_HTML = REPO_ROOT / "bake" / "preview" / "thumb_render.html"


@contextmanager
def _file_server(serve_dir: Path, port: int = 8772):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(serve_dir), **kwargs)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


# Two fingerprint specs.
SPECS = {
    "blank_default": {
        # Three.js MeshPhysicalMaterial defaults. Empty `threejs` block
        # → renderer constructs the material with no params, Three.js
        # falls through to its built-in defaults (color=white,
        # metalness=0, roughness=1).
        "threejs": {"type": "MeshPhysicalMaterial"},
    },
    "blank_default_grey": {
        # pymat _PBR_DEFAULTS — the "default grey plastic" fingerprint
        # bernhard's mat-vis#285 detection asserts on. If a baked thumb
        # matches this, the substrate's mat_vis.pbr was empty + pymat's
        # render floor leaked through.
        "threejs": {
            "type": "MeshPhysicalMaterial",
            "color": 0xCCCCCC,
            "metalness": 0.0,
            "roughness": 0.5,
            "ior": 1.5,
            "transmission": 0.0,
            "clearcoat": 0.0,
            "emissive": [0, 0, 0],
        },
    },
}


def _render_one(label: str, spec: dict, tmp: Path, server_url: str, browser) -> Path:
    spec_path = tmp / f"{label}.json"
    spec_path.write_text(json.dumps(spec))

    page = browser.new_page(viewport={"width": 1024, "height": 1024})
    page.goto(f"{server_url}/thumb_render.html?spec={spec_path.name}", timeout=180_000)
    page.wait_for_function("() => window.__renderComplete === true", timeout=180_000)
    page.wait_for_timeout(1500)  # extra settle for PMREM convergence

    data_url = page.evaluate("() => document.querySelector('canvas').toDataURL('image/png')")
    raw_png = base64.b64decode(data_url.split(",", 1)[1])
    page.close()

    # Downsample 1024² → 256² with Lanczos for the canonical thumb size.
    img = Image.open(io.BytesIO(raw_png)).convert("RGB").resize((256, 256), Image.LANCZOS)
    out = ASSETS / f"{label}.png"
    img.save(out, format="PNG", optimize=True)
    return out


def main():
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        # Serve the renderer + GLB from a single dir.
        shutil.copy(THUMB_HTML, tmp / "thumb_render.html")
        shutil.copy(ASSETS / "shader_ball.glb", tmp / "shader_ball.glb")

        with _file_server(tmp) as server_url, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-gl=swiftshader"])
            for label, spec in SPECS.items():
                out = _render_one(label, spec, tmp, server_url, browser)
                print(
                    f"  ✓ {label} → {out.relative_to(REPO_ROOT)} ({out.stat().st_size // 1024}KB)"
                )
            browser.close()

    print(f"\n# done — {len(SPECS)} blanks at {ASSETS.relative_to(REPO_ROOT)}/")


if __name__ == "__main__":
    sys.exit(main() or 0)
