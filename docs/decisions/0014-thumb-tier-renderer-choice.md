# ADR-0014: thumb-tier renderer — vendor pymat's headless-Three.js pipeline

- Status: Proposed
- Date: 2026-05-08
- Deciders: @gerchowl
- Related: #361 (substrate-side thumb tier), #362 (client-side discovery surface), ADR-0011 (curated + upstream mirror; `to_threejs` is the shared adapter)
- Milestone: [v0.6.0 — sharded pipeline + Dagger](https://github.com/MorePET/mat-vis/milestone/2)

## Context

mat-vis#361 wants a baked "thumb" tier — one ~256² PNG per material
showing a rendered preview, baked once per release. mat-vis#362 ships
the client-side `VisAsset.thumb` API with a named-tier alias that
will resolve to this baked tier when staged.

The renderer choice is non-trivial. mat-vis bake runs in
Dagger-orchestrated containers on GHA. ~5000 materials per release.
Three structural axes:

- **Quality**: PBR with image-based lighting (IBL) is most of what
  makes metals/dielectrics look real. Without IBL, surfaces look
  "stale" — flat 3-point lighting can't approximate the environment
  reflections that define a metal.
- **Speed**: bake is release-only (not per-PR), but excessive
  runtime makes the release pipeline unpleasant.
- **Integration debt**: every renderer choice carries a tail of
  install / shader / state-management complexity.

A sibling repo (`MorePET/py-mat`, downstream consumer) already has a
working preview-rendering pipeline at
[`py-mat/scripts/generate_previews.py`](https://github.com/MorePET/py-mat/blob/main/scripts/generate_previews.py)
- [`py-mat/tests/material_preview.html`](https://github.com/MorePET/py-mat/blob/main/tests/material_preview.html)
that pymat ships in CI to render its catalog page. It uses Three.js
with `MeshPhysicalMaterial` + `RoomEnvironment` PMREM + ACES tone-
mapping, driven by Playwright + headless Chromium + SwiftShader
software GL. Same `to_threejs(scalars, textures)` adapter mat-vis-
client also exposes (canonical impl in mat-vis-client; pymat consumes).

## Decision

**Vendor pymat's browser-based renderer into mat-vis bake.** Copy
`material_preview.html` and a stripped-down `generate_previews.py`
into `mat-vis/bake/preview/`; adapt the orchestration to walk the
mat-vis index instead of pymat's `_CATEGORY_BASES`.

Rationale (in priority order):

1. **Quality**: empirically verified equivalent to what pymat ships
   today. Visual A/B against three alternatives confirmed full PBR +
   IBL + ACES looks substantially better than the alternatives at
   thumbnail size — metals especially.
2. **Lowest integration debt**: zero new rendering code. The
   renderer is already running in pymat's CI. Container delta is
   ~500MB (Chromium), but bake containers already include heavy
   image-processing tooling.
3. **DRY**: same `to_threejs` adapter join, same Three.js scene
   setup. Two-renderer drift (mat-vis bake vs pymat interactive)
   collapses to a single Three.js codebase.
4. **Cross-arch determinism**: SwiftShader is CPU-side, byte-stable
   across amd64/arm64 (verified by pymat's CI matrix).
5. **Shape per pymat convention**: cube for solids
   (`metals`/`plastics`/`ceramics`/etc. — most of mat-vis), sphere
   for fluids (`liquids`/`gases`). Catalog-page continuity for
   downstream pymat users.

## Alternatives considered

A 3-angle structured spike (PBR specialist / CI infra / cross-repo
architecture) plus empirical benches retired three other paths:

### A. pyrender + osmesa (Python-native, no IBL)

- **Speed**: ✅ ~156ms/material × 5000 = ~13min single-threaded
- **Quality**: ❌ No image-based lighting. 3-point directional + ambient
  only. Metals look "stale" — no environment reflections.
- **Verdict**: Insufficient PBR quality for material discovery.

### B. Three.js in Node + headless-gl (`gl` npm)

- **Speed**: ✅ ~310ms/material × 5000 = ~26min single-threaded
- **Quality**: ✅ Same Three.js + PMREM + ACES as the browser stack
- **Integration debt**: ⚠️ Required pinning Three.js ≤0.162 (later
  versions need WebGL 2; `gl` is WebGL 1 only). State-management bug
  observed across multiple renders in headless-gl: first material
  rendered correctly, materials 2–10 produced near-flat output
  despite different inputs. Bug not fully diagnosed in spike
  timeframe; estimated 1–4 hours of focused debugging.
- **Verdict**: Viable but carries integration debt the
  vendor-pymat path avoids. Documented for future reference; can be
  revisited if browser-stack runtime becomes painful.

### C. Open3D (`rendering.OffscreenRenderer` + IBL)

- **Promised**: IBL via Filament's prefiltered cubemap (same
  algorithm as Three.js's PMREM); ~150ms/material expected; ~120MB
  wheel.
- **Empirical block**: Open3D ships no Linux/arm64 wheels; macOS
  native errors with `EGL Headless not supported`; podman amd64 via
  QEMU forbidden by local dev policy. Could not bench locally.
- **Verdict**: Plausible per agent survey but unverifiable in
  spike. Defer; not load-bearing given Decision is already viable.

### D. Filament-py / Mitsuba 3 / Panda3D + simplepbr

Surveyed by agent; cut for various reasons: filament-py has no
maintained Python binding (multi-week pybind11 work); Mitsuba 3
delivers full path-traced quality at 400–800ms/material (blows
budget); Panda3D + simplepbr requires building Panda3D from source
against osmesa (same class of integration debt as Node+gl).

## Consequences

### Positive

- Bake-time renderer reuses a battle-tested codebase already shipping
  in pymat CI. Zero new shader code.
- `VisAsset.thumb` (mat-vis#362) will return ~30–60KB sphere/cube
  renders with full PBR once #361 ships.
- Single source of truth for PBR rendering across both repos.

### Negative

- ~500MB Chromium layer added to bake container. Acceptable: bake
  runs once per release, not per PR.
- ~105min single-threaded for 5000 materials at the empirically
  measured 1.27s/material. Acceptable for release-only step;
  sharding via GHA matrix (P2) brings it under 30min if needed.
- Bake container needs Playwright + Chromium + (vendored) Three.js
  bundle for hermeticity (no JSDelivr fetch in bake).

### Neutral

- Three.js-version drift between mat-vis bake and pymat is a
  monitoring concern. Pin Three.js version explicitly in vendored
  HTML; document the rendering parameters (camera, exposure,
  tonemap, env map) in `mat-vis/bake/preview/RENDERING.md`.

## Empirical data

Bench harness: 10 gpuopen materials at tier=1k, downsampled to
256² via PIL Lanczos. Measured on M-series Mac (arm64).

| Stack | Steady-state | 5000 projection | Quality |
|---|---|---|---|
| **Browser + Chromium (vendored)** | **1.27s** | **105 min** | ✅ Full Three.js PBR |
| pyrender (textured, 3-point) | 156ms | 13min | ❌ No IBL |
| Node+gl (Three.js@0.162) | 310ms* | 26min | ⚠️ State bug |
| Open3D | n/a | n/a | unverifiable locally |

\*First render only; subsequent renders in the bench broke due to a
Three.js + headless-gl state-leakage bug not fully diagnosed.

PBR specialist's scene-setup deltas (apply as P1 polish, not P0
blockers):

- Studio HDRI (Poly Haven `studio_small_09_2k.hdr`, CC0, 1.5MB) in
  place of Three's stock `RoomEnvironment` — better metallic
  distinguishability via higher-frequency specular content.
- Tighter camera at (2.4, 1.6, 2.4) with 28° FOV — sphere fills
  ~85% of frame.
- Render at 1024² supersample → 256² Lanczos — kills specular
  aliasing + roughness banding.

## P0 / P1 / P2 scope

### P0 (this lands first)

- [ ] Vendor `material_preview.html` + Playwright orchestrator into
  `mat-vis/bake/preview/`
- [ ] Vendor Three.js bundle (no JSDelivr fetch in bake)
- [ ] Adapt orchestrator to walk mat-vis index, write
  `<source>/<material>/thumb.png`
- [ ] Catalog: list `"thumb"` in `available_tiers` for entries that
  have it baked
- [ ] HF upload picks up `thumb.png` (same path convention)

### P1 (PBR-quality polish)

- [ ] Vendored studio HDRI (Poly Haven CC0)
- [ ] Camera framing: (2.4, 1.6, 2.4) at 28° FOV
- [ ] Supersample 1024 → 256 Lanczos
- [ ] `RENDERING.md` documenting parameters

### P2 (operational)

- [ ] GHA matrix sharding for bake runtime under 30min
- [ ] Post-bake validator: every textured-source entry has
  `"thumb"` ∈ `available_tiers` (extends mat-vis#344, mat-vis#349)

## References

- mat-vis#361 — substrate-side thumb tier issue
- mat-vis#362 — client-side discovery surface (shipped in PR #363)
- ADR-0011 — `to_threejs` adapter contract (the join point)
- pymat repo `MorePET/py-mat` — origin of the vendored renderer
