"""Bake metrics — append-only record of what each release produced.

One row per ``(release_tag, source, tier, category)`` plus one aggregate
``category='__all__'`` row per ``(release_tag, source, tier)``. Appended
by the baker after each successful bake. Queried by
``scripts/validate_release.py`` to enforce regression / coverage
invariants.

Design: a single Parquet file at ``metrics/bake-metrics.parquet``
committed to the repo. Every append reads → concats → writes. Fine at
our scale (≤25 k rows over the lifetime of the project, <1 MB compressed).

See #88 for the QA design that motivated this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


METRICS_SCHEMA: pa.Schema = pa.schema(
    [
        # Identity
        pa.field("release_tag", pa.string(), nullable=False),
        pa.field("timestamp", pa.string(), nullable=False),  # ISO8601 UTC
        pa.field("source", pa.string(), nullable=False),
        pa.field("tier", pa.string(), nullable=False),
        pa.field("category", pa.string(), nullable=False),  # "wood" / "__all__"
        # Measurements
        pa.field("actual_count", pa.int64(), nullable=False),
        pa.field("upstream_count", pa.int64(), nullable=True),
        pa.field("total_bytes", pa.int64(), nullable=False),
        pa.field("n_parquets", pa.int32(), nullable=False),
        pa.field("rowmap_sha256", pa.string(), nullable=False),
        # Provenance
        pa.field("baker_version", pa.string(), nullable=False),
        pa.field("workflow_run_id", pa.int64(), nullable=True),
    ]
)


__all__ = [
    "METRICS_SCHEMA",
    "append_bake_metrics",
]


def append_bake_metrics(path: Path, rows: list[dict[str, Any]]) -> None:
    """Append ``rows`` to ``path``, creating the file if it doesn't exist.

    Reads the existing file (if any), concatenates, rewrites. For <100 k
    rows this is microsecond-scale and simpler than maintaining an
    append-mode Parquet writer.

    Raises ``KeyError`` / ``pa.ArrowInvalid`` if a row is missing a
    required (non-nullable) column — rejection here is better than a
    silently-null bench.
    """
    new_table = _rows_to_table(rows)
    if path.exists():
        old_table = pq.read_table(path)
        combined = pa.concat_tables([old_table, new_table])
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        combined = new_table
    pq.write_table(combined, path, compression="zstd")


def _rows_to_table(rows: list[dict[str, Any]]) -> pa.Table:
    """Convert a list of dicts to an Arrow table matching METRICS_SCHEMA.

    Validates that every required (non-nullable) column is present in
    every row — missing required columns raise immediately instead of
    producing a null in the Parquet.
    """
    required = [f.name for f in METRICS_SCHEMA if not f.nullable]
    for i, r in enumerate(rows):
        for col in required:
            if col not in r:
                raise KeyError(
                    f"row {i}: missing required column {col!r} (required: {sorted(required)})"
                )

    cols: dict[str, list[Any]] = {f.name: [] for f in METRICS_SCHEMA}
    for r in rows:
        for f in METRICS_SCHEMA:
            cols[f.name].append(r.get(f.name))
    return pa.table(cols, schema=METRICS_SCHEMA)
