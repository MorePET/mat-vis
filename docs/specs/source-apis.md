# Upstream Source API Reference

> **Canonical enums.** The authoritative definitions for source names,
> category values, channel names, and tier names live in the JSON
> Schemas (`index-schema.json`, `rowmap-schema.json`,
> `release-manifest-schema.json`). If this document or the ADRs
> diverge from the schemas, the schemas win.

Best-effort documentation of each upstream material source's API.
The implementation must verify endpoints at runtime, handle pagination and errors gracefully,
and log cleanly if a source is unreachable.

---

## ambientcg

| Field            | Value |
|------------------|-------|
| Website          | <https://ambientcg.com> |
| License          | **CC0-1.0** (all materials) |
| Auth required    | No |
| Rate limiting    | Unknown; appears generous for bulk downloads |

### Discovery

```
GET https://ambientcg.com/api/v2/full_json?type=Material&limit=100&offset=0
```

- Returns JSON with a top-level array/list of material entries.
- Paginate by incrementing `offset` by `limit` until the returned list is empty or shorter than `limit`.
- Each entry includes: `assetId`, `displayName`, `tags`, `category`, `downloadFolders` (keyed by resolution), and `dataTypeName`.

### Download

- Each material's download links point to **ZIP files** per resolution (1K, 2K, 4K, 8K).
- ZIP contents: `.mtlx` file + flat PNG textures (`*_Color.png`, `*_NormalGL.png`, `*_Roughness.png`, `*_AmbientOcclusion.png`, `*_Displacement.png`).
- Download URL pattern from the API response: follow `downloadFolders.<resolution>.downloadFiletypeCategories.zip.downloads[0].fullDownloadPath`.

### Notes

- The `type=Material` filter excludes HDRIs and 3D models.
- Some older materials may lack `.mtlx`; the implementation should handle missing files within the ZIP.

---

## polyhaven

| Field            | Value |
|------------------|-------|
| Website          | <https://polyhaven.com> |
| License          | **CC0-1.0** (all assets) |
| Auth required    | No |
| Rate limiting    | Public API, no key needed |

### Discovery

```
GET https://api.polyhaven.com/assets?t=textures
```

- Returns a JSON object keyed by asset slug (e.g. `"brick_wall_006"`).
- Each value contains: `name`, `tags`, `categories`, `date_published`.
- Filter the response for type `textures` (already done by query param `t=textures`).

### Per-material detail

```
GET https://api.polyhaven.com/files/<asset_slug>
```

- Returns a JSON object keyed by resolution (e.g. `"1k"`, `"2k"`), each containing map types.
- Structure: `response[resolution][map_type][format]` where format is `"png"`, `"jpg"`, `"exr"`, etc.
- Each leaf has a `url` field with the direct download link.

### Download

- **Individual file downloads** (not ZIPs). One HTTP GET per map per resolution.
- `.mtlx` files: available at `https://api.polyhaven.com/files/<slug>` under the `"gltf"` or `"blend"` keys, or constructed via known URL patterns. Verify at implementation time.
- **EXR format**: displacement maps are often EXR only. The pipeline should convert EXR to PNG or skip if not needed.

### Map name mapping

| Polyhaven key  | Normalized channel |
|----------------|--------------------|
| `diffuse`      | `color`            |
| `nor_gl`       | `normal`           |
| `rough`        | `roughness`        |
| `metal`        | `metalness`        |
| `ao`           | `ao`               |
| `disp`         | `displacement`     |
| `emission`     | `emission`         |

---

## gpuopen (AMD GPUOpen MaterialX Library)

| Field            | Value |
|------------------|-------|
| Website          | <https://matlib.gpuopen.com> |
| License          | **TBV** (to be verified per material; check each material's metadata) |
| Auth required    | No (currently) |
| Rate limiting    | Unknown |

### Discovery

```
GET https://api.matlib.gpuopen.com/api/packages?limit=100&offset=0
```

- Returns a JSON object with a `results` array of material packages.
- Paginate by incrementing `offset` by `limit`.
- Each result includes: `id`, `title`, `tags`, `category`, `thumbnailUrl`, `createdAt`, `updatedAt`.

### Download

- Per-package detail (to get download URL):
  ```
  GET https://api.matlib.gpuopen.com/api/packages/<package_id>
  ```
- Downloads are **ZIP packages** containing `.mtlx` + texture files.
- Download URL is typically in `response.downloadUrl` or a similar field; verify at implementation time.

### Notes

- Some materials have **layered MaterialX graphs** (not just flat texture references). These require MaterialX baking to produce flat PNG textures.
- The implementation should detect layered graphs (presence of `<nodegraph>` elements beyond simple `<tiledimage>` references) and either bake them or skip with a warning.
- Texture filenames within ZIPs are not standardized; use the `.mtlx` file's `<input file="...">` references to locate them.

---

## physicallybased.info

| Field            | Value |
|------------------|-------|
| Website          | <https://physicallybased.info> |
| License          | **CC0-1.0** |
| Auth required    | No |
| Rate limiting    | None known |

### Discovery

```
GET https://api.physicallybased.info/materials
```

- Returns a JSON array of all materials in a single response (no pagination needed; dataset is small).
- Each entry includes scalar properties only:
  - `name` (string)
  - `category` (string)
  - `density` (number, kg/m^3)
  - `ior` (number)
  - `color` (array of 3 floats, RGB in [0,1])
  - `metalness` (number, 0 or 1)
  - `roughness` (number, 0-1)
  - `specularColor` (array of 3 floats, optional)
  - `transmissionColor` (array of 3 floats, optional)
  - Various other physical properties (subsurface, thin film, etc.)

### Download

- **No textures to download.** This source provides scalar/analytic material properties only.
- Materials from this source will have `available_tiers: []` and `maps: []` in the index.
- Useful for: IOR lookups, representative color computation, metalness/roughness ground truth.

> **No Parquet output.** physicallybased materials are scalar-only — they appear in `index/physicallybased.json` but never in any Parquet file or rowmap. The `available_tiers` and `maps` arrays are empty. Consumers access these materials purely through the JSON index.

### Color conversion

- The `color` field is an RGB tuple in [0,1] range. Convert to `#RRGGBB` hex:
  ```python
  hex_color = "#{:02X}{:02X}{:02X}".format(
      int(round(r * 255)),
      int(round(g * 255)),
      int(round(b * 255)),
  )
  ```

---

## Cross-source implementation notes

1. **Pagination**: ambientcg and gpuopen use `limit`/`offset`. polyhaven returns everything in one call. physicallybased returns everything in one call.
2. **Error handling**: All fetchers should retry transient HTTP errors (429, 5xx) with exponential backoff (3 retries, 1s/2s/4s delays). Log and skip individual materials on persistent failure.
3. **Format normalization**: Upstream map names vary. Each fetcher must normalize to the canonical channel names: `color`, `normal`, `roughness`, `metalness`, `ao`, `displacement`, `emission`.
4. **Category normalization**: Upstream categories are freeform strings. Each fetcher must map them to the canonical set: `metal`, `wood`, `stone`, `fabric`, `plastic`, `concrete`, `ceramic`, `glass`, `organic`, `other`.
5. **watch.yml behavior**: The watch workflow should log clearly which sources succeeded/failed and exit 0 even if a source is down (to avoid blocking the entire pipeline). Failed sources should be retried on the next scheduled run.

---

## Error handling and partial failures

### Fetch failures

If a single material fails to download (HTTP error, corrupt ZIP,
missing .mtlx inside ZIP), the baker:

1. Logs the failure with material ID, source, and error.
2. Skips the material.
3. Continues processing remaining materials.
4. At the end, exits with code 0 if ≥95% of materials succeeded,
   code 1 otherwise.

The Parquet and rowmap only contain successfully baked materials.
The index JSON includes all materials (including failed ones),
with a `"status": "failed"` field on entries that couldn't be
baked. This lets consumers see what exists upstream even if the
bake failed.

### Bake failures

Same policy — skip, log, continue. Common causes:
- MaterialX graph uses unsupported node types (gpuopen layered
  materials without materialx installed)
- EXR file corrupt or unsupported pixel format
- .mtlx references a texture file not present in the download

### Source-level failures

If an entire source is unreachable (API down, DNS failure), the
baker:

1. Logs the failure.
2. Skips the entire source.
3. Exits with code 1 (source-level failure is always an error).

The watch.yml workflow handles this gracefully — if a source is
down during the daily poll, it logs and exits cleanly without
opening a PR. No partial-release.
