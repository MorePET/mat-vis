"""Per-layer render helpers for the failure-isolation suite (#361).

Each ``render_lN_*`` builds a Three.js spec dict at a different point
in the bake pipeline, then drives ``thumb_render.html`` through the
shared Playwright browser to produce a 256² PNG.

The spec format is the canonical ``{"threejs": {...}}`` envelope the
prod renderer accepts — see ``bake/preview/thumb_render.html``. By
holding the renderer + scene constant and varying only the spec
*construction path*, pixel-diffs between consecutive layers point at
the exact step that introduced a divergence.

Layer summary:

L0  raw substrate URLs  + hand-coded scalars (bypass client + adapter)
L1  fetch_all_textures  + hand-coded scalars (bypass _scalars_for + adapter)
L2  fetch_all_textures  + _scalars_for      (bypass adapter)
L3  fetch_all_textures  + _scalars_for      + to_threejs adapter
L4  whatever the prod orchestrator builds (mirrors ``run.py::_build_threejs_for``)

Why "hand-coded scalars" for L0/L1: the goal is to measure texture
fidelity in isolation from scalar-lookup logic. White color +
metalness-by-category + roughness=1.0 gives a consistent "baseline
material" that exercises every texture map without conflating scalar
bugs into the diff.
"""

from __future__ import annotations

import base64
import io
import json
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from .conftest import TST_REPO, TST_TAG

# Channel → Three.js MeshPhysicalMaterial property. Mirrors the
# ``_THREEJS_TEX_MAP`` in ``mat_vis_client.adapters`` but is duplicated
# here so L0/L1 can build a spec WITHOUT going through the adapter
# (otherwise the "skip the adapter" layers would silently route
# through it). Diverging from the registry is a bug the L1↔L3 diff
# would catch.
_CHANNEL_TO_THREEJS_PROP: dict[str, str] = {
    "color": "map",
    "normal": "normalMap",
    "roughness": "roughnessMap",
    "metalness": "metalnessMap",
    "ao": "aoMap",
    "emission": "emissiveMap",
    "opacity": "alphaMap",
    # NOTE: 'displacement' intentionally omitted — the renderer drops
    # displacementMap (see thumb_render.html comment + #385) so
    # including it here would create a false L1↔L3 divergence.
}

# Sources whose materials are inherently metallic. Used by the
# "hand-coded scalars" preset for L0/L1 to set metalness=1 so we
# actually see specular reflection on metal samples (otherwise
# roughness=1 + metalness=0 would render a flat lambertian brown for
# every metal in the suite).
_METAL_SOURCES = frozenset({"gpuopen"})  # heuristic; refined per-material below
_METAL_NAME_HINTS = (
    "metal",
    "chrome",
    "steel",
    "iron",
    "aluminum",
    "copper",
    "brass",
    "gold",
    "silver",
)

# Per-render output dimensions. Match the orchestrator (1024 supersample
# → 256 Lanczos downsample) so any cross-layer diff is comparable to
# what consumers actually see.
RENDER_W = 1024
RENDER_H = 1024
THUMB_SIZE = 256

# Per-render Playwright timeout. The renderer waits on
# ``window.__renderComplete`` which is set after warmup-render +
# 800ms settle (see thumb_render.html). 60s is generous for cold
# CDN fetches of the three.js bundle.
RENDER_TIMEOUT_MS = 60_000


# ── spec construction helpers ─────────────────────────────────────


def _png_to_data_uri(png_bytes: bytes) -> str:
    """Encode PNG bytes as a base64 data URI for the renderer to
    inline as a texture. Same shape ``adapters._to_data_uri`` emits."""
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _hand_picked_scalars(
    source: str, material_id: str, name_hint: str | None = None
) -> dict[str, Any]:
    """Return the L0/L1 baseline scalars: white base color, roughness 1,
    metalness 0 or 1 by category.

    Deliberately NOT using ``_scalars_for`` — that's the layer L2
    introduces. Hand-picked so L0/L1 isolate texture-fidelity bugs
    from scalar-lookup bugs.
    """
    name = (name_hint or material_id).lower()
    is_metal = source in _METAL_SOURCES or any(h in name for h in _METAL_NAME_HINTS)
    return {
        "color": "#ffffff",  # white pass-through so colorMap tints unmodified
        "metalness": 1.0 if is_metal else 0.0,
        "roughness": 1.0,
    }


def _spec_from_textures(scalars: dict[str, Any], textures: dict[str, bytes]) -> dict[str, Any]:
    """Build a threejs spec dict the hard way — one entry per known
    channel, no adapter involvement.

    The L0/L1/L2 layers all use this builder; only the *source* of
    ``scalars`` and ``textures`` varies. L3 swaps this for
    ``adapters.to_threejs`` so an L2↔L3 diff isolates adapter bugs.
    """
    spec: dict[str, Any] = {"type": "MeshPhysicalMaterial"}
    spec.update(scalars)
    for channel, prop in _CHANNEL_TO_THREEJS_PROP.items():
        if channel in textures:
            spec[prop] = _png_to_data_uri(textures[channel])
    if "opacity" in textures:
        # Same flag the adapter sets — see adapters.to_threejs.
        spec["transparent"] = True
    return spec


# ── raw substrate fetch (L0) ──────────────────────────────────────


def _raw_substrate_url(source: str, tier: str, mid: str, channel: str) -> str:
    """Direct HF resolve URL for one channel PNG. Bypasses the client
    so L0 measures "what the baker actually staged" with zero client
    code in the path."""
    return (
        f"https://huggingface.co/datasets/{TST_REPO}/resolve/{TST_TAG}"
        f"/{source}/{tier}/{mid}/{channel}.png"
    )


def _list_raw_channels(source: str, tier: str, mid: str) -> list[str]:
    """HEAD-probe the canonical channel set, return which ones the
    substrate actually has staged. No client code in the path.

    The probe set covers every channel the renderer can consume — if
    the baker stages something outside this set we won't render it,
    which would cause L0↔L1 to diverge and surface the gap.
    """
    found = []
    for ch in ("color", "normal", "roughness", "metalness", "ao", "emission", "opacity"):
        url = _raw_substrate_url(source, tier, mid, ch)
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    found.append(ch)
        except Exception:  # noqa: BLE001 — 404s are the common case
            continue
    return found


def _fetch_raw_substrate_textures(source: str, tier: str, mid: str) -> dict[str, bytes]:
    """GET each staged channel from the HF substrate. No caching, no
    typed errors — just the bytes the baker put on the wire."""
    textures: dict[str, bytes] = {}
    for ch in _list_raw_channels(source, tier, mid):
        url = _raw_substrate_url(source, tier, mid, ch)
        with urllib.request.urlopen(url, timeout=60) as r:
            textures[ch] = r.read()
    return textures


# ── shared render driver ──────────────────────────────────────────


def _render_spec(
    spec: dict[str, Any],
    *,
    browser,
    server_url: str,
    layered_tmpdir: Path,
    label: str,
) -> bytes:
    """Drop ``spec`` as a JSON file under the served tmpdir, drive
    ``thumb_render.html?spec=...`` to ``__renderComplete``, screenshot
    the canvas, downsample to 256² (matches prod), return PNG bytes.

    Same wait-for-completion + render-error-surfacing pattern the
    orchestrator (``bake/preview/run.py``) uses, including the post-
    complete 800ms settle. Diverging here would mean the test suite
    measures something different from what the prod bake produces.
    """
    spec_name = f"layered_{label}_{uuid.uuid4().hex[:8]}.json"
    spec_path = layered_tmpdir / spec_name
    spec_path.write_text(json.dumps({"threejs": spec}))

    page = browser.new_page(viewport={"width": RENDER_W, "height": RENDER_H})
    try:
        page.goto(
            f"{server_url}/thumb_render.html?spec={spec_name}&w={RENDER_W}&h={RENDER_H}",
            timeout=RENDER_TIMEOUT_MS,
        )
        page.wait_for_function("() => window.__renderComplete === true", timeout=RENDER_TIMEOUT_MS)
        # Surface JS-side errors instead of silently screenshotting a
        # stale canvas. Same defense ``run.py`` carries after #385.
        render_error = page.evaluate("() => window.__renderError || null")
        if render_error:
            raise RuntimeError(f"renderer JS error ({label}): {render_error}")
        page.wait_for_timeout(800)  # settle, matches run.py
        data_url = page.evaluate("() => document.querySelector('canvas').toDataURL('image/png')")
        raw = base64.b64decode(data_url.split(",", 1)[1])
    finally:
        page.close()
        spec_path.unlink(missing_ok=True)

    # Downsample to 256² with Lanczos — matches the orchestrator path.
    # PBR specialist verdict in #361/#385: kills specular aliasing +
    # roughness banding at thumbnail size, so doing it here keeps the
    # comparison apples-to-apples with what consumers fetch.
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ── per-layer renderers ───────────────────────────────────────────


def render_l0_raw(
    source: str,
    material_id: str,
    tier: str,
    *,
    browser,
    server_url: str,
    layered_tmpdir: Path,
    name_hint: str | None = None,
) -> bytes:
    """L0 — raw substrate textures + hand-coded scalars.

    Bypasses ``MatVisClient`` entirely. Goes straight to the HF
    resolve URL with urllib for each channel, uses the L0/L1 hand-
    picked scalars. This is the closest we can get to "what the
    baker actually produced" without re-hitting the upstream
    (gpuopen.com / ambientcg.com / polyhaven.com) — which we
    deliberately don't, because the substrate IS the mirror.

    Scalar-only sources (gpuopen scalar-tier, physicallybased) have
    no per-channel PNGs to pull, so this layer collapses to a flat
    scalar-only render — documented in the suite README.
    """
    textures = _fetch_raw_substrate_textures(source, tier, material_id) if tier != "scalar" else {}
    scalars = _hand_picked_scalars(source, material_id, name_hint=name_hint)
    spec = _spec_from_textures(scalars, textures)
    return _render_spec(
        spec,
        browser=browser,
        server_url=server_url,
        layered_tmpdir=layered_tmpdir,
        label="l0",
    )


def render_l1_substrate(
    client,
    source: str,
    material_id: str,
    tier: str,
    *,
    browser,
    server_url: str,
    layered_tmpdir: Path,
    name_hint: str | None = None,
) -> bytes:
    """L1 — textures via ``client.fetch_all_textures`` + hand-coded scalars.

    Adds the client's per-file fetch path (cache, channel-existence
    check, ``.tier_complete`` probe, error-typing). Same hand-coded
    scalars as L0. An L0↔L1 diff means the client is re-encoding,
    re-compressing, or otherwise mutating what the substrate served.
    """
    if tier != "scalar":
        textures = client.fetch_all_textures(source, material_id, tier)
    else:
        textures = {}
    scalars = _hand_picked_scalars(source, material_id, name_hint=name_hint)
    spec = _spec_from_textures(scalars, textures)
    return _render_spec(
        spec,
        browser=browser,
        server_url=server_url,
        layered_tmpdir=layered_tmpdir,
        label="l1",
    )


def render_l2_client(
    client,
    source: str,
    material_id: str,
    tier: str,
    *,
    browser,
    server_url: str,
    layered_tmpdir: Path,
) -> bytes:
    """L2 — textures via fetch + scalars via ``_scalars_for`` (no adapter).

    Adds the catalog-lookup path. Spec is still constructed by hand.
    An L1↔L2 diff means ``_scalars_for`` produced different scalars
    than the L0/L1 hand-coded baseline, which is *expected* per
    material (substrate scalars beat L1's flat-white guess) — but
    the flavor of difference catches scalar-passthrough regressions
    (e.g. dropped clearcoat, miss-keyed transmission).

    Notes on shape: ``_scalars_for`` returns a dict keyed by adapter-
    interface names (``color_hex``, ``metalness``, ``roughness``,
    ``clearcoat``, ...). The renderer's ``_buildMaterial`` consumes
    only a subset (color/metalness/roughness/ior/transmission/
    clearcoat/emissive + texture maps), so we passthrough the keys
    the renderer recognizes and rename ``color_hex``→``color``.
    """
    if tier != "scalar":
        textures = client.fetch_all_textures(source, material_id, tier)
    else:
        textures = {}
    raw_scalars = client._scalars_for(source, material_id)
    spec_scalars = _renderer_scalars_from_lookup(raw_scalars)
    spec = _spec_from_textures(spec_scalars, textures)
    return _render_spec(
        spec,
        browser=browser,
        server_url=server_url,
        layered_tmpdir=layered_tmpdir,
        label="l2",
    )


def _renderer_scalars_from_lookup(raw: dict) -> dict:
    """Map ``_scalars_for`` keys onto the renderer's spec keys, *without*
    going through the to_threejs adapter.

    Mirrors the renderer-side ``_buildMaterial`` reading list (see
    ``thumb_render.html``). Anything not in the renderer's read set is
    dropped here — keeping the L2 spec narrow makes the L2↔L3 diff a
    clean isolation of the adapter's behavior (color de-gamma,
    specular-color encoding, metalness alias normalization, ...).
    """
    spec: dict = {}
    if "color_hex" in raw and raw["color_hex"]:
        spec["color"] = raw["color_hex"]
    # Raw scalar keys are snake_case from the substrate; the Three.js
    # renderer expects camelCase. Map the keys that differ.
    _RAW_TO_THREEJS = {
        "clearcoat_roughness": "clearcoatRoughness",
        "specular_intensity": "specularIntensity",
        "specular_color": "specularColor",
        "sheen_color": "sheenColor",
        "sheen_roughness": "sheenRoughness",
        "iridescence_ior": "iridescenceIOR",
        "iridescence_thickness": "iridescenceThicknessRange",
        "emissive_intensity": "emissiveIntensity",
    }
    for key in (
        "metalness", "roughness", "ior", "transmission", "thickness",
        "dispersion", "clearcoat", "clearcoat_roughness",
        "specular_intensity", "specular_color",
        "sheen", "sheen_color", "sheen_roughness",
        "iridescence", "iridescence_ior", "iridescence_thickness",
        "emissive_intensity",
    ):
        val = raw.get(key)
        if val is not None:
            spec[_RAW_TO_THREEJS.get(key, key)] = val
    if raw.get("emissive") is not None:
        spec["emissive"] = list(raw["emissive"])
    return spec


def render_l3_adapter(
    client,
    source: str,
    material_id: str,
    tier: str,
    *,
    browser,
    server_url: str,
    layered_tmpdir: Path,
) -> bytes:
    """L3 — textures via fetch + scalars via ``_scalars_for`` + spec via ``to_threejs``.

    Adds the adapter layer. An L2↔L3 diff isolates adapter behavior:
    color sRGB↔linear handling, metalness alias resolution,
    specular_color packing, hex-vs-int color format, etc. This is the
    layer where post-#380 / #381 passthrough regressions land.
    """
    from mat_vis_client.adapters import to_threejs

    if tier != "scalar":
        textures = client.fetch_all_textures(source, material_id, tier)
    else:
        textures = {}
    raw_scalars = client._scalars_for(source, material_id)
    spec = to_threejs(raw_scalars, textures)
    return _render_spec(
        spec,
        browser=browser,
        server_url=server_url,
        layered_tmpdir=layered_tmpdir,
        label="l3",
    )


def render_l4_full(
    client,
    source: str,
    material_id: str,
    tier: str,
    *,
    browser,
    server_url: str,
    layered_tmpdir: Path,
) -> bytes:
    """L4 — full pipeline: same path the prod orchestrator uses.

    Mirrors ``bake/preview/run.py::_build_threejs_for``: walks the
    tier list (1k → 512 → 256 → 128) for the largest available, falls
    back to scalar-only if nothing is staged. The L3↔L4 diff
    therefore catches orchestrator-side regressions (tier-resolution
    bugs, missing channel sets, spec-envelope mistakes).

    The ``tier`` arg is honored as the FIRST candidate so fixture
    materials don't silently get downsized. The fallback chain still
    runs if the named tier is missing.
    """
    from mat_vis_client.adapters import to_threejs

    # Match run.py's tier-fallback walk; use the test-requested tier first.
    tier_candidates = (tier, "1k", "512", "256", "128")
    seen = set()
    textures: dict[str, bytes] = {}
    for t in tier_candidates:
        if t in seen or t == "scalar":
            continue
        seen.add(t)
        try:
            textures = client.fetch_all_textures(source, material_id, t)
            break
        except Exception:  # noqa: BLE001 — fall through, matches run.py
            continue
    raw_scalars = client._scalars_for(source, material_id)
    spec = to_threejs(raw_scalars, textures)
    return _render_spec(
        spec,
        browser=browser,
        server_url=server_url,
        layered_tmpdir=layered_tmpdir,
        label="l4",
    )


# ── pixel-diff utilities ──────────────────────────────────────────


def _aligned_pair(a_png: bytes, b_png: bytes) -> tuple[Image.Image, Image.Image]:
    """Decode both PNGs to RGB, resize ``b`` to match ``a``. Pillow-only
    so the suite carries no numpy dependency.
    """
    a = Image.open(io.BytesIO(a_png)).convert("RGB")
    b = Image.open(io.BytesIO(b_png)).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    return a, b


def rms_diff(a_png: bytes, b_png: bytes) -> float:
    """RMS pixel difference (0..255 scale, sRGB) between two PNGs.

    Resizes ``b`` to match ``a``'s shape so callers don't have to
    hand-align dimensions when one path emits a different downsample.
    Returns 0.0 for identical images.

    Pillow-only implementation — uses ``ImageChops.difference`` +
    ``Image.histogram`` to compute the per-channel sum-of-squares
    without numpy. Matches numpy's ``sqrt(((a-b)**2).mean())`` to
    within float32 rounding.
    """
    from PIL import ImageChops

    a, b = _aligned_pair(a_png, b_png)
    diff = ImageChops.difference(a, b)
    hist = diff.histogram()  # 256 bins per band, 3 bands for RGB → 768
    # Sum of v² × count across all bands.
    total_sq = 0
    total_pixels = 0
    for band in range(3):
        band_hist = hist[band * 256 : (band + 1) * 256]
        for v, count in enumerate(band_hist):
            total_sq += (v * v) * count
            total_pixels += count
    if total_pixels == 0:
        return 0.0
    mean_sq = total_sq / total_pixels
    return mean_sq**0.5


def diff_image(a_png: bytes, b_png: bytes) -> bytes:
    """Pixelwise abs-diff PNG, scaled ×4 for visibility. Caller saves
    it next to the L0..L4 outputs so failures are inspectable by eye.
    """
    from PIL import ImageChops

    a, b = _aligned_pair(a_png, b_png)
    diff = ImageChops.difference(a, b)
    # ×4 visibility scale; clip via point() so saturation looks right.
    boosted = diff.point(lambda v: min(255, v * 4))
    out = io.BytesIO()
    boosted.save(out, format="PNG", optimize=True)
    return out.getvalue()


def unique_color_count(png: bytes, sample_size: int | None = None) -> int:
    """Count distinct sRGB colors in a PNG. Catches the "scalar-only
    collapse" failure mode where a render comes back as 7-12 unique
    colors (flat lighting on flat scalars + no textures bound).

    ``sample_size``: optional resize-before-count for tests that only
    care about gross variation. Default None counts on the full image.
    """
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if sample_size is not None:
        img = img.resize((sample_size, sample_size), Image.LANCZOS)
    # Pillow 14 deprecates ``getdata()``; iterate via ``tobytes()`` +
    # 3-byte chunks instead. Same set semantics, no deprecation noise.
    raw = img.tobytes()
    return len({raw[i : i + 3] for i in range(0, len(raw), 3)})
