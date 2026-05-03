"""Tests for the bake-metrics append mechanism (#88).

Every bake should append one row per ``(source, tier, category)`` (plus
an ``__all__`` aggregate) to ``metrics/bake-metrics.parquet``. The
mechanism must be append-only (history preserved) and fast — O(existing
rows) per append is acceptable at our scale (~25 k rows lifetime).
"""

from __future__ import annotations


import pyarrow as pa
import pyarrow.parquet as pq
import pytest


# ── Schema ──────────────────────────────────────────────────────


def test_metrics_schema_has_required_columns():
    from mat_vis_baker.metrics import METRICS_SCHEMA

    names = {f.name for f in METRICS_SCHEMA}
    # Core identity + measurements
    required = {
        "release_tag",
        "timestamp",
        "source",
        "tier",
        "category",
        "actual_count",
        "upstream_count",
        "total_bytes",
        "n_parquets",
        "rowmap_sha256",
        "baker_version",
        "workflow_run_id",
    }
    missing = required - names
    assert not missing, f"metrics schema missing columns: {missing}"


def test_metrics_schema_nullability():
    """upstream_count and workflow_run_id are optional (null in v1)."""
    from mat_vis_baker.metrics import METRICS_SCHEMA

    by_name = {f.name: f for f in METRICS_SCHEMA}
    assert by_name["upstream_count"].nullable
    assert by_name["workflow_run_id"].nullable
    # actual_count is never null — missing measurement is a bug, not a blank
    assert not by_name["actual_count"].nullable


# ── Append mechanism ────────────────────────────────────────────


@pytest.fixture
def tmp_metrics(tmp_path):
    return tmp_path / "bake-metrics.parquet"


def _row(**kw):
    base = {
        "release_tag": "v2026.04.0",
        "timestamp": "2026-04-18T20:00:00Z",
        "source": "ambientcg",
        "tier": "1k",
        "category": "__all__",
        "actual_count": 1965,
        "upstream_count": None,
        "total_bytes": 9_200_000_000,
        "n_parquets": 9,
        "rowmap_sha256": "a" * 64,
        "baker_version": "0.1.0",
        "workflow_run_id": None,
    }
    base.update(kw)
    return base


def test_append_to_missing_file_creates_it(tmp_metrics):
    from mat_vis_baker.metrics import append_bake_metrics

    append_bake_metrics(tmp_metrics, [_row()])
    assert tmp_metrics.exists()
    table = pq.read_table(tmp_metrics)
    assert table.num_rows == 1


def test_append_preserves_existing_rows(tmp_metrics):
    from mat_vis_baker.metrics import append_bake_metrics

    append_bake_metrics(tmp_metrics, [_row(release_tag="v2026.04.0")])
    append_bake_metrics(tmp_metrics, [_row(release_tag="v2026.04.1")])
    table = pq.read_table(tmp_metrics)
    assert table.num_rows == 2
    tags = set(table.column("release_tag").to_pylist())
    assert tags == {"v2026.04.0", "v2026.04.1"}


def test_append_many_rows_at_once(tmp_metrics):
    from mat_vis_baker.metrics import append_bake_metrics

    rows = [_row(source="ambientcg", category=c) for c in ["stone", "metal", "wood", "__all__"]]
    append_bake_metrics(tmp_metrics, rows)
    table = pq.read_table(tmp_metrics)
    assert table.num_rows == 4


def test_append_rejects_rows_with_missing_required_columns(tmp_metrics):
    from mat_vis_baker.metrics import append_bake_metrics

    bad = _row()
    del bad["actual_count"]
    with pytest.raises((KeyError, pa.ArrowInvalid, ValueError)):
        append_bake_metrics(tmp_metrics, [bad])


# ── Computation from rowmaps ────────────────────────────────────


def test_compute_metrics_from_rowmaps_produces_one_row_per_category_plus_aggregate(
    tmp_path,
):
    """Given a set of rowmaps for a (source, tier), compute a metrics row
    per category + one ``__all__`` aggregate row."""
    from mat_vis_baker.metrics import compute_metrics_from_rowmaps
    import json

    # Two rowmaps: stone (3 materials), metal (2 materials)
    stone = tmp_path / "ambientcg-1k-stone-rowmap.json"
    stone.write_text(
        json.dumps(
            {
                "parquet_file": "mat-vis-ambientcg-1k-stone.parquet",
                "materials": {
                    f"Rock{i:03d}": {"color": {"offset": 0, "length": 100}} for i in range(3)
                },
            }
        )
    )
    metal = tmp_path / "ambientcg-1k-metal-rowmap.json"
    metal.write_text(
        json.dumps(
            {
                "parquet_file": "mat-vis-ambientcg-1k-metal.parquet",
                "materials": {
                    f"Metal{i:03d}": {"color": {"offset": 0, "length": 100}} for i in range(2)
                },
            }
        )
    )
    # Fake parquets (zero-byte; we just need the files to exist for size)
    (tmp_path / "mat-vis-ambientcg-1k-stone.parquet").write_bytes(b"\x00" * 1000)
    (tmp_path / "mat-vis-ambientcg-1k-metal.parquet").write_bytes(b"\x00" * 500)

    rows = compute_metrics_from_rowmaps(
        rowmap_dir=tmp_path,
        source="ambientcg",
        tier="1k",
        release_tag="v2026.04.0",
        baker_version="0.1.0",
    )

    by_cat = {r["category"]: r for r in rows}
    assert "stone" in by_cat
    assert "metal" in by_cat
    assert "__all__" in by_cat
    assert by_cat["stone"]["actual_count"] == 3
    assert by_cat["metal"]["actual_count"] == 2
    assert by_cat["__all__"]["actual_count"] == 5  # aggregate
    # Size accounting
    assert by_cat["stone"]["total_bytes"] == 1000
    assert by_cat["metal"]["total_bytes"] == 500
    assert by_cat["__all__"]["total_bytes"] == 1500
    assert by_cat["__all__"]["n_parquets"] == 2
    # SHA stable + set
    assert len(by_cat["stone"]["rowmap_sha256"]) == 64
