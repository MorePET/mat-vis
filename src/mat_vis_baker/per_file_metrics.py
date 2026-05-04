"""Per-file substrate bake metrics — append-only record of every batch
commit emitted by the v0.6.0 per-file pipeline (#263 phase B).

One row per atomic HF commit (texture batch / derive batch). Rows
accumulate at ``metrics/per-file-metrics.parquet`` (git-tracked, separate
from the frozen v0.5.x ``metrics/bake-metrics.parquet`` artifact). Phase C
will wire ``scripts/validate_release.py`` to consume this file for the
regression + tier-parity gates that catch under-bake regressions of the
v2026.04.0 ``gpuopen-1k 2234 → 10`` shape.

Schema (one row per ``api.create_commit`` of a material payload):

- ``release_tag``: the HF branch the bake committed against (e.g.
  ``v2026.04.2``).
- ``source``: ``ambientcg | polyhaven | gpuopen | physicallybased``.
- ``tier``: tier label as written to HF (storage_tier in #230 parlance).
- ``operation``: ``bake | derive_resize | derive_ktx2`` — distinguishes
  the upstream-fetching baker from the substrate-deriving paths so the
  validator can scope to bake-only rows when comparing material totals.
- ``batch_seq``: 1-indexed batch number within the
  ``(release_tag, source, tier, operation)`` group. Lets the validator
  sum batches per group without depending on commit timestamps.
- ``materials_committed``: count of materials in this batch's commit.
- ``files_committed``: count of file ops in this batch's commit.
- ``bytes_committed``: total bytes uploaded in this batch's commit
  (matches ``pending_bytes`` in the baker's bytes-aware batching, #228).
- ``hf_commit_oid``: the OID returned by ``HfApi.create_commit``.
  Empty string for ``dry_run`` and test paths that bypass the network.
- ``timestamp_utc``: ISO 8601 UTC, second precision.
- ``repo_id``: e.g. ``gerchowl/mat-vis`` or ``gerchowl/mat-vis-tst``.

Append-by-rewrite: read existing parquet (if any), concat one new row,
write. At ≤ a few thousand rows over the project lifetime this is
microsecond-scale and avoids the operational complexity of an
append-mode Parquet writer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


PER_FILE_METRICS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("release_tag", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("tier", pa.string(), nullable=False),
        pa.field("operation", pa.string(), nullable=False),
        pa.field("batch_seq", pa.int32(), nullable=False),
        pa.field("materials_committed", pa.int32(), nullable=False),
        pa.field("files_committed", pa.int32(), nullable=False),
        pa.field("bytes_committed", pa.int64(), nullable=False),
        pa.field("hf_commit_oid", pa.string(), nullable=False),
        pa.field("timestamp_utc", pa.string(), nullable=False),
        pa.field("repo_id", pa.string(), nullable=False),
    ]
)


VALID_OPERATIONS = frozenset({"bake", "derive_resize", "derive_ktx2"})


__all__ = [
    "PER_FILE_METRICS_SCHEMA",
    "VALID_OPERATIONS",
    "MetricsRow",
    "record_batch",
]


@dataclass(frozen=True)
class MetricsRow:
    """One row of the per-file metrics parquet.

    Frozen so callers can build it once at flush time and pass it into
    :func:`record_batch` without worrying about post-construction edits.
    """

    release_tag: str
    source: str
    tier: str
    operation: str
    batch_seq: int
    materials_committed: int
    files_committed: int
    bytes_committed: int
    hf_commit_oid: str
    timestamp_utc: str
    repo_id: str


def _utc_now_iso() -> str:
    """ISO 8601 UTC at second precision — matches what humans paste
    into release notes and what ``date -u`` emits by default."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_batch(
    metrics_path: Path,
    *,
    release_tag: str,
    source: str,
    tier: str,
    operation: str,
    batch_seq: int,
    materials_committed: int,
    files_committed: int,
    bytes_committed: int,
    hf_commit_oid: str,
    repo_id: str,
    timestamp_utc: str | None = None,
) -> MetricsRow:
    """Append one row to ``metrics_path``, creating the parquet if it
    doesn't yet exist.

    Returns the appended :class:`MetricsRow` so the caller can include
    it in structured logs or pass it to a telemetry sink.

    Raises ``ValueError`` if ``operation`` isn't in
    :data:`VALID_OPERATIONS` — early rejection beats a downstream
    validator silently classifying a typo as a new operation kind.
    """
    if operation not in VALID_OPERATIONS:
        raise ValueError(
            f"unknown operation {operation!r}; expected one of {sorted(VALID_OPERATIONS)}"
        )

    row = MetricsRow(
        release_tag=release_tag,
        source=source,
        tier=tier,
        operation=operation,
        batch_seq=int(batch_seq),
        materials_committed=int(materials_committed),
        files_committed=int(files_committed),
        bytes_committed=int(bytes_committed),
        hf_commit_oid=hf_commit_oid or "",
        timestamp_utc=timestamp_utc or _utc_now_iso(),
        repo_id=repo_id,
    )

    new_table = _row_to_table(row)
    if metrics_path.exists():
        old_table = pq.read_table(metrics_path)
        # Defensive: if a stale parquet exists with a mismatched schema
        # (e.g. a pre-v0.6 file at this path), refuse to corrupt it.
        if old_table.schema != PER_FILE_METRICS_SCHEMA:
            raise ValueError(
                f"existing parquet at {metrics_path} has incompatible schema; "
                "refusing to append (delete the file or migrate first)"
            )
        combined = pa.concat_tables([old_table, new_table])
    else:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        combined = new_table

    pq.write_table(combined, metrics_path, compression="zstd")
    return row


def _row_to_table(row: MetricsRow) -> pa.Table:
    """Build a one-row Arrow table conforming to
    :data:`PER_FILE_METRICS_SCHEMA`. Centralising the shape here keeps
    schema drift impossible without an explicit edit to both the dataclass
    and the schema literal."""
    cols = {k: [v] for k, v in asdict(row).items()}
    return pa.table(cols, schema=PER_FILE_METRICS_SCHEMA)
