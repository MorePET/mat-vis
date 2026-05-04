"""Per-file substrate bake metrics — phase B of #263.

Covers:

1. Schema + dataclass round-trip via :func:`record_batch`.
2. Append-by-rewrite produces a multi-row parquet.
3. Bad operation strings are rejected before the parquet touches disk.
4. Schema mismatch on an existing file at the same path raises
   instead of silently overwriting (refusal-not-corruption).
5. Integration: ``bake_one_per_file`` with a mocked HfApi emits one
   metrics row per ``api.create_commit`` of a material batch.
6. Integration: ``derive_smaller_tier`` does the same with
   ``operation='derive_resize'``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest

from mat_vis_baker.per_file_metrics import (
    PER_FILE_METRICS_SCHEMA,
    VALID_OPERATIONS,
    MetricsRow,
    record_batch,
)


# ── unit: record_batch ─────────────────────────────────────────


def _kwargs(**overrides):
    """Default well-formed kwargs for record_batch — overrides per test."""
    base = dict(
        release_tag="v2026.04.2",
        source="ambientcg",
        tier="1k",
        operation="bake",
        batch_seq=1,
        materials_committed=42,
        files_committed=294,
        bytes_committed=123_456_789,
        hf_commit_oid="deadbeef" * 5,
        repo_id="gerchowl/mat-vis-tst",
    )
    base.update(overrides)
    return base


def test_record_batch_creates_parquet_with_schema(tmp_path):
    p = tmp_path / "metrics.parquet"
    row = record_batch(p, **_kwargs())

    assert isinstance(row, MetricsRow)
    table = pq.read_table(p)
    assert table.schema == PER_FILE_METRICS_SCHEMA
    assert table.num_rows == 1


def test_record_batch_appends_to_existing_parquet(tmp_path):
    p = tmp_path / "metrics.parquet"
    record_batch(p, **_kwargs(batch_seq=1))
    record_batch(p, **_kwargs(batch_seq=2, materials_committed=99))
    record_batch(p, **_kwargs(batch_seq=3, materials_committed=10))

    table = pq.read_table(p)
    assert table.num_rows == 3
    seqs = table.column("batch_seq").to_pylist()
    assert seqs == [1, 2, 3]
    mats = table.column("materials_committed").to_pylist()
    assert mats == [42, 99, 10]


def test_record_batch_rejects_bad_operation(tmp_path):
    p = tmp_path / "metrics.parquet"
    with pytest.raises(ValueError, match="unknown operation"):
        record_batch(p, **_kwargs(operation="not-a-real-op"))
    assert not p.exists(), "parquet must not be created when validation fails"


def test_record_batch_accepts_every_documented_operation(tmp_path):
    p = tmp_path / "metrics.parquet"
    for i, op in enumerate(sorted(VALID_OPERATIONS), start=1):
        record_batch(p, **_kwargs(operation=op, batch_seq=i))
    table = pq.read_table(p)
    assert sorted(table.column("operation").to_pylist()) == sorted(VALID_OPERATIONS)


def test_record_batch_refuses_to_overwrite_incompatible_schema(tmp_path):
    """A parquet at the same path with a different schema (e.g. the
    frozen v0.5.x bake-metrics.parquet) must trigger a refusal, not
    silently corrupt the file."""
    p = tmp_path / "metrics.parquet"
    # Write a parquet with a different schema first.
    import pyarrow as pa

    other = pa.table({"foo": [1], "bar": ["x"]})
    pq.write_table(other, p)

    with pytest.raises(ValueError, match="incompatible schema"):
        record_batch(p, **_kwargs())


def test_record_batch_returns_normalised_row(tmp_path):
    p = tmp_path / "metrics.parquet"
    row = record_batch(p, **_kwargs(batch_seq=5, hf_commit_oid=""))

    # Defaulted timestamp should be present + ISO-shaped.
    assert row.timestamp_utc.endswith("Z")
    assert "T" in row.timestamp_utc
    assert row.batch_seq == 5
    assert row.hf_commit_oid == ""


def test_record_batch_honours_explicit_timestamp(tmp_path):
    p = tmp_path / "metrics.parquet"
    row = record_batch(p, **_kwargs(timestamp_utc="2026-04-18T20:00:00Z"))
    assert row.timestamp_utc == "2026-04-18T20:00:00Z"


# ── integration: bake_one_per_file emits metrics ──────────────


def _fake_record(mid: str, channels: dict[str, bytes], work_dir: Path):
    """Re-export the per-file bake test helper so we don't depend on
    the test_hf_bake_per_file module being importable as a sibling."""
    from mat_vis_baker.common import (
        AttributionBlock,
        MatVisBlock,
        MaterialRecord,
    )

    d = work_dir / "textures" / mid
    d.mkdir(parents=True, exist_ok=True)
    paths = {}
    for ch, data in channels.items():
        p = d / f"{ch}.png"
        p.write_bytes(data)
        paths[ch] = p

    return MaterialRecord(
        id=mid,
        source="polyhaven",
        mat_vis=MatVisBlock(
            name=mid,
            category="other",
            upstream_id=mid,
            attribution=AttributionBlock(license_spdx="CC0-1.0"),
        ),
        texture_paths=paths,
        maps=list(channels.keys()),
        status="ok",
    )


def test_bake_one_per_file_writes_one_metrics_row_per_batch_commit(tmp_path):
    """Three materials, batch_size=1 → three batch commits → three
    metrics rows. Catalog + sentinel commits MUST NOT add metrics
    rows (those aren't material payloads).
    """
    from mat_vis_baker.hf_bake_per_file import bake_one_per_file

    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    fake_records = [
        _fake_record(
            f"mat_{i}",
            {"color": PNG_MAGIC + b"\x00" * 200, "normal": PNG_MAGIC + b"\x00" * 200},
            tmp_path,
        )
        for i in range(3)
    ]

    metrics_path = tmp_path / "per-file-metrics.parquet"

    with (
        patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
        patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
        patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
    ):

        def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
            end = None if limit is None else offset + limit
            return fake_records[offset:end]

        fetcher.return_value = _sliced
        api = api_cls.return_value
        api.list_repo_tree.return_value = []
        api.create_commit.return_value = SimpleNamespace(oid="cafef00d" * 5)

        result = bake_one_per_file(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            work_dir=tmp_path,
            hf_token="t",
            repo_id="gerchowl/mat-vis-tst",
            batch_size=1,
            metrics_path=metrics_path,
        )

    assert result["ok"] == 3, result
    assert metrics_path.exists()
    table = pq.read_table(metrics_path)
    # Three material-batch commits → three rows. Catalog + sentinel
    # commits go through api.create_commit too but DON'T pass through
    # _flush_batch, so no metrics row.
    assert table.num_rows == 3
    rows = table.to_pylist()
    assert all(r["operation"] == "bake" for r in rows)
    assert all(r["source"] == "polyhaven" for r in rows)
    assert all(r["tier"] == "1k" for r in rows)
    assert all(r["release_tag"] == "v0.0.0-test" for r in rows)
    assert [r["batch_seq"] for r in rows] == [1, 2, 3]
    assert all(r["materials_committed"] == 1 for r in rows)
    assert all(r["files_committed"] == 2 for r in rows)
    assert all(r["bytes_committed"] > 0 for r in rows)
    assert all(r["hf_commit_oid"] for r in rows)


def test_bake_one_per_file_skips_metrics_when_path_omitted(tmp_path):
    """The default behavior (no metrics_path) must not touch disk —
    important so existing test fixtures and CLI callers stay
    bytes-free."""
    from mat_vis_baker.hf_bake_per_file import bake_one_per_file

    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    fake_records = [
        _fake_record("only_one", {"color": PNG_MAGIC + b"\x00" * 100}, tmp_path),
    ]

    with (
        patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
        patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
        patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
    ):

        def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
            end = None if limit is None else offset + limit
            return fake_records[offset:end]

        fetcher.return_value = _sliced
        api = api_cls.return_value
        api.list_repo_tree.return_value = []
        api.create_commit.return_value = SimpleNamespace(oid="abc" * 5)

        result = bake_one_per_file(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            work_dir=tmp_path,
            hf_token="t",
            repo_id="gerchowl/mat-vis-tst",
            batch_size=1,
        )

    assert result["ok"] == 1
    # No metrics file should exist anywhere we'd look.
    assert not (tmp_path / "per-file-metrics.parquet").exists()


def test_bake_one_per_file_skips_metrics_in_dry_run(tmp_path):
    """Dry-run never makes a real commit, so the captured OID would be
    empty — recording the row would mislead the validator into thinking
    a phantom commit happened."""
    from mat_vis_baker.hf_bake_per_file import bake_one_per_file

    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    fake_records = [
        _fake_record("dryrun_mat", {"color": PNG_MAGIC + b"\x00" * 100}, tmp_path),
    ]
    metrics_path = tmp_path / "per-file-metrics.parquet"

    with (
        patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
        patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
        patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
    ):

        def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
            end = None if limit is None else offset + limit
            return fake_records[offset:end]

        fetcher.return_value = _sliced
        api = api_cls.return_value
        api.list_repo_tree.return_value = []

        bake_one_per_file(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            work_dir=tmp_path,
            hf_token="t",
            repo_id="gerchowl/mat-vis-tst",
            batch_size=1,
            metrics_path=metrics_path,
            dry_run=True,
        )

    assert not metrics_path.exists(), "dry-run must not emit metrics rows"


def test_bake_one_writes_metrics_for_storage_tier_when_overridden(tmp_path):
    """#230 storage_tier override: when set, the metrics row must
    record the storage tier (where files actually landed), not the
    upstream-fetch tier. Mirrors the manifest's storage_tier semantics."""
    from mat_vis_baker.hf_bake_per_file import bake_one_per_file

    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    fake_records = [
        _fake_record("mat_a", {"color": PNG_MAGIC + b"\x00" * 100}, tmp_path),
    ]
    metrics_path = tmp_path / "per-file-metrics.parquet"

    with (
        patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
        patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
        patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
    ):

        def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
            end = None if limit is None else offset + limit
            return fake_records[offset:end]

        fetcher.return_value = _sliced
        api = api_cls.return_value
        api.list_repo_tree.return_value = []
        api.create_commit.return_value = SimpleNamespace(oid="b" * 40)

        bake_one_per_file(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            work_dir=tmp_path,
            hf_token="t",
            repo_id="gerchowl/mat-vis-tst",
            batch_size=1,
            storage_tier="1k-shard-a",
            metrics_path=metrics_path,
        )

    table = pq.read_table(metrics_path)
    rows = table.to_pylist()
    assert all(r["tier"] == "1k-shard-a" for r in rows), rows


# ── integration: derive emits metrics ────────────────────────


def test_derive_smaller_tier_emits_metrics(tmp_path):
    """The shared derive driver routes both ``derive_smaller_tier`` and
    ``derive_ktx2_tier`` through the same `_flush_batch`, so testing
    one proves the wiring is symmetric. We pick resize because it's
    pure-Python (no toktx dependency)."""
    from mat_vis_baker.hf_derive_per_file import derive_smaller_tier

    metrics_path = tmp_path / "per-file-metrics.parquet"
    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    # A real 4×4 PNG so PIL.Image.open + LANCZOS resize succeed.
    import io as _io

    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (4, 4), color="white").save(buf, format="PNG")
    src_png = buf.getvalue()
    assert src_png.startswith(PNG_MAGIC)

    # Mock list_repo_tree → 2 mids; HEAD probe → False (not yet derived);
    # _http_get → returns src_png; channel listing → ["color"].
    with (
        patch("mat_vis_baker.hf_derive_per_file.HfApi") as api_cls,
        patch(
            "mat_vis_baker.hf_derive_per_file._http_head_ok",
            return_value=False,
        ),
        patch(
            "mat_vis_baker.hf_derive_per_file._http_get",
            return_value=src_png,
        ),
        patch(
            "mat_vis_baker.hf_derive_per_file._fetch_catalog",
            return_value=[],
        ),
        patch(
            "mat_vis_baker.hf_derive_per_file._list_source_channels",
            return_value=["color"],
        ),
    ):
        api = api_cls.return_value

        # Two materials present at 2k.
        def _tree(*args, **kwargs):
            for path in ("polyhaven/2k/mat_a/color.png", "polyhaven/2k/mat_b/color.png"):
                yield SimpleNamespace(path=path)

        api.list_repo_tree.side_effect = _tree
        api.create_commit.return_value = SimpleNamespace(oid="d" * 40)

        result = derive_smaller_tier(
            source="polyhaven",
            target_tier="1k",
            source_tier="2k",
            release_tag="v0.0.0-test",
            work_dir=tmp_path,
            repo_id="gerchowl/mat-vis-tst",
            hf_token="t",
            batch_size=1,
            metrics_path=metrics_path,
        )

    assert result["ok"] == 2, result
    table = pq.read_table(metrics_path)
    assert table.num_rows == 2
    rows = table.to_pylist()
    assert all(r["operation"] == "derive_resize" for r in rows)
    assert all(r["tier"] == "1k" for r in rows)
    assert [r["batch_seq"] for r in rows] == [1, 2]
