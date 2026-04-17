"""Tests for mat_vis_baker.parquet_writer — the critical correctness tests."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from mat_vis_baker.common import CANONICAL_CHANNELS, MaterialRecord
from mat_vis_baker.parquet_writer import generate_rowmap, write_parquet


class TestWriteParquet:
    def test_schema_columns(self, tmp_path: Path, sample_records: list[MaterialRecord]):
        out = tmp_path / "test.parquet"
        write_parquet(sample_records, "ambientcg", "1k", out, 1024)
        pf = pq.ParquetFile(out)
        cols = pf.schema_arrow.names
        assert "id" in cols
        assert "source" in cols
        assert "color" in cols
        assert "normal" in cols

    def test_binary_columns_uncompressed(
        self, tmp_path: Path, sample_records: list[MaterialRecord]
    ):
        out = tmp_path / "test.parquet"
        write_parquet(sample_records, "ambientcg", "1k", out, 1024)
        pf = pq.ParquetFile(out)
        meta = pf.metadata
        rg = meta.row_group(0)
        for col_idx in range(rg.num_columns):
            col = rg.column(col_idx)
            if col.path_in_schema in CANONICAL_CHANNELS:
                assert col.compression == "UNCOMPRESSED", (
                    f"{col.path_in_schema} should be UNCOMPRESSED"
                )

    def test_one_row_per_row_group(self, tmp_path: Path, sample_records: list[MaterialRecord]):
        out = tmp_path / "test.parquet"
        write_parquet(sample_records, "ambientcg", "1k", out, 1024)
        pf = pq.ParquetFile(out)
        assert pf.metadata.num_row_groups == len(sample_records)

    def test_row_count(self, tmp_path: Path, sample_records: list[MaterialRecord]):
        out = tmp_path / "test.parquet"
        write_parquet(sample_records, "ambientcg", "1k", out, 1024)
        pf = pq.ParquetFile(out)
        assert pf.metadata.num_rows == len(sample_records)


class TestRowmap:
    def test_rowmap_offsets_yield_valid_png(
        self, tmp_path: Path, sample_records: list[MaterialRecord], tiny_png_bytes: bytes
    ):
        """The critical test: read bytes at rowmap offsets → valid PNG."""
        out = tmp_path / "test.parquet"
        write_parquet(sample_records, "ambientcg", "1k", out, 1024)
        rowmap = generate_rowmap(out, "ambientcg", "1k", "v0000.00.0", sample_records)

        file_bytes = out.read_bytes()

        for mid, channels in rowmap["materials"].items():
            for ch, rng in channels.items():
                offset = rng["offset"]
                length = rng["length"]
                extracted = file_bytes[offset : offset + length]
                assert extracted[:4] == b"\x89PNG", (
                    f"{mid}/{ch}: bytes at offset don't start with PNG magic"
                )
                assert len(extracted) == length
                assert extracted == tiny_png_bytes, (
                    f"{mid}/{ch}: extracted bytes differ from original"
                )

    def test_rowmap_structure(self, tmp_path: Path, sample_records: list[MaterialRecord]):
        out = tmp_path / "test.parquet"
        write_parquet(sample_records, "ambientcg", "1k", out, 1024)
        rowmap = generate_rowmap(out, "ambientcg", "1k", "v0000.00.0", sample_records)

        assert rowmap["version"] == 1
        assert rowmap["source"] == "ambientcg"
        assert rowmap["tier"] == "1k"
        assert "materials" in rowmap
        assert len(rowmap["materials"]) == len(sample_records)

    def test_all_channels_present(self, tmp_path: Path, sample_records: list[MaterialRecord]):
        out = tmp_path / "test.parquet"
        write_parquet(sample_records, "ambientcg", "1k", out, 1024)
        rowmap = generate_rowmap(out, "ambientcg", "1k", "v0000.00.0", sample_records)

        for mid, channels in rowmap["materials"].items():
            assert set(channels.keys()) == {"color", "normal", "roughness"}
