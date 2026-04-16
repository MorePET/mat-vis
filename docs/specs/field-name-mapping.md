# Field name mapping across ecosystems

Canonical mapping between py-mat, mat-vis, Three.js, and glTF.
Each ecosystem uses its own naming conventions. Adapters translate
between them using this table.

## Scalars

| Concept | py-mat (`properties.pbr`) | mat-vis (index JSON) | Three.js | glTF |
|---|---|---|---|---|
| Metalness | `metallic` | `metalness` | `metalness` | `metallicFactor` |
| Roughness | `roughness` | `roughness` | `roughness` | `roughnessFactor` |
| Base color | `base_color` (RGBA float tuple) | `color_hex` (#RRGGBB string) | `color` (hex int) | `baseColorFactor` (RGBA) |
| IOR | `ior` | `ior` | `ior` | `ior` (KHR_materials_ior) |
| Transmission | `transmission` | — | `transmission` | `transmissionFactor` |

## Texture channels

| Channel | mat-vis (Parquet column / rowmap key) | Three.js property | glTF texture |
|---|---|---|---|
| Color / albedo | `color` | `map` | `baseColorTexture` |
| Normal | `normal` | `normalMap` | `normalTexture` |
| Roughness map | `roughness` | `roughnessMap` | packed in `metallicRoughnessTexture` (G) |
| Metalness map | `metalness` | `metalnessMap` | packed in `metallicRoughnessTexture` (B) |
| Ambient occlusion | `ao` | `aoMap` | `occlusionTexture` |
| Displacement | `displacement` | `displacementMap` | — (extension) |
| Emission | `emission` | `emissiveMap` | `emissiveTexture` |

## Naming rationale

- **py-mat uses `metallic`**: established API, aligns with glTF's
  root term (`metallicFactor`), matches the adjectival form used
  in materials science.
- **mat-vis uses `metalness`**: matches Three.js property names and
  upstream source naming (ambientcg, polyhaven both use "metalness").
- Adapters translate. Neither repo changes its naming.

## glTF packed textures

glTF packs metalness (B channel) and roughness (G channel) into a
single `metallicRoughnessTexture`. The `to_gltf` adapter must
composite mat-vis's separate `metalness` and `roughness` PNGs into
one packed image. If only scalar values exist (no textures), glTF
uses `metallicFactor` / `roughnessFactor` directly.

## Source: this table

This is the **canonical** mapping. If adapter code or documentation
elsewhere diverges, this file wins.
