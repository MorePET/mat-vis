"""Regression tests for ``emit_rowmaps_for_bake`` (#82 root-cause fix).

Before consolidation, three pipelines (``__main__``, ``ktx2``,
``derive_from_release``) each had a near-duplicate rowmap-emission loop.
Two of them iterated ``records_by_cat.keys()`` — the set of categories
with at least one *successfully-baked* material — instead of the set of
parquets actually written to disk. If every material in a category
failed transcode, the writer still opened + closed a header-only parquet
but the rowmap was never written; ``regenerate-rowmaps`` then found the
empty parquet and emitted ``{"materials": {}}``, producing the silent
inconsistency tracked as #82.

The fix: ``emit_rowmaps_for_bake`` takes the list of parquet paths as
the source of truth and emits one rowmap per parquet — including a
legitimate ``{"materials": {}}`` rowmap for a genuinely empty parquet.
These tests pin that contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from mat_vis_baker.parquet_writer import (
    _SCHEMA,
    RowmapCollector,
    emit_rowmaps_for_bake,
)


def _write_parquet(path: Path, rows: list[tuple[str, dict[str, bytes]]]) -> None:
    """Write a minimal parquet at ``path`` with the canonical _SCHEMA.

    ``rows`` is ``[(material_id, {channel: bytes | None})]``. Null channels
    get None in the column. Mirrors the real bake pipeline's write shape
    so build_rowmap_from_sidecar can find the payloads.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    field_names = [f.name for f in _SCHEMA]
    writer = pq.ParquetWriter(
        path,
        _SCHEMA,
        compression={
            n: "NONE"
            if n in ("color", "normal", "roughness", "metalness", "ao", "displacement", "emission")
            else "ZSTD"
            for n in field_names
        },
        use_dictionary={
            n: n
            not in ("color", "normal", "roughness", "metalness", "ao", "displacement", "emission")
            for n in field_names
        },
    )
    try:
        for mid, channel_bytes in rows:
            row = {n: [None] for n in field_names}
            row["id"] = [mid]
            row["source"] = ["ambientcg"]
            row["category"] = ["stone"]
            row["resolution_px"] = [1024]
            row["source_url"] = [f"https://example.com/{mid}"]
            row["source_license"] = ["CC0-1.0"]
            row["baker_version"] = ["0.0.0+test"]
            row["baked_at"] = ["2026-04-18T00:00:00Z"]
            for ch, data in channel_bytes.items():
                row[ch] = [data]
            table = pa.table(row, schema=_SCHEMA)
            writer.write_table(table)
    finally:
        writer.close()


def test_emits_rowmap_for_every_parquet_path(tmp_path: Path, tiny_png_bytes: bytes):
    """Happy path: 2 parquets, 2 rowmaps, contents match the collectors."""
    stone_pq = tmp_path / "mat-vis-ambientcg-1k-stone.parquet"
    wood_pq = tmp_path / "mat-vis-ambientcg-1k-wood.parquet"

    _write_parquet(stone_pq, [("Rock001", {"color": tiny_png_bytes})])
    _write_parquet(wood_pq, [("Oak001", {"color": tiny_png_bytes})])

    stone_coll = RowmapCollector()
    stone_coll.record("Rock001", {"color": len(tiny_png_bytes)})
    wood_coll = RowmapCollector()
    wood_coll.record("Oak001", {"color": len(tiny_png_bytes)})

    written = emit_rowmaps_for_bake(
        [stone_pq, wood_pq],
        {stone_pq: stone_coll, wood_pq: wood_coll},
        source="ambientcg",
        tier="1k",
        release_tag="v2026.04.0",
        output_dir=tmp_path,
    )

    assert len(written) == 2
    stone_rm = json.loads((tmp_path / "ambientcg-1k-stone-rowmap.json").read_text())
    wood_rm = json.loads((tmp_path / "ambientcg-1k-wood-rowmap.json").read_text())
    assert "Rock001" in stone_rm["materials"]
    assert "Oak001" in wood_rm["materials"]


def test_empty_parquet_still_emits_rowmap_with_empty_materials(
    tmp_path: Path,
):
    """Regression for #82: if every material in a category failed to bake,
    the parquet exists but has no rows and the collector is empty. The
    pre-fix code (iterating records_by_cat.keys()) skipped the rowmap
    entirely; the release then looked like rowmap vs parquet drift.

    Correct behavior: emit a rowmap with ``{"materials": {}}``. Clients
    see "no materials in this category" cleanly, not a missing file or
    a dangling pointer."""
    ceramic_pq = tmp_path / "mat-vis-ambientcg-1k-ceramic.parquet"
    _write_parquet(ceramic_pq, [])  # header-only parquet
    empty_collector = RowmapCollector()

    written = emit_rowmaps_for_bake(
        [ceramic_pq],
        {ceramic_pq: empty_collector},
        source="ambientcg",
        tier="1k",
        release_tag="v2026.04.0",
        output_dir=tmp_path,
    )

    assert len(written) == 1
    rm = json.loads((tmp_path / "ambientcg-1k-ceramic-rowmap.json").read_text())
    assert rm["materials"] == {}, (
        "empty-parquet rowmap must have materials={}; pre-fix code silently skipped these (see #82)"
    )
    assert rm["parquet_file"] == "mat-vis-ambientcg-1k-ceramic.parquet"


def test_skips_parquet_that_was_uploaded_and_unlinked(tmp_path: Path):
    """The streaming bake uploads + unlinks each parquet as it closes.
    emit_rowmaps_for_bake must tolerate a missing path (rowmap already
    emitted at close time)."""
    gone_pq = tmp_path / "mat-vis-ambientcg-1k-metal.parquet"
    # intentionally do NOT create the file

    written = emit_rowmaps_for_bake(
        [gone_pq],
        {},
        source="ambientcg",
        tier="1k",
        release_tag="v2026.04.0",
        output_dir=tmp_path,
    )

    assert written == []
    assert not (tmp_path / "ambientcg-1k-metal-rowmap.json").exists()


def test_rowmap_filename_derivation_survives_chunked_parquets(
    tmp_path: Path, tiny_png_bytes: bytes
):
    """Chunked categories produce parquets like
    ``mat-vis-ambientcg-2k-other-7.parquet`` when a category overflows
    1.8 GB. The paired rowmap must be named ``ambientcg-2k-other-7-rowmap.json``
    — the ``-N`` chunk suffix has to survive the mat-vis prefix strip."""
    pq_path = tmp_path / "mat-vis-ambientcg-2k-other-7.parquet"
    _write_parquet(pq_path, [("X001", {"color": tiny_png_bytes})])
    coll = RowmapCollector()
    coll.record("X001", {"color": len(tiny_png_bytes)})

    emit_rowmaps_for_bake(
        [pq_path],
        {pq_path: coll},
        source="ambientcg",
        tier="2k",
        release_tag="v2026.04.0",
        output_dir=tmp_path,
    )

    assert (tmp_path / "ambientcg-2k-other-7-rowmap.json").exists()
