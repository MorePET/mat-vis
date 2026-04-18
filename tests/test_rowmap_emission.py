"""Tests for the sidecar rowmap emission path (#57).

The sidecar approach records authoritative byte lengths during write and
resolves offsets via parquet column-chunk metadata. These tests verify:

 1. PNG bytes written to a parquet are retrievable via the emitted rowmap.
 2. KTX2 bytes (synthetic — we don't need real toktx output) are retrievable.
 3. Null channels are excluded from the rowmap (no phantom entries).
 4. ``write_parquet_with_rowmap`` is consistent with a round-trip.
"""

from __future__ import annotations

import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

from mat_vis_baker.common import BAKER_VERSION, MaterialRecord
from mat_vis_baker.parquet_writer import (
    CHANNEL_COLS,
    KTX2_MAGIC,
    PNG_MAGIC,
    _SCHEMA,
    RowmapCollector,
    build_rowmap_from_sidecar,
    write_parquet_with_rowmap,
)


def _make_png(size: int = 16, color=(255, 0, 0)) -> bytes:
    img = Image.new("RGB", (size, size), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_synthetic_ktx2(payload_size: int = 512) -> bytes:
    """Build a byte string that starts with the KTX2 magic.

    The rowmap code only cares about: (a) the 12-byte magic at the start of
    the payload, and (b) matching the known byte length. We don't need a
    real KTX2 file — just something with the right magic and length.
    """
    body = b"\x00" * max(payload_size - len(KTX2_MAGIC), 0)
    return KTX2_MAGIC + body


def _write_row(writer: pq.ParquetWriter, row_fields: dict) -> None:
    """Helper: assemble a single-row table from a partial dict and write it."""
    full = {f.name: row_fields.get(f.name, [None]) for f in _SCHEMA}
    # Scalar fields must be non-null per the schema
    for k in (
        "id",
        "source",
        "category",
        "resolution_px",
        "source_url",
        "source_license",
        "baker_version",
        "baked_at",
    ):
        assert full[k][0] is not None, f"scalar {k!r} missing"
    table = pa.table(full, schema=_SCHEMA)
    writer.write_table(table)


def _png_record(tmp_path: Path, mid: str, channels: dict[str, bytes]) -> MaterialRecord:
    """Build a MaterialRecord by writing each channel's bytes to disk."""
    mat_dir = tmp_path / mid
    mat_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for ch, data in channels.items():
        p = mat_dir / f"{ch}.png"
        p.write_bytes(data)
        paths[ch] = p
    return MaterialRecord(
        id=mid,
        source="ambientcg",
        name=mid,
        category="metal",
        tags=[],
        source_url="https://example.com/",
        source_license="CC0-1.0",
        last_updated="2026-04-16",
        available_tiers=["1k"],
        maps=sorted(paths.keys()),
        texture_paths=paths,
    )


class TestSidecarRowmapPng:
    def test_roundtrip_single_material_two_channels(self, tmp_path: Path) -> None:
        color_png = _make_png(16, (255, 0, 0))
        normal_png = _make_png(16, (128, 128, 255))
        rec = _png_record(tmp_path, "Mat001", {"color": color_png, "normal": normal_png})

        out = tmp_path / "single.parquet"
        path, rowmap = write_parquet_with_rowmap(
            [rec], "ambientcg", "1k", out, 1024, release_tag="v0000.00.0"
        )
        assert path == out
        assert rowmap is not None
        assert rowmap["version"] == 1
        assert rowmap["parquet_file"] == "single.parquet"
        assert "Mat001" in rowmap["materials"]

        file_bytes = out.read_bytes()
        channels = rowmap["materials"]["Mat001"]
        assert set(channels.keys()) == {"color", "normal"}

        # Length + starting magic must match exactly.
        color_slice = file_bytes[
            channels["color"]["offset"] : channels["color"]["offset"] + channels["color"]["length"]
        ]
        normal_slice = file_bytes[
            channels["normal"]["offset"] : channels["normal"]["offset"]
            + channels["normal"]["length"]
        ]
        assert color_slice == color_png, "color bytes do not round-trip"
        assert normal_slice == normal_png, "normal bytes do not round-trip"

    def test_null_channels_excluded(self, tmp_path: Path) -> None:
        """A material with only `color` must not have `normal` in its rowmap."""
        color_png = _make_png()
        rec = _png_record(tmp_path, "MatColorOnly", {"color": color_png})

        out = tmp_path / "null.parquet"
        _, rowmap = write_parquet_with_rowmap(
            [rec], "ambientcg", "1k", out, 1024, release_tag="v0000.00.0"
        )
        assert rowmap is not None
        channels = rowmap["materials"]["MatColorOnly"]
        assert set(channels.keys()) == {"color"}, (
            f"expected only 'color', got {list(channels.keys())}"
        )
        # And no phantom key for any other channel
        for ch in CHANNEL_COLS:
            if ch != "color":
                assert ch not in channels

    def test_multiple_materials_and_row_groups(self, tmp_path: Path) -> None:
        pngs = [_make_png(color=(i * 50, 0, 0)) for i in range(3)]
        recs = [
            _png_record(tmp_path, f"Mat{i:03d}", {"color": pngs[i], "normal": pngs[i]})
            for i in range(3)
        ]
        out = tmp_path / "multi.parquet"
        _, rowmap = write_parquet_with_rowmap(
            recs, "ambientcg", "1k", out, 1024, release_tag="v0000.00.0"
        )
        assert rowmap is not None

        pf = pq.ParquetFile(out)
        assert pf.metadata.num_row_groups == 3
        assert set(rowmap["materials"].keys()) == {"Mat000", "Mat001", "Mat002"}

        file_bytes = out.read_bytes()
        for i in range(3):
            mid = f"Mat{i:03d}"
            off = rowmap["materials"][mid]["color"]["offset"]
            length = rowmap["materials"][mid]["color"]["length"]
            assert file_bytes[off : off + length] == pngs[i], (
                f"{mid}/color bytes do not match source"
            )


class TestSidecarRowmapKtx2:
    def test_synthetic_ktx2_payload_roundtrip(self, tmp_path: Path) -> None:
        """Write synthetic KTX2 bytes directly; rowmap must locate them."""
        ktx2_a = _make_synthetic_ktx2(256)
        ktx2_b = _make_synthetic_ktx2(512)

        out = tmp_path / "ktx2.parquet"
        compression = {
            col: "NONE" if col in CHANNEL_COLS else "ZSTD" for col in [f.name for f in _SCHEMA]
        }
        use_dictionary = {col: col not in CHANNEL_COLS for col in [f.name for f in _SCHEMA]}
        writer = pq.ParquetWriter(
            out, _SCHEMA, compression=compression, use_dictionary=use_dictionary
        )
        collector = RowmapCollector()
        try:
            for mid, color_bytes, normal_bytes in [
                ("MatK001", ktx2_a, ktx2_b),
                ("MatK002", ktx2_b, ktx2_a),
            ]:
                row = {
                    "id": [mid],
                    "source": ["ambientcg"],
                    "category": ["metal"],
                    "resolution_px": [1024],
                    "color": [color_bytes],
                    "normal": [normal_bytes],
                    "roughness": [None],
                    "metalness": [None],
                    "ao": [None],
                    "displacement": [None],
                    "emission": [None],
                    "source_url": ["https://example.com"],
                    "source_license": ["CC0-1.0"],
                    "baker_version": [BAKER_VERSION],
                    "baked_at": ["2026-04-16T00:00:00+00:00"],
                }
                collector.record(
                    mid,
                    {"color": len(color_bytes), "normal": len(normal_bytes)},
                )
                writer.write_table(pa.table(row, schema=_SCHEMA))
        finally:
            writer.close()

        rowmap = build_rowmap_from_sidecar(out, collector, "ambientcg", "ktx2-1k", "v0000.00.0")
        assert set(rowmap["materials"].keys()) == {"MatK001", "MatK002"}

        file_bytes = out.read_bytes()
        # MatK001/color was ktx2_a, normal was ktx2_b
        mk1 = rowmap["materials"]["MatK001"]
        assert (
            file_bytes[mk1["color"]["offset"] : mk1["color"]["offset"] + mk1["color"]["length"]]
            == ktx2_a
        )
        assert (
            file_bytes[mk1["normal"]["offset"] : mk1["normal"]["offset"] + mk1["normal"]["length"]]
            == ktx2_b
        )
        mk2 = rowmap["materials"]["MatK002"]
        assert (
            file_bytes[mk2["color"]["offset"] : mk2["color"]["offset"] + mk2["color"]["length"]]
            == ktx2_b
        )
        assert (
            file_bytes[mk2["normal"]["offset"] : mk2["normal"]["offset"] + mk2["normal"]["length"]]
            == ktx2_a
        )

    def test_all_rowmap_slices_start_with_magic(self, tmp_path: Path) -> None:
        """Every offset in the rowmap must point at a known magic (PNG or KTX2)."""
        png = _make_png()
        ktx2 = _make_synthetic_ktx2(300)

        out = tmp_path / "mixed.parquet"
        compression = {
            col: "NONE" if col in CHANNEL_COLS else "ZSTD" for col in [f.name for f in _SCHEMA]
        }
        use_dictionary = {col: col not in CHANNEL_COLS for col in [f.name for f in _SCHEMA]}
        writer = pq.ParquetWriter(
            out, _SCHEMA, compression=compression, use_dictionary=use_dictionary
        )
        collector = RowmapCollector()
        try:
            row = {
                "id": ["Mixed001"],
                "source": ["ambientcg"],
                "category": ["metal"],
                "resolution_px": [1024],
                "color": [png],
                "normal": [ktx2],
                "roughness": [None],
                "metalness": [None],
                "ao": [None],
                "displacement": [None],
                "emission": [None],
                "source_url": ["https://example.com"],
                "source_license": ["CC0-1.0"],
                "baker_version": [BAKER_VERSION],
                "baked_at": ["2026-04-16T00:00:00+00:00"],
            }
            collector.record("Mixed001", {"color": len(png), "normal": len(ktx2)})
            writer.write_table(pa.table(row, schema=_SCHEMA))
        finally:
            writer.close()

        rowmap = build_rowmap_from_sidecar(out, collector, "ambientcg", "1k", "v0000.00.0")
        file_bytes = out.read_bytes()
        color_slice = file_bytes[
            rowmap["materials"]["Mixed001"]["color"]["offset"] : rowmap["materials"]["Mixed001"][
                "color"
            ]["offset"]
            + rowmap["materials"]["Mixed001"]["color"]["length"]
        ]
        normal_slice = file_bytes[
            rowmap["materials"]["Mixed001"]["normal"]["offset"] : rowmap["materials"]["Mixed001"][
                "normal"
            ]["offset"]
            + rowmap["materials"]["Mixed001"]["normal"]["length"]
        ]
        assert color_slice.startswith(PNG_MAGIC)
        assert normal_slice.startswith(KTX2_MAGIC)


class TestSidecarMismatch:
    def test_row_count_mismatch_raises(self, tmp_path: Path) -> None:
        """If the sidecar disagrees with the parquet row count, we fail loud."""
        rec = _png_record(tmp_path, "M1", {"color": _make_png()})
        out = tmp_path / "m.parquet"
        _, rowmap = write_parquet_with_rowmap(
            [rec], "ambientcg", "1k", out, 1024, release_tag="v0000.00.0"
        )
        assert rowmap is not None

        # Now hand a collector with MORE rows than the parquet has
        mismatched = RowmapCollector()
        mismatched.record("M1", {"color": 1})
        mismatched.record("M2", {"color": 1})

        import pytest

        with pytest.raises(ValueError, match="rowmap sidecar mismatch"):
            build_rowmap_from_sidecar(out, mismatched, "ambientcg", "1k", "v0000.00.0")
