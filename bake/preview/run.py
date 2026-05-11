"""Per-material thumb-tier bake orchestrator (mat-vis#361).

Walks the mat-vis catalog, builds a `to_threejs(vis)` payload per
material via mat-vis-client, drives Playwright + headless Chromium
through `thumb_render.html`, writes one PNG per material under
``<out_dir>/<source>/<material_id>/thumb.png``.

After the loop, writes a ``_bake_complete.json`` sentinel under
``<out_dir>`` and runs ``check_thumbs.py`` as a CI gate to catch
silently-broken bakes (default-grey fingerprints, empty material specs,
byte-identical cross-material renders #385). The sentinel is what tells
the gate the directory represents a *completed* bake — without it the
gate refuses to validate (#385 exit code 3) so a crashed-mid-bake
directory can't pass for a clean one.

Usage:
    uv run python bake/preview/run.py --out /tmp/thumbs
    uv run python bake/preview/run.py --out /tmp/thumbs --source gpuopen
    uv run python bake/preview/run.py --out /tmp/thumbs --source gpuopen --limit 10

This script is the runtime entry point of the thumb bake. The Dagger
cell wraps it; the GHA matrix shards across sources.
"""

from __future__ import annotations

import argparse
import base64
import http.server
import io
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSETS = REPO_ROOT / "bake" / "preview" / "assets"
THUMB_HTML = REPO_ROOT / "bake" / "preview" / "thumb_render.html"
SHADER_BALL = ASSETS / "shader_ball.glb"
CHECK_THUMBS = REPO_ROOT / "bake" / "preview" / "utils" / "check_thumbs.py"

# Default sources to bake. Order matters only for log readability.
DEFAULT_SOURCES = ("gpuopen", "ambientcg", "polyhaven", "physicallybased")

log = logging.getLogger("thumb-bake")


@contextmanager
def _file_server(serve_dir: Path, port: int = 8773):
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


def _build_threejs_for(client, source: str, material_id: str) -> dict | None:
    """Build a `to_threejs` payload for one material. Returns None
    when the material is unrenderable (skip with a warning, don't crash
    the bake).
    """
    from mat_vis_client.adapters import to_threejs

    try:
        scalars = client._scalars_for(source, material_id)
    except Exception as e:  # noqa: BLE001
        log.warning("scalars lookup failed for %s/%s: %s", source, material_id, e)
        return None

    # Pick the largest texture tier this material has. mat-vis#361
    # convention: render from 1k when staged (best quality at 256²
    # output via mipmap downsampling). Fall back to smaller; if
    # nothing is staged, render scalar-only.
    textures: dict[str, bytes] = {}
    for tier in ("1k", "512", "256", "128"):
        try:
            textures = client.fetch_all_textures(source, material_id, tier)
            break
        except Exception:  # noqa: BLE001
            continue

    return to_threejs(scalars, textures)


def _bake_source(
    client, source: str, out_dir: Path, limit: int | None, browser, server_url: str, tmpdir: Path
) -> dict[str, int]:
    """Bake every material in a source. Returns counters.

    ``limit`` is for incremental dev runs; production passes None.
    """
    counters = {"ok": 0, "skipped": 0, "errors": 0}
    entries = client.index(source)
    if limit is not None:
        entries = entries[:limit]

    src_out = out_dir / source
    src_out.mkdir(parents=True, exist_ok=True)
    page = browser.new_page(viewport={"width": 1024, "height": 1024})

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        if not isinstance(mid, str):
            continue

        threejs = _build_threejs_for(client, source, mid)
        if threejs is None:
            counters["skipped"] += 1
            continue

        spec = {"threejs": threejs}
        spec_path = tmpdir / f"spec_{source}_{idx}.json"
        spec_path.write_text(json.dumps(spec))

        out_path = src_out / mid / "thumb.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            page.goto(f"{server_url}/thumb_render.html?spec={spec_path.name}", timeout=180_000)
            page.wait_for_function("() => window.__renderComplete === true", timeout=180_000)
            # Surface JS-side render errors instead of silently screenshotting
            # a stale canvas — the original byte-identical-x3 bake (#385) was
            # caused by an undefined-var ReferenceError that was caught by
            # `main().catch()`, set `__renderComplete=true`, and let the
            # orchestrator screenshot a pre-texture-load frame.
            render_error = page.evaluate("() => window.__renderError || null")
            if render_error:
                raise RuntimeError(f"renderer JS error: {render_error}")
            page.wait_for_timeout(800)
            data_url = page.evaluate(
                "() => document.querySelector('canvas').toDataURL('image/png')"
            )
            raw = base64.b64decode(data_url.split(",", 1)[1])
            img = Image.open(io.BytesIO(raw)).convert("RGB").resize((256, 256), Image.LANCZOS)
            img.save(out_path, format="PNG", optimize=True)
            counters["ok"] += 1
            if (idx + 1) % 50 == 0:
                log.info("%s: %d/%d done", source, idx + 1, len(entries))
        except Exception as e:  # noqa: BLE001
            log.warning("render failed for %s/%s: %s", source, mid, e)
            counters["errors"] += 1
        finally:
            spec_path.unlink(missing_ok=True)

    page.close()
    return counters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help=f"limit to a specific source; repeatable. default: {','.join(DEFAULT_SOURCES)}",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="limit materials per source (for dev)"
    )
    parser.add_argument(
        "--skip-check", action="store_true", help="skip the post-bake fingerprint check"
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="explicit substrate revision (HF branch or tag). Default: client picks DEFAULT_TAG",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    sources = tuple(args.source) if args.source else DEFAULT_SOURCES

    args.out.mkdir(parents=True, exist_ok=True)

    from mat_vis_client import MatVisClient
    from playwright.sync_api import sync_playwright

    client = MatVisClient(tag=args.tag) if args.tag else MatVisClient()
    log.info("active release tag: %s", client._tag)

    with tempfile.TemporaryDirectory() as tmpd:
        tmpdir = Path(tmpd)
        # Serve the renderer + GLB + per-spec JSONs from a single dir.
        shutil.copy(THUMB_HTML, tmpdir / "thumb_render.html")
        shutil.copy(SHADER_BALL, tmpdir / "shader_ball.glb")

        with _file_server(tmpdir) as server_url, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-gl=swiftshader"])
            try:
                totals = {"ok": 0, "skipped": 0, "errors": 0}
                source_results: dict[str, dict[str, int]] = {}
                for source in sources:
                    log.info("baking source: %s", source)
                    t0 = time.monotonic()
                    counters = _bake_source(
                        client, source, args.out, args.limit, browser, server_url, tmpdir
                    )
                    dt = time.monotonic() - t0
                    log.info(
                        "%s done in %.1fs: ok=%d skipped=%d errors=%d",
                        source,
                        dt,
                        counters["ok"],
                        counters["skipped"],
                        counters["errors"],
                    )
                    source_results[source] = counters
                    for k, v in counters.items():
                        totals[k] += v
            finally:
                browser.close()

        log.info(
            "total: ok=%d skipped=%d errors=%d",
            totals["ok"],
            totals["skipped"],
            totals["errors"],
        )

        # #385: drop the sentinel so check_thumbs.py knows this directory
        # represents a completed bake. Written here (inside the tmpdir
        # context but after the bake loop) so a crashed bake never lands one.
        from datetime import datetime, timezone

        sentinel = args.out / "_bake_complete.json"
        sentinel.write_text(
            json.dumps(
                {
                    "totals": totals,
                    "source_results": source_results,
                    "release_tag": client._tag,
                    "baked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                indent=2,
                sort_keys=True,
            )
        )

    if not args.skip_check:
        log.info("running thumb-check (#385 + fingerprint) against %s", args.out)
        rc = subprocess.run(
            [
                sys.executable,
                str(CHECK_THUMBS),
                str(args.out),
                "--release-tag",
                client._tag,
            ],
            check=False,
        ).returncode
        if rc != 0:
            log.error("thumb-check failed (rc=%d) — see above + thumb-check.json", rc)
            return rc

    return 0 if totals["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
