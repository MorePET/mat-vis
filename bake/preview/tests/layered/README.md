# Layered failure-isolation tests (mat-vis#361)

Diagnostic suite for the thumb-bake render pipeline. When a thumb
looks wrong, the test that fails *names the layer* that introduced
the divergence.

## What each layer represents

The thumb-bake pipeline has five layers, top → bottom:

```
upstream (gpuopen / ambientcg / polyhaven / physicallybased)
  ↓ baker
substrate (HF dataset: per-channel PNGs + catalog JSON)
  ↓ MatVisClient.fetch_all_textures + ._scalars_for
client-extracted scalars + texture bytes
  ↓ adapters.to_threejs
threejs spec dict (with data-URI textures)
  ↓ thumb_render.html (Three.js MeshPhysicalMaterial)
rendered PNG
```

For each fixture material the suite renders the same shader-ball at
five depths, varying ONLY how the spec dict is constructed — the
renderer HTML, scene, camera, lighting, and downsample are held
constant. The first cross-layer pixel-diff that exceeds threshold
identifies the offending layer:

| Layer | Spec built from |
|---|---|
| **L0** raw substrate | `urllib.GET(<HF>/<source>/<tier>/<mid>/<channel>.png)` per channel + hand-coded scalars (color=`#ffffff`, metalness=0/1 by category, roughness=1.0). Bypasses the client entirely. |
| **L1** substrate via client | `MatVisClient.fetch_all_textures(...)` + same hand-coded scalars. Adds the client's per-file path (cache, `.tier_complete` probe, channel-existence check). |
| **L2** client + scalar lookup | Textures via fetch + scalars via `client._scalars_for(...)`. Spec still constructed by hand (no adapter). |
| **L3** client + adapter | Textures via fetch + scalars via `_scalars_for` + `adapters.to_threejs(scalars, textures)`. Adds the adapter (color sRGB↔linear, metalness alias, etc.). |
| **L4** full pipeline | Mirrors `bake/preview/run.py::_build_threejs_for` — tier-fallback walk + `to_threejs`. This is what the prod orchestrator produces. |

## How to run

```sh
# default: skipped (CI-safe)
uv run pytest bake/preview/tests/layered/

# full visual run — Playwright + HF fetch + headless Chromium
MAT_VIS_VISUAL=1 uv run pytest bake/preview/tests/layered/ -v -s

# diagnostic mode — never fails on threshold; writes report + diffs
MAT_VIS_VISUAL=1 MAT_VIS_LAYERED_REPORT_ONLY=1 \
    uv run pytest bake/preview/tests/layered/ -v -s

# point at a different substrate revision
MAT_VIS_VISUAL=1 MAT_VIS_DATASET=gerchowl/mat-vis@v2026.04.2 \
    uv run pytest bake/preview/tests/layered/ -v -s
```

First run downloads Chromium + the HF substrate textures; subsequent
runs hit the per-test tmp cache (each test gets a fresh cache dir, so
no cross-test contamination). The renderer HTML pulls Three.js from
jsdelivr; offline runs need a CDN cache or a vendored bundle (out of
scope for this suite — same constraint as the prod bake).

## Output layout

```
bake/preview/tests/layered/output/<source>__<material>__<tier>/
├── l0.png                  # 256² PNG, raw substrate
├── l1.png                  # 256² PNG, client fetch
├── l2.png                  # 256² PNG, + scalar lookup
├── l3.png                  # 256² PNG, + adapter
├── l4.png                  # 256² PNG, full pipeline
├── diff_l0_l1.png          # abs-diff x4 (visibility scaled)
├── diff_l1_l2.png
├── diff_l2_l3.png
├── diff_l3_l4.png
└── pairs.json              # {pairs: {l0_l1: {rms, threshold, over_threshold}}, ...}
```

The diff images are scaled ×4 — anything visible to the eye is well
over the RMS threshold. A black diff = identical layers.

## How to interpret failures

| First-failing pair | Likely culprit |
|---|---|
| **L0 ↔ L1** | Client mutates substrate bytes — re-encoding, channel-name remap, or a stale cache layer. Check `MatVisClient.fetch_texture` + `_resolve_material_id`. |
| **L1 ↔ L2** | Scalar lookup changed material intent. `client._scalars_for` returned scalars that don't match the L0/L1 hand-coded baseline. Expected on a per-material basis (e.g. metals get a non-white tint), but a regression here means a passthrough field is now missing or wrong. |
| **L2 ↔ L3** | Adapter bug. `adapters.to_threejs` is producing a different spec than the hand-built L2 spec. Common causes: color sRGB↔linear (#380), metalness alias normalization, specular_color encoding, hex-vs-int color format. |
| **L3 ↔ L4** | Orchestrator-side regression. `_build_threejs_for` is picking a different tier, dropping channels, or wrapping the spec wrong. Compare the chosen tier in `pairs.json` vs the requested fixture tier. |
| **Color count under threshold** at L1/L2/L3/L4 | "Scalar-only collapse" — texture maps did not bind. Check the renderer's `_buildMaterial` await path (was the cause of #385) or whether the texture PNG bytes are non-empty. |

The "first-failing pair" is the diagnostic — once it's identified,
narrow further by inspecting the `diff_*.png` for that pair. Lower-
layer diffs (L0↔L1) implicate the substrate or fetch path; higher
ones implicate scalar/adapter/orchestrator.

## When to update thresholds

The four pair thresholds are set at:

- L0↔L1, L1↔L2, L2↔L3 → RMS < 12.0 (these layers should be near-
  lossless; small diffs come from scalar intent-changes between L1
  and L2, which the threshold is set to tolerate)
- L3↔L4 → RMS < 30.0 (the orchestrator's tier-fallback may pick a
  smaller tier than the fixture requested)

Update them when:

1. **Renderer settings legitimately change** — exposure tweak, tone-
   map swap, scene background change. Update L3↔L4 first; the others
   should still match.
2. **Adapter behavior intentionally changes** — e.g. moving more
   passthrough into `to_threejs`. Update L2↔L3.
3. **Substrate format changes** — e.g. KTX2 displacing PNG. Update
   L0↔L1 (client-side decoder change).

Don't loosen thresholds to silence a flaky test — re-run with
`MAT_VIS_LAYERED_REPORT_ONLY=1` and inspect the diff. RMS this low
is signal; flakiness this high comes from a real source.

## CI gating

The full suite is gated on `MAT_VIS_VISUAL=1`. Default `pytest
bake/preview/tests/layered/` collects the tests but skips them all
(see `conftest.py::pytest_collection_modifyitems`). Wire it into a
dedicated visual-tests workflow if/when you want CI signal — until
then, this suite is operator-driven (run when a thumb regression is
suspected).

## References

- ADR-0014 — `docs/decisions/0014-thumb-tier-renderer-choice.md`
- mat-vis#361 — substrate-side thumb tier (parent issue)
- mat-vis#385 — async texture-decode regression (the bug this suite
  is built to catch in future)
- mat-vis#391 — `MatVisClient(repo=, tag=)` constructor kwargs (the
  API the suite uses for substrate routing)
- pymat#392 — visual regression port (different scope: end-to-end,
  not failure-isolation)
