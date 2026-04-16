# mat-vis

**Pre-baked PBR material indexes + textures** for the [MorePET/mat][mat]
family: `py-materials`, `rs-materials`, and the build123d integration.

Consolidates the four major open MaterialX / PBR libraries
(ambientcg, polyhaven, gpuopen, physicallybased.info) into a
single versioned, auditable, language-agnostic data distribution.

## Status

🚧 **Bootstrap phase.** Design is captured in
[`docs/decisions/`](docs/decisions/); data pipelines are not yet
implemented. See the ADRs for the architecture rationale.

Expected first data release: once the build pipeline and
watch-and-PR workflow are in place. No ETA yet.

## Why this exists

The [MorePET/mat][mat] ecosystem needs rendering-grade PBR data for:

- **build123d shapes** rendered in `ocp_vscode` (via `threejs-materials`)
- **CAD/MC pipelines** that want material appearance alongside physics
- **Web viewers** (`three-cad-viewer` and others) consuming PBR JSON
- **Headless renderers** that need offline-available textures

The source libraries aren't directly consumable by all of these —
`ambientcg` / `polyhaven` / `gpuopen` ship MaterialX `.mtlx` files
with procedural graph baking required before rendering;
`physicallybased.info` is scalar-only (no textures). `mat-vis`
bakes once at data-publish time and distributes the flat output,
so downstream consumers never need the MaterialX SDK and its
compile-time dependencies.

## Design

Five ADRs under [`docs/decisions/`](docs/decisions/) capture the
architecture:

1. [**ADR-0001**](docs/decisions/0001-storage-architecture-json-index-parquet-textures.md)
   — Two-tier storage: diffable JSON indexes in-repo, Parquet
   texture bundles as Release assets.
2. [**ADR-0002**](docs/decisions/0002-hosting-github-releases-watch-and-pr.md)
   — GitHub Releases for hosting (free, versioned, CDN-backed);
   daily watch-and-PR flow for upstream change detection.
3. [**ADR-0003**](docs/decisions/0003-resolution-tiers-and-partitioning.md)
   — Per (source × resolution tier) Parquet files; category
   partitioning at 4K+ to fit GitHub's 2 GB per-asset limit.
4. [**ADR-0004**](docs/decisions/0004-access-modes-lazy-local-cache-default.md)
   — Clients default to lazy local caching under
   `~/.cache/mat-vis/`; eager prefetch and pure-remote modes are
   opt-in.
5. [**ADR-0005**](docs/decisions/0005-sql-shim-embedded-in-clients.md)
   — ~~SQL ergonomics via a small DuckDB URL-table shim embedded
   in each client.~~ **Superseded**: DuckDB/pyarrow users query
   the Parquet files directly; no client-side shim needed.

Typical user journeys are documented in [`docs/user-stories.md`](docs/user-stories.md).

## Intended usage (once released)

```python
from pymat import Material

# Default: lazy local cache, range-reads on first access.
steel = Material("ambientcg/Metal064", tier="1k")  # first call: HTTP range read + cache write
steel = Material("ambientcg/Metal064", tier="1k")  # second call: served from ~/.cache/mat-vis/

# Filter via the JSON index (list comprehension over ~3100 materials)
woods = Material.filter(category="wood")
```

mat's built-in texture client is pure Python (~150-300 lines),
ships inside the `mat` wheel, and requires no pyarrow or other
binary dependencies. Rust and JS clients will mirror this shape.

## License

- **Code** (index schemas, build scripts, client wrappers): MIT —
  see [`LICENSE`](LICENSE).
- **Data** (indexes + textures): license inherits from each
  upstream source. The three of four that we mirror today are
  **CC0 1.0 Universal** (public domain dedication) — see
  [`LICENSES.md`](LICENSES.md) for the per-source breakdown once
  data starts shipping.

## Links

- **[MorePET/mat][mat]** — py-materials + rs-materials (physics + scalar PBR)
- **[bernhard-42/threejs-materials](https://github.com/bernhard-42/threejs-materials)** — the MaterialX baker that produces the data we redistribute
- **[gumyr/build123d](https://github.com/gumyr/build123d)** — primary CAD consumer
- **Collaboration thread**: [MorePET/mat#3](https://github.com/MorePET/mat/issues/3)

[mat]: https://github.com/MorePET/mat
