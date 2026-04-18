"""Tests for the derive-from-release command.

Uses a mock MatVisClient that serves tiny PNGs from an in-memory parquet,
so no network access is needed.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image

from mat_vis_baker.common import TIER_TO_PX
from mat_vis_baker.derive_from_release import _resize_png, derive_from_release
from mat_vis_baker.parquet_writer import _SCHEMA, generate_rowmap_from_parquet


def _make_png(size: int = 64) -> bytes:
    """Create a minimal valid PNG at the given size."""
    img = Image.new("RGB", (size, size), color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── unit tests ─────────────────────────────────────────────────────


class TestResizePng:
    def test_downsizes_large_image(self):
        big = _make_png(256)
        small = _resize_png(big, 64)
        with Image.open(io.BytesIO(small)) as img:
            assert img.width <= 64
            assert img.height <= 64

    def test_preserves_small_image(self):
        small = _make_png(32)
        result = _resize_png(small, 64)
        # Should return the original bytes unchanged
        assert result == small

    def test_returns_valid_png(self):
        result = _resize_png(_make_png(128), 32)
        assert result[:4] == b"\x89PNG"
        with Image.open(io.BytesIO(result)) as img:
            assert img.format == "PNG"


# ── integration test with mocked client ────────────────────────────


def _build_mock_client(material_data: dict[str, dict], source: str, tier: str):
    """Build a mock MatVisClient that serves from material_data.

    material_data = {
        "MatA": {"category": "metal", "channels": {"color": <png_bytes>, "normal": <png_bytes>}},
        ...
    }
    """
    client = MagicMock()

    # materials() returns sorted IDs
    client.materials.return_value = sorted(material_data.keys())

    # channels() returns channel names for a material
    def _channels(src, mid, t):
        return sorted(material_data[mid]["channels"].keys())

    client.channels.side_effect = _channels

    # fetch_texture() returns PNG bytes
    def _fetch_texture(src, mid, ch, t):
        return material_data[mid]["channels"][ch]

    client.fetch_texture.side_effect = _fetch_texture

    # index() returns index entries
    index_entries = []
    for mid, data in material_data.items():
        index_entries.append(
            {
                "id": mid,
                "source": source,
                "name": mid,
                "category": data["category"],
                "tags": ["test"],
                "source_url": f"https://example.com/{mid}",
                "source_license": "CC0-1.0",
                "last_updated": "2026-04-16",
                "available_tiers": [tier],
                "maps": sorted(data["channels"].keys()),
            }
        )
    client.index.return_value = index_entries

    return client


@pytest.fixture
def three_materials():
    """Three materials with 64px PNGs across different categories."""
    png = _make_png(64)
    return {
        "TestMat001": {
            "category": "metal",
            "channels": {"color": png, "normal": png, "roughness": png},
        },
        "TestMat002": {
            "category": "metal",
            "channels": {"color": png, "normal": png},
        },
        "TestMat003": {
            "category": "stone",
            "channels": {"color": png, "roughness": png},
        },
    }


def test_derive_from_release_produces_parquets(tmp_path, three_materials):
    """End-to-end: mock client -> derive -> verify parquet + rowmap + index."""
    source = "ambientcg"
    source_tier = "1k"
    target_tier = "128"
    output_dir = tmp_path / "out"

    mock_client = _build_mock_client(three_materials, source, source_tier)

    rc = derive_from_release(
        source=source,
        target_tier=target_tier,
        output_dir=output_dir,
        source_tier=source_tier,
        release_tag="v2026.04.0",
        limit=3,
        _client=mock_client,
    )

    assert rc == 0

    # Should produce per-category parquets
    metal_pq = output_dir / f"mat-vis-{source}-{target_tier}-metal.parquet"
    stone_pq = output_dir / f"mat-vis-{source}-{target_tier}-stone.parquet"
    assert metal_pq.exists(), f"Missing {metal_pq}"
    assert stone_pq.exists(), f"Missing {stone_pq}"

    # Verify parquet contents
    metal_table = pq.read_table(metal_pq)
    assert metal_table.num_rows == 2  # TestMat001, TestMat002
    assert set(metal_table.column("id").to_pylist()) == {"TestMat001", "TestMat002"}

    stone_table = pq.read_table(stone_pq)
    assert stone_table.num_rows == 1
    assert stone_table.column("id")[0].as_py() == "TestMat003"

    # Verify resolution_px is the target
    for rpx in metal_table.column("resolution_px").to_pylist():
        assert rpx == TIER_TO_PX[target_tier]

    # Verify PNGs in parquet are valid and resized
    color_bytes = metal_table.column("color")[0].as_py()
    assert color_bytes[:4] == b"\x89PNG"
    with Image.open(io.BytesIO(color_bytes)) as img:
        assert img.width <= TIER_TO_PX[target_tier]

    # Verify rowmaps exist
    metal_rm = output_dir / f"{source}-{target_tier}-metal-rowmap.json"
    stone_rm = output_dir / f"{source}-{target_tier}-stone-rowmap.json"
    assert metal_rm.exists()
    assert stone_rm.exists()

    metal_rowmap = json.loads(metal_rm.read_text())
    assert metal_rowmap["version"] == 1
    assert metal_rowmap["tier"] == target_tier
    assert "TestMat001" in metal_rowmap["materials"]
    assert "color" in metal_rowmap["materials"]["TestMat001"]

    # Verify index
    index_path = output_dir / f"{source}.json"
    assert index_path.exists()
    index_data = json.loads(index_path.read_text())
    assert len(index_data) == 3
    ids = {e["id"] for e in index_data}
    assert ids == {"TestMat001", "TestMat002", "TestMat003"}


def test_derive_from_release_with_limit(tmp_path, three_materials):
    """--limit restricts the number of materials processed."""
    source = "ambientcg"
    output_dir = tmp_path / "out"

    mock_client = _build_mock_client(three_materials, source, "1k")

    rc = derive_from_release(
        source=source,
        target_tier="256",
        output_dir=output_dir,
        source_tier="1k",
        release_tag="v2026.04.0",
        limit=1,
        _client=mock_client,
    )

    assert rc == 0

    # Only 1 material should be processed (first alphabetically = TestMat001)
    index_data = json.loads((output_dir / f"{source}.json").read_text())
    assert len(index_data) == 1


def test_derive_from_release_rejects_upscale(tmp_path):
    """Target tier >= source tier should fail."""
    rc = derive_from_release(
        source="ambientcg",
        target_tier="1k",
        output_dir=tmp_path / "out",
        source_tier="512",
    )
    assert rc == 1


def test_generate_rowmap_from_parquet(tmp_path, tiny_png_bytes):
    """Verify generate_rowmap_from_parquet finds PNG offsets correctly."""
    from mat_vis_baker.common import BAKER_VERSION
    from mat_vis_baker.parquet_writer import CHANNEL_COLS

    pq_path = tmp_path / "test.parquet"

    # Build a small parquet with one row
    row = {
        "id": ["Mat001"],
        "source": ["ambientcg"],
        "category": ["metal"],
        "resolution_px": [128],
        "color": [tiny_png_bytes],
        "normal": [tiny_png_bytes],
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

    compression = {
        col: "NONE" if col in CHANNEL_COLS else "ZSTD" for col in [f.name for f in _SCHEMA]
    }
    use_dictionary = {col: col not in CHANNEL_COLS for col in [f.name for f in _SCHEMA]}

    table = pa.table(row, schema=_SCHEMA)
    writer = pq.ParquetWriter(
        pq_path, _SCHEMA, compression=compression, use_dictionary=use_dictionary
    )
    writer.write_table(table)
    writer.close()

    rowmap = generate_rowmap_from_parquet(pq_path, "ambientcg", "128", "v1.0.0")

    assert rowmap["version"] == 1
    assert "Mat001" in rowmap["materials"]
    mat = rowmap["materials"]["Mat001"]
    assert "color" in mat
    assert "normal" in mat
    # roughness was None, should not appear
    assert "roughness" not in mat

    # Verify offsets point to actual PNG data
    with open(pq_path, "rb") as f:
        for ch_name, ch_info in mat.items():
            f.seek(ch_info["offset"])
            magic = f.read(4)
            assert magic == b"\x89PNG", (
                f"{ch_name}: expected PNG magic at offset {ch_info['offset']}"
            )
