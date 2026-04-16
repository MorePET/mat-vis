# 0005. SQL shim embedded in clients; optional GH Pages DuckDB-WASM shell

- Status: Superseded by ADR-0001 (rowmap amendment)
- Date: 2026-04-16
- Deciders: @gerchowl

> **Superseded (2026-04-16).** The embedded DuckDB shim described
> below was over-engineered. The JSON index handles corpus filtering
> (a list comprehension over ~3100 materials), the companion rowmap
> (see ADR-0001) handles byte-level texture access via pure-Python
> HTTP range reads, and DuckDB/pyarrow power users query the Parquet
> files directly with their own tooling. No client-side SQL
> infrastructure is needed.

## Context

Consumers occasionally want SQL ergonomics over the corpus:

```sql
SELECT id, color_hex, roughness
FROM materials
WHERE source = 'ambientcg'
  AND category = 'wood'
  AND roughness < 0.4
ORDER BY roughness
LIMIT 50;
```

DuckDB handles this natively against remote Parquet via
`httpfs` — no local download needed for predicate-pushdown
queries against the footer + relevant row groups. The open
question: **where does the DuckDB runtime live?**

Two orthogonal concerns:

1. **How does the client library expose SQL?** Embedded in
   each language client, or via a hosted SQL endpoint the
   client calls out to over HTTP?
2. **Is there a browser-accessible "explore the corpus" UI?**
   Useful for picking materials interactively, sharing query
   URLs, and onboarding new users without `pip install`.

The tempting architecture is "host a DuckDB server that holds
all the parquet URLs as views, and every client calls it".
Sounds clean, but it's an ongoing hosted service with cost,
uptime, and CORS concerns — directly contradicts ADR-0002's
zero-cost goal. And it's unnecessary: DuckDB is ~20 MB in
Python / ~5 MB WASM in JS, and the URL table is tiny (four
sources × four tiers = a small config blob).

## Decision

**Every language client ships a small URL-table shim that
registers the Release asset URLs as DuckDB views in-process.
No hosted SQL service. Optionally ship a GitHub Pages
DuckDB-WASM "explorer" site that reuses the same shim for
browser-side ad-hoc queries.**

Concretely:

### Embedded shim in each client

Python (`py-materials`):

```python
import duckdb
import mat_vis

con = duckdb.connect(":memory:")
mat_vis.register_views(con, tier="2k")
# Now 'ambientcg', 'polyhaven', 'gpuopen', 'physicallybased'
# are views against the remote Parquets.

df = con.execute("""
  SELECT id, color_hex, roughness
  FROM ambientcg
  WHERE category = 'wood' AND roughness < 0.4
""").fetch_df()
```

Under the hood, `register_views()` issues statements like:

```sql
INSTALL httpfs; LOAD httpfs;
CREATE VIEW ambientcg AS
  SELECT * FROM read_parquet(
    'https://github.com/MorePET/mat-vis/releases/download/'
    'v2026.04.0/mat-vis-ambientcg-2k.parquet'
  );
```

The URL table is a generated Python dict, sourced from
`release-manifest.json` that the release workflow publishes
as the first asset of every release. Clients fetch the
manifest once per session (or cache it alongside the texture
cache from ADR-0004).

Rust (`rs-materials`) and JS (`three-cad-viewer` adapter)
ship functionally identical shims, each calling their native
DuckDB binding.

### Optional GitHub Pages shell

A static site at `morepet.github.io/mat-vis/explore/` ships
DuckDB-WASM + the same URL-table manifest + a minimal UI
(text box + results table + "open in notebook" copy-paste).
Re-uses the manifest asset published by every release, so the
explorer auto-updates without a separate deploy.

Zero server. One-time ~5 MB WASM download. All queries run
in the user's browser against the CDN-backed Parquet assets.

## Consequences

**Enables**:

- **Power-user ergonomics without a hosted service.**
  SQL-literate researchers get full DuckDB anywhere they
  install the client — no account, no tokens, no endpoint
  to fall over.
- **Single source of truth for URLs.** The manifest is part
  of each release; every client and the optional explorer
  site read the same blob, so URLs can't drift out of sync.
- **Shareable queries.** The explorer encodes query + tier
  + release tag in the URL, so "here's a link that
  reproduces the wood-materials list at 2K from v2026.04.0"
  just works.
- **Progressive disclosure.** Casual users never touch
  DuckDB — `mat_vis.get(id)` is the friendly path.
  SQL users drop one extra line to register views.
- **Browser demo for onboarding.** New users click through
  the explorer first, then pip-install when they decide to
  integrate.

**Costs**:

- **DuckDB is a dependency** of the language clients' SQL
  path. Python: ~20 MB. Rust: ~5 MB binary. JS: ~5 MB WASM.
  Mitigation: the core `Material` fetch path (ADR-0004) does
  not require DuckDB — it uses `pyarrow` / `parquet` crate /
  `parquet-wasm` directly. SQL is an opt-in extra via a
  feature flag / optional extra / separate submodule.
- **Explorer site is extra scope.** Not a v1 requirement.
  Treat it as a "when someone wants it" follow-up rather
  than a blocker for the first release.
- **CORS must be honored by the Parquet host.** GitHub
  Release asset URLs historically serve permissive CORS
  headers, but this is not contractually guaranteed. If a
  future GitHub change breaks CORS for WASM clients, the
  explorer site still has mirror options (HF Datasets,
  per ADR-0002 upgrade trigger).

**Rules out**:

- **Hosted DuckDB endpoint** (a server that accepts SQL
  and returns results). Adds recurring cost, uptime
  obligations, rate limiting, and an attack surface. The
  embedded shim solves the exact same use case with zero
  of these. Rejected unless a specific future need (e.g.
  queries over non-CDN-cacheable private data) justifies
  hosting.
- **Per-query SQL URL scheme** (e.g. a GET endpoint that
  proxies to a DuckDB instance). Same reasons as above.
- **Shipping a `.duckdb` file** alongside the Parquets.
  Adds a redundant file, breaks language portability
  (DuckDB native format is only readable by DuckDB),
  and gives nothing the in-process view registration
  doesn't already provide. See ADR-0001's DuckDB-file
  comparison.

## Alternatives considered

- **Hosted DuckDB instance on Cloudflare Workers / Fly.io**:
  technically viable, but directly adds a cost line item and
  a moving part. Rejected unless a concrete need justifies
  the cost.
- **Per-client generated SQL wrappers** (e.g. a typed query
  builder): nicer for small focused queries, much worse for
  exploration. Keep raw SQL as the interface; add typed
  helpers on top only when clear patterns emerge.
- **Serve DuckDB via Pyodide in Jupyter**: interesting in
  principle for the browser-side notebook case, but covered
  by the GitHub Pages explorer and by regular DuckDB in
  local Jupyter. No specific win.

## Upgrade trigger

Revisit when any of:

1. **WASM in browser becomes painfully slow** for
   realistically-sized corpora. If the explorer page hangs
   on common queries, consider a tiny server that proxies
   queries to a server-side DuckDB against the same
   Parquets (shared URL manifest).
2. **Non-public data becomes part of the corpus.** If we
   ever add materials that aren't public-CDN-cacheable (e.g.
   vendor-internal datasets with auth), a hosted endpoint
   with auth becomes necessary. That would be a new ADR.
3. **DuckDB dependency weight becomes a pain** for some
   client. If a user of the Rust or JS client complains
   about binary size, consider shipping a lighter-weight
   subset or making DuckDB strictly optional via a feature
   flag. Python users pay the weight gladly; web consumers
   might not.
4. **CORS breakage on Release asset URLs.** If GitHub
   changes CORS behavior in a way that breaks the explorer,
   mirror to an HF dataset (per ADR-0002 upgrade trigger)
   and point the manifest at the mirror.
