# 0005. SQL shim embedded in clients

- Status: Superseded by ADR-0001 (rowmap + direct Parquet access)
- Date: 2026-04-16
- Deciders: @gerchowl

## Summary

Originally proposed embedding a DuckDB URL-table shim in every
language client to register Release asset URLs as SQL views, plus
an optional GitHub Pages DuckDB-WASM explorer.

**Superseded.** The JSON index handles corpus filtering (a list
comprehension over ~3100 materials). The companion rowmap
(ADR-0001) handles byte-level texture access via pure-Python HTTP
range reads. DuckDB/pyarrow power users query the Parquet files
directly with their own tooling — no client-side infrastructure
needed.

The original proposal over-engineered the problem: 3100 materials
is not a database-scale dataset. It's a dict.
