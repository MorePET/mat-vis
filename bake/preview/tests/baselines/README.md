# Visual-regression baselines

Committed reference PNGs for `bake/preview/tests/test_visual_regression.py`.
Each PNG is a 1024² render of the bernhard mat-vis#285 grid through
`bake/preview/thumb_render.html` — the same renderer the production
thumb bake (`bake/preview/run.py`) drives.

## Generate / regenerate

```bash
MAT_VIS_HF_BASE="https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve" \
MAT_VIS_TAG=v2026.04.99-tst-full-369 \
MAT_VIS_UPDATE_BASELINES=1 \
MAT_VIS_SKIP_VISUAL=0 \
  uv run --with playwright --with pillow --with-editable clients/python \
  pytest bake/preview/tests/test_visual_regression.py -v -s
```

After generation:

1. `open bake/preview/tests/baselines/*.png` — eyeball every render.
   Anything collapsed to default-grey plastic indicates a substrate
   miss for that material; investigate before committing.
2. `git add bake/preview/tests/baselines/*.png` and commit with a
   message that records which substrate revision the baselines were
   generated against (e.g. `gerchowl/mat-vis-tst@v2026.04.99-tst-full-369`).

## Verify

Re-run without `MAT_VIS_UPDATE_BASELINES`:

```bash
MAT_VIS_HF_BASE="https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve" \
MAT_VIS_TAG=v2026.04.99-tst-full-369 \
MAT_VIS_SKIP_VISUAL=0 \
  uv run --with playwright --with pillow --with-editable clients/python \
  pytest bake/preview/tests/test_visual_regression.py -v
```

All visual tests should pass with RMS ≈ 0 (same machine, same renderer
state, same substrate).

## Naming

- `bernhard_textured_<label>_<tier>.png` — textured-source materials
  from `BERNHARD_TEXTURED`. Default tier is `1k`; override via
  `MAT_VIS_VISUAL_TIER`.
- `bernhard_scalar_<label>.png` — scalar-only materials from
  `BERNHARD_SCALAR_ONLY` (physicallybased.info).

Materials missing from a given substrate snapshot are skipped (not
failed) and produce no baseline.

## Tolerance

`_visual_compare.py` uses an RMS tolerance of 8.0 / 255 per channel —
enough to absorb cross-platform Chromium / SwiftShader noise but tight
enough that a material rendering as default-grey instead of gold (Δ ≈ 50+)
fails the comparison.
