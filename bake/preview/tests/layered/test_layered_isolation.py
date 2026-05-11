"""Layered failure-isolation tests for the thumb-bake render pipeline.

For each fixture material, render the same shader-ball at five
pipeline depths (L0..L4 — see ``render_helpers``) and pixel-diff
each consecutive pair. The first pair whose RMS exceeds threshold
identifies the layer that introduced the divergence.

Outputs (under ``output/<source>__<material>__<tier>/``):
    l0.png ... l4.png      — per-layer renders
    diff_l0_l1.png ...     — abs-diff x4 between consecutive layers
    pairs.json             — structured RMS report

Thresholds (sRGB 0..255 RMS):
    L0→L1, L1→L2, L2→L3   < 12.0   (these layers should be near-lossless;
                                     small diffs come from scalar
                                     intent-changes between L1 and L2)
    L3→L4                  < 30.0   (orchestrator picks the largest tier
                                     available + applies the same
                                     adapter — should match L3 closely
                                     for fixture rows that already
                                     specify the right tier, looser
                                     bound for fallback paths)

Gating: skipped unless ``MAT_VIS_VISUAL=1`` (see conftest).

Diagnostic-mode env: ``MAT_VIS_LAYERED_REPORT_ONLY=1`` writes the
pair report + diff images but DOES NOT fail on threshold violation —
useful when inventorying which layers diverge across a new substrate
revision.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from .conftest import OUTPUT_DIR
from .render_helpers import (
    diff_image,
    render_l0_raw,
    render_l1_substrate,
    render_l2_client,
    render_l3_adapter,
    render_l4_full,
    rms_diff,
    unique_color_count,
)

# Diagnostic mode: write artifacts + RMS report but don't fail. Lets a
# reviewer scan a fresh substrate without the suite immediately
# blocking on known-divergent fixtures.
REPORT_ONLY = os.environ.get("MAT_VIS_LAYERED_REPORT_ONLY", "0") == "1"

# Per-pair RMS thresholds. See module docstring for rationale.
RMS_THRESHOLDS = {
    ("l0", "l1"): 12.0,
    ("l1", "l2"): 12.0,
    ("l2", "l3"): 12.0,
    ("l3", "l4"): 30.0,
}

# Minimum unique-color count per layer at the rendered 256² output.
# Calibrated empirically against the fixture set (observed counts on
# the v2026.04.99-tst-full-369 substrate revision):
#   - Metal007 (correct, full texture binding):  8372 unique colors
#   - yellow_plaster (washed; colorMap binds but
#     normal/roughness detail does not surface):  1672 unique colors
#   - Bark001  (washed; no texture binding at all):  576 unique colors
#   - scalar-only sphere (lighting gradient + tint):  1850-3272 colors
# Textured threshold = 3000: catches both Bark001 and yellow_plaster
# while letting Metal007 (the known-correct reference) pass with margin.
# Scalar threshold = 200: scalar-only renders carry intent through the
# lighting gradient + base color tint, even when L0/L1 collapse to
# white spheres (no scalars applied in the hand-coded baseline).
MIN_UNIQUE_COLORS_TEXTURED = 3000
MIN_UNIQUE_COLORS_SCALAR = 200

# Fixture materials. Picked to cover:
#   - each source (ambientcg, polyhaven, gpuopen, physicallybased)
#   - one known-correct textured material (Metal007 — bernhard's
#     reference looked good in #285)
#   - two known-washed textured materials (Bark001, yellow_plaster)
#     — these MUST trigger the suite, otherwise the suite is useless
#     (acceptance criterion).
#   - two scalar-only materials so L0/L1 collapse is exercised.
FIXTURES: list[tuple[str, str, str]] = [
    ("ambientcg", "Bark001", "1k"),  # known to look washed
    ("ambientcg", "Metal007", "1k"),  # bernhard reference, known good
    ("polyhaven", "yellow_plaster", "1k"),  # known to look washed
    ("gpuopen", "34f2c1f9-6169-4975-b5d8-4e21f49ddf55", "scalar"),  # Chrome (scalar-only)
    ("physicallybased", "germanium", "scalar"),  # scalar-only
]


def _fixture_id(triple: tuple[str, str, str]) -> str:
    """Pretty-print id for parametrize so test names stay short.

    UUIDs (gpuopen) get truncated; scalar-only sources get a tag.
    """
    src, mid, tier = triple
    short_mid = mid[:8] + "…" if len(mid) > 16 else mid
    return f"{src}-{short_mid}-{tier}"


def _outdir_for(triple: tuple[str, str, str]) -> Path:
    """Per-fixture output dir. Underscore-joined so ``ls output/`` is
    a flat, sortable listing."""
    src, mid, tier = triple
    safe_mid = mid.replace("/", "_")
    out = OUTPUT_DIR / f"{src}__{safe_mid}__{tier}"
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.mark.parametrize("fixture", FIXTURES, ids=_fixture_id)
def test_layered_pipeline(fixture, mat_vis_client, playwright_browser, file_server, layered_tmpdir):
    """Render L0..L4 for ``fixture``, write artifacts, assert pair RMS
    + per-layer color variation.

    A single test per fixture (rather than one per pair) keeps the
    five renders co-located so the diff artifacts on disk tell a
    coherent story per material. Failure messages name the offending
    pair so the diagnostic ("which layer broke it?") is in the
    pytest output, not just the diff images.
    """
    source, material_id, tier = fixture
    out_dir = _outdir_for(fixture)

    # Render every layer. Catch + record per-layer errors so a
    # failure at e.g. L2 doesn't hide a passing L0/L1 — the suite is
    # a diagnostic, partial coverage is more valuable than zero.
    renders: dict[str, bytes] = {}
    errors: dict[str, str] = {}
    layer_calls = [
        (
            "l0",
            lambda: render_l0_raw(
                source,
                material_id,
                tier,
                browser=playwright_browser,
                server_url=file_server,
                layered_tmpdir=layered_tmpdir,
            ),
        ),
        (
            "l1",
            lambda: render_l1_substrate(
                mat_vis_client,
                source,
                material_id,
                tier,
                browser=playwright_browser,
                server_url=file_server,
                layered_tmpdir=layered_tmpdir,
            ),
        ),
        (
            "l2",
            lambda: render_l2_client(
                mat_vis_client,
                source,
                material_id,
                tier,
                browser=playwright_browser,
                server_url=file_server,
                layered_tmpdir=layered_tmpdir,
            ),
        ),
        (
            "l3",
            lambda: render_l3_adapter(
                mat_vis_client,
                source,
                material_id,
                tier,
                browser=playwright_browser,
                server_url=file_server,
                layered_tmpdir=layered_tmpdir,
            ),
        ),
        (
            "l4",
            lambda: render_l4_full(
                mat_vis_client,
                source,
                material_id,
                tier,
                browser=playwright_browser,
                server_url=file_server,
                layered_tmpdir=layered_tmpdir,
            ),
        ),
    ]
    for name, fn in layer_calls:
        try:
            png = fn()
            (out_dir / f"{name}.png").write_bytes(png)
            renders[name] = png
        except Exception as e:  # noqa: BLE001 — diagnostic suite, capture + continue
            errors[name] = f"{type(e).__name__}: {e}"

    # Build the pair report. RMS=None when one side errored.
    pair_report: dict[str, dict[str, Any]] = {}
    for (a, b), threshold in RMS_THRESHOLDS.items():
        entry: dict[str, Any] = {"threshold": threshold}
        if a in renders and b in renders:
            r = rms_diff(renders[a], renders[b])
            entry["rms"] = r
            entry["over_threshold"] = r > threshold
            (out_dir / f"diff_{a}_{b}.png").write_bytes(diff_image(renders[a], renders[b]))
        else:
            entry["rms"] = None
            entry["over_threshold"] = False
            entry["missing"] = [n for n in (a, b) if n not in renders]
        pair_report[f"{a}_{b}"] = entry

    # Per-layer color variation (catches scalar-only collapse).
    color_report: dict[str, int] = {}
    for name, png in renders.items():
        color_report[name] = unique_color_count(png, sample_size=256)

    full_report = {
        "fixture": {"source": source, "material_id": material_id, "tier": tier},
        "errors": errors,
        "pairs": pair_report,
        "unique_colors": color_report,
        "report_only": REPORT_ONLY,
    }
    (out_dir / "pairs.json").write_text(json.dumps(full_report, indent=2))

    # Surface the diagnostic in stdout — pytest -s prints it; pytest
    # -v shows it on failure. Either way the operator sees which
    # layer the divergence starts at without opening the JSON.
    print(f"\n[layered] {source}/{material_id}@{tier}")
    for name, png in renders.items():
        print(f"  {name}: {len(png)} bytes, {color_report.get(name, '?')} unique colors")
    for pair, entry in pair_report.items():
        rms = entry["rms"]
        rms_s = f"{rms:.2f}" if rms is not None else "—"
        flag = "  OVER" if entry["over_threshold"] else ""
        print(f"  pair {pair}: rms={rms_s} (threshold {entry['threshold']}){flag}")
    if errors:
        print(f"  errors: {errors}")

    if REPORT_ONLY:
        # Diagnostic-only mode: write artifacts + return success so
        # the suite can be used for inventorying without blocking.
        return

    # Hard assertions. Errors first — a layer that crashes is louder
    # than one that drifts in pixels.
    assert not errors, f"layer render(s) failed for {source}/{material_id}@{tier}: {errors}"

    # Per-layer color variation. Two thresholds:
    #   - textured fixtures (tier != 'scalar'): every layer must show
    #     rich texture binding (≥ MIN_UNIQUE_COLORS_TEXTURED). This
    #     catches the Bark001 / yellow_plaster "washed" failure mode
    #     where all 5 layers render a flat sphere with no surface
    #     detail (textures present in the spec but not sampled).
    #   - scalar-only fixtures: the sphere lighting gradient alone is
    #     enough variation; require only MIN_UNIQUE_COLORS_SCALAR.
    for name, count in color_report.items():
        if tier == "scalar":
            min_count = MIN_UNIQUE_COLORS_SCALAR
        else:
            min_count = MIN_UNIQUE_COLORS_TEXTURED
        if count < min_count:
            pytest.fail(
                f"{name} render for {source}/{material_id}@{tier} has only "
                f"{count} unique colors (< {min_count}); likely texture-"
                f"binding failure — channels in the spec did not sample "
                f"on the shader ball. See {out_dir}/{name}.png."
            )

    # Pair RMS thresholds. First-failing pair names the breaking layer.
    #
    # Scalar-only fixtures (gpuopen scalar tier, physicallybased) get
    # L0↔L1 and L1↔L2 excluded from hard-fail: those layers carry the
    # hand-coded baseline scalars (white + 0/1 metalness + 1.0 rough),
    # while L2 introduces the substrate's real scalars. The L1↔L2 RMS
    # is therefore *expected* to be large (it's the whole point of
    # _scalars_for for these sources). The diff still gets written +
    # reported for diagnostic value; it just isn't a failure.
    excluded_pairs = {"l0_l1", "l1_l2"} if tier == "scalar" else set()
    breakers = [
        (pair, entry["rms"], entry["threshold"])
        for pair, entry in pair_report.items()
        if entry["over_threshold"] and pair not in excluded_pairs
    ]
    assert not breakers, (
        f"{source}/{material_id}@{tier}: pipeline divergence detected at "
        f"pair(s) {breakers}. See {out_dir}/diff_*.png + pairs.json. "
        f"The first pair listed is where the divergence enters the stack."
    )
