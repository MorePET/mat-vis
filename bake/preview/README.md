# Thumb-tier bake pipeline (mat-vis#361)

Renders one ~256×256 PBR thumbnail per material per release, using
bernhard's [`create_shader_ball`](#geometry-shaderballglb) geometry +
the same Three.js scene (`RoomEnvironment` IBL + ACES) that ocp_vscode
ships with. See ADR-0014 for the architectural decision.

## Layout

```
bake/preview/
├── README.md                         # this file
├── thumb_render.html                 # the renderer (Three.js + GLTFLoader)
├── assets/
│   └── shader_ball.glb               # vendored geometry (~4.5MB, Apache-2.0)
└── utils/
    ├── bake_shader_ball.py           # dev script that produces shader_ball.glb
    └── requirements-dev.txt          # build123d (NOT a runtime dep)
```

## Geometry: `shader_ball.glb`

Vendored from
[`bernhard-42/vscode-ocp-cad-viewer`](https://github.com/bernhard-42/vscode-ocp-cad-viewer/blob/main/ocp_vscode/utils.py)
— `create_shader_ball()`, Apache-2.0, Copyright 2025 Bernhard Walter.
Procedural build123d compound (4 children: hollow sphere + display
sphere + cylindrical base + central sphere), bbox ~22×22×22mm.

Why vendor instead of generating at bake time:
- build123d (~200MB with cadquery + OCP) is too heavy for the bake
  container; the GLB is ~4.5MB and ships once
- License-clear: Apache-2.0 attribution preserved in `bake_shader_ball.py`
  and this README
- Reproducible: anyone with `bake/preview/utils/requirements-dev.txt`
  can regenerate the GLB

### Regenerating

```sh
uv pip install -r bake/preview/utils/requirements-dev.txt
uv run python bake/preview/utils/bake_shader_ball.py
```

This rewrites `bake/preview/assets/shader_ball.glb`. Re-run when:
- bernhard updates `create_shader_ball()` upstream
- We need to tweak parameters (radius, fillet, plinth size)
- License audit needs to verify provenance

## Renderer: `thumb_render.html`

Three.js v0.170 single-material renderer that loads `shader_ball.glb`,
applies one `MeshPhysicalMaterial` (built from `to_threejs(vis)`
output), renders at 1024² supersample → caller downsamples to 256² with
Lanczos.

Scene matches bernhard's mat-vis#285 reference (the "should look like"
image):
- Background: `#1a1a2e` (dark navy)
- IBL: `RoomEnvironment` via `PMREMGenerator`
- Tone-mapping: `ACESFilmicToneMapping`, exposure 1.0
- Output color space: sRGB
- Geometry: `shader_ball.glb` scaled m→mm, rotated Z-up→Y-up

URL contract:

```
thumb_render.html?spec=<spec.json>
```

Where `spec.json` is either:

```json
{"threejs": {<MeshPhysicalMaterial params>}}
```

or (for compatibility with grid renderers):

```json
{"items": [{"label": "...", "threejs": {...}}]}
```

The Python orchestrator (TBD: `bake/preview/run.py`) drives this via
Playwright + headless Chromium with SwiftShader software GL, the same
stack pymat uses in CI for visual regression.

## Conventions

### Per-material output

`bake/preview/run.py` (forthcoming) walks `client.index(source)` for each
configured source, builds a `to_threejs(vis)` payload per material,
renders one PNG per material, writes to
`textures/<source>/<material_id>/thumb.png` for HF substrate upload.

### What to render from

Render from the **largest texture tier available** per material
(typically `1k`); `MeshPhysicalMaterial` mipmaps down internally for
the 1024² render. Smaller source tiers are noisier on metals (specular
aliasing); larger tiers are wasted bandwidth at 256² output.

### Determinism

Cache thumbs keyed by `(material_id, source_tier_hash, scene_settings_hash)`.
Re-bake only when one of those changes. SwiftShader is byte-deterministic
across CI runs; chase parity at the input-hash layer, not output bytes
(per PBR specialist verdict in the spike).

## References

- ADR-0014 — `docs/decisions/0014-thumb-tier-renderer-choice.md`
- mat-vis#361 — substrate-side thumb tier (this work)
- mat-vis#362 — client-side `VisAsset.thumb` API (already shipped via PR #363)
- mat-vis#285 — bernhard's quality reference (the "should look like" image)
- mat-vis#376 — operational: trigger prod re-bake to clear v2026.04.2 stale catalog
