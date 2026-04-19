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

import hashlib
import json
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
    "compute_metrics_from_rowmaps",
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


def compute_metrics_from_rowmaps(
    rowmap_dir: Path,
    *,
    source: str,
    tier: str,
    release_tag: str,
    baker_version: str,
    timestamp: str | None = None,
    workflow_run_id: int | None = None,
    upstream_count: int | None = None,
) -> list[dict[str, Any]]:
    """Scan ``{source}-{tier}-*-rowmap.json`` + associated parquets in
    ``rowmap_dir`` and compute one metrics row per category, plus an
    ``__all__`` aggregate.

    Used as a post-bake step: the baker writes rowmaps + parquets into
    an output dir, then this is called to produce the metrics-append
    payload that the next step pushes into ``metrics/bake-metrics.parquet``.
    """
    from datetime import datetime, timezone

    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()

    # rowmap files for this (source, tier)
    pattern = f"{source}-{tier}-*-rowmap.json"
    rowmap_paths = sorted(rowmap_dir.glob(pattern))

    rows: list[dict[str, Any]] = []
    total_count = 0
    total_bytes = 0

    for rmp in rowmap_paths:
        data = json.loads(rmp.read_text())
        materials = data.get("materials", {})
        count = len(materials)

        # Extract category from filename: {source}-{tier}-{category}-rowmap.json
        stem = rmp.stem[: -len("-rowmap")]
        prefix = f"{source}-{tier}-"
        assert stem.startswith(prefix), f"unexpected rowmap name: {rmp.name}"
        category = stem[len(prefix) :]

        # Find the parquet this rowmap points at (or infer by name)
        pq_name = data.get("parquet_file", f"mat-vis-{source}-{tier}-{category}.parquet")
        pq_path = rowmap_dir / pq_name
        size = pq_path.stat().st_size if pq_path.exists() else 0

        rowmap_sha = hashlib.sha256(rmp.read_bytes()).hexdigest()

        rows.append(
            {
                "release_tag": release_tag,
                "timestamp": timestamp,
                "source": source,
                "tier": tier,
                "category": category,
                "actual_count": count,
                "upstream_count": upstream_count,
                "total_bytes": size,
                "n_parquets": 1,
                "rowmap_sha256": rowmap_sha,
                "baker_version": baker_version,
                "workflow_run_id": workflow_run_id,
            }
        )
        total_count += count
        total_bytes += size

    # __all__ aggregate row. SHA is over the sorted list of per-category SHAs —
    # deterministic and changes if any category row changes.
    agg_sha = hashlib.sha256(
        b"\n".join(sorted(r["rowmap_sha256"].encode() for r in rows))
    ).hexdigest()

    rows.append(
        {
            "release_tag": release_tag,
            "timestamp": timestamp,
            "source": source,
            "tier": tier,
            "category": "__all__",
            "actual_count": total_count,
            "upstream_count": upstream_count,
            "total_bytes": total_bytes,
            "n_parquets": len(rowmap_paths),
            "rowmap_sha256": agg_sha,
            "baker_version": baker_version,
            "workflow_run_id": workflow_run_id,
        }
    )
    return rows
