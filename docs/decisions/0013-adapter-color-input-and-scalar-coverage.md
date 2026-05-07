# ADR-0013: adapter canonical color input + colorspace discipline + scalar coverage

- Status: Proposed
- Date: 2026-05-07
- Deciders: @gerchowl
- Related: ADR-0011 (curated + upstream mirror; defines `pbr.color_rgb` shape upstream)
- Milestone: [#3 — adapters: py-mat downstream parity](https://github.com/MorePET/mat-vis/milestone/3)
- Resolves under one decision: #298, #302, #303, #304, #305

## Context

`mat-vis-client` ships three adapters from a unified `scalars` dict
in `clients/python/src/mat_vis_client/adapters.py`:

| Adapter | Output | Color field |
|---|---|---|
| `to_threejs` | dict for `new THREE.MeshPhysicalMaterial(...)` | `color` (currently hex int) |
| `to_gltf` | glTF 2.0 material JSON | `pbrMetallicRoughness.baseColorFactor` |
| `export_mtlx` | MaterialX 1.38 XML (UsdPreviewSurface) | currently *missing* on the scalar path |

A spike-loop review of #298 (proposing a hex-string default for
`to_threejs`'s `color`) surfaced four substrate issues that compose
poorly if patched piecemeal:

### Finding 1 — The canonical input shape is lossy.

The five-key allowlist (`metalness`, `roughness`, `color_hex`, `ior`,
`transmission`) silently drops keys py-mat documents on its public
`Vis` surface: `emissive` (RGB), `clearcoat` (float), `metallic`
(glTF-spec name for `metalness`), and the alpha channel of
`base_color` (RGBA in py-mat 3.10+). See #302, #303, #304.

### Finding 2 — sRGB hex with no colorspace discipline.

`_color_hex_to_rgba` at `adapters.py:81-85` does naive
`int(byte, 16) / 255.0`:

```python
def _color_hex_to_rgba(hex_str: str) -> list[float]:
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return [r / 255.0, g / 255.0, b / 255.0, 1.0]
```

That output is sRGB-encoded floats. It is then assigned directly to
glTF `baseColorFactor` at `adapters.py:187`. **glTF 2.0 spec requires
linear-space `baseColorFactor`** (Khronos glTF 2.0 §3.9.2). A
baker-emitted `#bfbfc4` (sRGB ≈ 0.749) becomes `[0.749, 0.749,
0.768, 1.0]` linear in the file — the renderer interprets that as
linear 0.749, which displays as sRGB ≈ 0.886. Materials render
**systematically too bright** in any spec-compliant glTF viewer.
This is a latent correctness bug independent of #298 but
foundational to it: shipping a `color_format="tuple"` output that
emits the same wrong-space float locks the bug into a public kwarg.

### Finding 3 — MaterialX drops the base color entirely on the scalar path.

`_build_mtlx_tree` at `adapters.py:332-343` emits scalar inputs for
`roughness`, `metalness`, `ior` only. `color_hex` is not synthesized
into a `<color3>` / `<color4>` `diffuseColor` input on
UsdPreviewSurface. Color flows through *only* if a color texture is
present (line 308 sets `colorspace="srgb_texture"` on the
`<image>`). A material that has only a `color_hex` scalar — common
for PBR scalar-only entries like Stainless Steel — exports an mtlx
that has no diffuse color at all. Renderers fall back to white.

### Finding 4 — `to_threejs`'s `color` int default is un-Pythonic.

The originating concern of #298 (py-mat #99, Bernhard / build123d):
emitting `12566468` instead of `"#bfbfc4"` makes REPL inspection,
log lines, and JSON round-trips opaque. Three.js
`MeshPhysicalMaterial` accepts both, so the emit is JS-lossless.

### Finding 5 — `export_mtlx` requires consumer-side path sanitization.

`material_name` is taken verbatim into `output_dir / f"{name}.mtlx"`
at `adapters.py:471`. Names with spaces, slashes, or path-traversal
sequences land on disk verbatim. Every downstream wrapper sanitizes
independently. See #305.

### Finding 6 — naming friction at the spec/library boundary.

Three.js `MeshPhysicalMaterial.metalness` vs glTF
`metallicFactor` / `KHR_materials.metallic`. py-mat picked the
glTF-spec name (`metallic`) on its `Vis` field. Adapters accept only
`metalness`. Every wrapper renames at the boundary. See #303.

## Decision

A single, coherent input schema with explicit colorspace contracts
and full PBR-scalar coverage. Land in three coordinated phases.

### 1. Canonical color input is **linear RGBA float-4**.

```
scalars["base_color_linear"]: tuple[float, float, float, float]   # NEW canonical
scalars["color_rgba"]:        tuple[float, float, float, float]   # legacy alias (sRGB)
scalars["color_hex"]:         str "#RRGGBB"                       # legacy alias (sRGB)
```

Adapters resolve in priority order. Conflicting keys with non-equal
values raise `ValueError`. New colorspace helpers:

```python
def _srgb_to_linear(c: float) -> float: ...     # IEC 61966-2-1 piecewise
def _linear_to_srgb(c: float) -> float: ...
def _resolve_base_color(scalars) -> tuple[float, float, float, float] | None:
    """Returns linear RGBA, or None if no color key present.
       sRGB inputs are de-gammaed at the boundary."""
```

`base_color_linear` is the future-canonical key py-mat will pass.
The two sRGB aliases stay supported indefinitely (legacy data, hex
literals in user code) but de-gamma on entry — colorspace-correct
from any input.

### 2. Per-adapter output contracts (spec-pinned).

| Adapter | Output | Source |
|---|---|---|
| `to_threejs` | `color`: `"#RRGGBB"` sRGB hex string by default; `color_format` kwarg for `"int"` opt-out | Three.js r152+ ColorManagement treats hex as sRGB and de-gammas internally — lossless |
| `to_gltf` | `pbrMetallicRoughness.baseColorFactor`: linear float-4 | glTF 2.0 §3.9.2 |
| `export_mtlx` | `<input name="diffuseColor" type="color3" value="r,g,b"/>` linear float-3 on the surface shader (NEW); existing `srgb_texture` colorspace tag stays for the texture path | MaterialX 1.38 / UsdPreviewSurface diffuseColor is linear by convention |

`color_format` is **`to_threejs`-only**. Other adapters have
spec-pinned formats with no genuine choice. Asymmetric kwarg surface
mirrors asymmetric reality.

`color_format` enum: `Literal["hex", "int"]`. **No `"tuple"`** — its
sRGB-vs-linear ambiguity is the same hazard that produced Finding 2.
Float output exists at `to_gltf` (linear) and via direct
`_resolve_base_color` access for power users who need the raw value.

### 3. Scalar coverage expands to mirror Three.js MeshPhysicalMaterial + glTF 2.0.

Newly accepted input keys:

| Key | Type | Three.js | glTF | MaterialX |
|---|---|---|---|---|
| `emissive` | linear RGB float-3 | `emissive` (Color, hex/array) | `emissiveFactor` (core spec) | `<input name="emissiveColor" type="color3">` |
| `clearcoat` | float [0,1] | `clearcoat` | `KHR_materials_clearcoat.clearcoatFactor` | n/a (UsdPreviewSurface lacks clearcoat — out of scope) |
| `metallic` | float [0,1] alias for `metalness` | resolves to `metalness` | resolves to `metallicFactor` | resolves to `metallic` input |

Conflict rule: setting both `metallic` and `metalness` (or both
`base_color_linear` and `color_hex`) with non-equal values raises
`ValueError`.

### 4. `export_mtlx` sanitizes `material_name` internally.

```python
safe = re.sub(r"[^A-Za-z0-9_-]", "_", material_name).strip("_") or "material"
```

Path traversal blocked (`"../escape"` → `"escape"`). Empty / all-
stripped names fall back to `"material"`. Removes the duplicated
sanitize logic from every consumer wrapper.

### 5. Versioning + rollout.

| Version | Change |
|---|---|
| **0.6.5** | Land #300 (delete legacy duplicate). Introduce `color_format` kwarg on `to_threejs` defaulting to `"int"` (no behavior change). Emit `DeprecationWarning` when `color_format` is unset. Add `_srgb_to_linear` helpers + `_resolve_base_color`. Accept `metallic` / `emissive` / `clearcoat` / `color_rgba` / `base_color_linear` as inputs (all additive). Sanitize `material_name`. Add `<color3>` `diffuseColor` to MTLX scalar path. **No output-shape break.** |
| **0.7.0** | Flip `color_format` default to `"hex"`. Fix `to_gltf` `baseColorFactor` to emit linear (sRGB→linear at the boundary) — this is a **correctness fix that is also a behavior change** for any glTF consumer who was compensating for the old over-bright values; called out explicitly in CHANGELOG. py-mat `>=0.7.0` adopts `base_color_linear` as canonical. |

py-mat tracks: closes py-mat #99 on the 0.7.0 bump.

## Consequences

### Wins

- One file (`adapters.py`) becomes colorspace-correct end to end.
- py-mat's `Vis` field set (`base_color`, `emissive`, `clearcoat`,
  `metallic`) round-trips through all three adapters lossless.
- glTF output stops rendering systematically too bright in
  spec-compliant viewers (gltf-viewer, Babylon.js, Filament).
- MaterialX scalar-only materials get a working diffuse color.
- `material_name` sanitization, alpha preservation, and
  metallic-alias bikeshedding move from N consumers to one substrate.

### Costs

- 0.7.0 glTF output is **numerically different** from 0.6.x for any
  scalar-with-base-color material. Renderers that were "compensating"
  via gamma settings will need adjustment. Document explicitly with
  before/after examples in the CHANGELOG.
- Three pinned-int test sites need updating regardless
  (`tests/test_client.py`, `test_client_legacy.py`,
  `test_adapters_metalcolormap.py`). Parametrize across `color_format`.
- `mat_vis_client_standalone.py` (the bundled standalone client)
  ships its own copy of adapter logic — needs a synchronized update
  pass each phase, gated by the existing standalone-sync hook.

### Rejected alternatives

- **Ship #298 standalone**: bakes sRGB-keyed `color_format` kwarg
  on top of a substrate that mishandles colorspace; the eventual
  fix is a second breaking change.
- **Open-callable `color_format=fn`**: defeats the wire-format
  guarantee. Power users post-process with `fn(out["color"])`.
- **Cross-adapter `color_format` kwarg**: glTF and MTLX have no
  genuine output choice — fake symmetry.
- **`base_color` as the canonical key (py-mat's name)**: ambiguous
  on colorspace. Explicit `_linear` suffix removes the ambiguity at
  the cost of one extra word in the canonical key.

## Upgrade triggers

Revisit this ADR when:

- glTF spec changes the `baseColorFactor` colorspace (extremely
  unlikely; would be a major-version glTF break).
- Three.js color management changes again post-r152 — verify the
  hex-string default still round-trips lossless.
- A fourth adapter target appears (USD, FBX, Renderman) — the
  per-adapter contract pattern should accommodate it without
  extending `color_format`.
- mat-vis-client moves to 1.0 — at that point the legacy `color_hex`
  / `color_rgba` aliases can be considered for removal.

## References

- #298, #302, #303, #304, #305 (milestone #3)
- #299 (#290 follow-up — neutral-multiplier convention pattern)
- py-mat #99 (Bernhard / build123d — original `to_threejs` color
  ergonomics report)
- Khronos glTF 2.0 spec §3.9.2 (`baseColorFactor` linear)
- IEC 61966-2-1 (sRGB transfer function)
- Three.js r152 ColorManagement migration notes
- MaterialX 1.38 spec (UsdPreviewSurface `diffuseColor` input type)
- `clients/python/src/mat_vis_client/adapters.py:81-85, 130, 187,
  332-343, 471` (current behavior, anchored)
