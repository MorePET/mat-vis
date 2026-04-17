"""Tests for the Python reference client and adapters.

Unit tests (mocked) run unconditionally.
Live tests hit the real release and are skipped with MAT_VIS_SKIP_LIVE_TESTS=1.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mat_vis_client import MatVisClient
from mat_vis_client.client import _in_range
from mat_vis_client.adapters import (
    to_threejs,
    to_gltf,
    export_mtlx,
    _color_hex_to_int,
    _color_hex_to_rgba,
    _to_data_uri,
)

# ── Fixtures ────────────────────────────────────────────────────

# Minimal PNG: 1x1 red pixel (valid PNG header + IHDR + IDAT + IEND)
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


MOCK_MANIFEST = {
    "version": 1,
    "release_tag": "v2026.04.0",
    "tiers": {
        "1k": {
            "base_url": "https://example.com/releases/download/v2026.04.0/",
            "sources": {
                "ambientcg": {
                    "parquet_files": ["ambientcg-1k.parquet"],
                    "rowmap_file": "ambientcg-1k-rowmap.json",
                },
                "polyhaven": {
                    "parquet_files": ["polyhaven-1k.parquet"],
                    "rowmap_file": "polyhaven-1k-rowmap.json",
                },
            },
        }
    },
}

MOCK_ROWMAP = {
    "parquet_file": "mat-vis-ambientcg-1k.parquet",
    "materials": {
        "Rock064": {
            "color": {"offset": 0, "length": 1024, "parquet_file": "mat-vis-ambientcg-1k.parquet"},
            "normal": {
                "offset": 1024,
                "length": 2048,
                "parquet_file": "mat-vis-ambientcg-1k.parquet",
            },
            "roughness": {
                "offset": 3072,
                "length": 512,
                "parquet_file": "mat-vis-ambientcg-1k.parquet",
            },
        },
        "Metal032": {
            "color": {
                "offset": 4000,
                "length": 800,
                "parquet_file": "mat-vis-ambientcg-1k.parquet",
            },
            "metalness": {
                "offset": 4800,
                "length": 600,
                "parquet_file": "mat-vis-ambientcg-1k.parquet",
            },
            "roughness": {
                "offset": 5400,
                "length": 500,
                "parquet_file": "mat-vis-ambientcg-1k.parquet",
            },
        },
    },
}

MOCK_INDEX_AMBIENTCG = [
    {
        "id": "Rock064",
        "source": "ambientcg",
        "name": "Rough Granite",
        "category": "stone",
        "roughness": 0.8,
        "metalness": 0.0,
        "color_hex": "#A0522D",
        "ior": 1.5,
        "source_url": "https://ambientcg.com/view?id=Rock064",
        "source_license": "CC0-1.0",
        "available_tiers": ["1k", "2k"],
        "maps": ["color", "normal", "roughness"],
        "last_updated": "2025-01-15",
    },
    {
        "id": "Metal032",
        "source": "ambientcg",
        "name": "Brushed Steel",
        "category": "metal",
        "roughness": 0.3,
        "metalness": 1.0,
        "color_hex": "#C0C0C0",
        "ior": 2.5,
        "source_url": "https://ambientcg.com/view?id=Metal032",
        "source_license": "CC0-1.0",
        "available_tiers": ["1k"],
        "maps": ["color", "metalness", "roughness"],
        "last_updated": "2025-02-10",
    },
    {
        "id": "Wood045",
        "source": "ambientcg",
        "name": "Oak Planks",
        "category": "wood",
        "roughness": 0.6,
        "metalness": 0.0,
        "color_hex": "#8B4513",
        "ior": 1.5,
        "source_url": "https://ambientcg.com/view?id=Wood045",
        "source_license": "CC0-1.0",
        "available_tiers": ["1k", "2k", "4k"],
        "maps": ["color", "normal", "roughness", "ao"],
        "last_updated": "2025-03-01",
    },
]


@pytest.fixture
def mock_client():
    """Client with mocked HTTP and temp cache."""
    with tempfile.TemporaryDirectory() as tmp:
        client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
        # Pre-populate manifest cache so no HTTP needed
        cache_path = Path(tmp) / ".manifest.json"
        cache_path.write_text(json.dumps(MOCK_MANIFEST))
        yield client


# ── Helper tests ────────────────────────────────────────────────


class TestInRange:
    def test_within_range(self):
        assert _in_range(0.5, 0.0, 1.0)

    def test_at_boundaries(self):
        assert _in_range(0.0, 0.0, 1.0)
        assert _in_range(1.0, 0.0, 1.0)

    def test_outside_range(self):
        assert not _in_range(1.5, 0.0, 1.0)

    def test_none_value(self):
        assert not _in_range(None, 0.0, 1.0)


# ── Client unit tests (mocked HTTP) ────────────────────────────


class TestClientManifest:
    def test_manifest_loads_from_cache(self, mock_client):
        m = mock_client.manifest
        assert m["version"] == 1
        assert "tiers" in m

    def test_tiers(self, mock_client):
        assert mock_client.tiers() == ["1k"]

    def test_sources(self, mock_client):
        sources = mock_client.sources("1k")
        assert "ambientcg" in sources
        assert "polyhaven" in sources


class TestClientRowmap:
    @patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP)
    def test_fetch_rowmap(self, mock_get, mock_client):
        rm = mock_client.rowmap("ambientcg", "1k")
        assert "materials" in rm
        assert "Rock064" in rm["materials"]

    @patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP)
    def test_materials_list(self, mock_get, mock_client):
        mats = mock_client.materials("ambientcg", "1k")
        assert "Metal032" in mats
        assert "Rock064" in mats

    @patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP)
    def test_channels(self, mock_get, mock_client):
        channels = mock_client.channels("ambientcg", "Rock064", "1k")
        assert "color" in channels
        assert "normal" in channels

    @patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP)
    def test_rowmap_entry(self, mock_get, mock_client):
        entry = mock_client.rowmap_entry("ambientcg", "Rock064", "1k")
        assert "color" in entry
        assert entry["color"]["offset"] == 0
        assert entry["color"]["length"] == 1024
        assert entry["color"]["parquet_file"] == "mat-vis-ambientcg-1k.parquet"


class TestClientSearch:
    @patch("mat_vis_client.client._get_json")
    def test_search_by_category(self, mock_get, mock_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_client.search("metal", source="ambientcg")
        assert len(results) == 1
        assert results[0]["id"] == "Metal032"

    @patch("mat_vis_client.client._get_json")
    def test_search_by_roughness_range(self, mock_get, mock_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_client.search(roughness_range=(0.5, 0.9), source="ambientcg")
        # Rock064 (0.8) and Wood045 (0.6) match
        ids = {r["id"] for r in results}
        assert ids == {"Rock064", "Wood045"}

    @patch("mat_vis_client.client._get_json")
    def test_search_by_metalness_range(self, mock_get, mock_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_client.search(metalness_range=(0.9, 1.0), source="ambientcg")
        assert len(results) == 1
        assert results[0]["id"] == "Metal032"

    @patch("mat_vis_client.client._get_json")
    def test_search_combined_filters(self, mock_get, mock_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_client.search(
            "stone",
            roughness_range=(0.5, 1.0),
            source="ambientcg",
        )
        assert len(results) == 1
        assert results[0]["id"] == "Rock064"

    @patch("mat_vis_client.client._get_json")
    def test_search_tier_filter(self, mock_get, mock_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        # Metal032 is only available in 1k
        results = mock_client.search("metal", source="ambientcg", tier="2k")
        assert len(results) == 0

    @patch("mat_vis_client.client._get_json")
    def test_search_no_filters_returns_all(self, mock_get, mock_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_client.search(source="ambientcg")
        assert len(results) == 3

    def test_search_invalid_category(self, mock_client):
        with pytest.raises(ValueError, match="Unknown category"):
            mock_client.search("invalid_category")


class TestClientPrefetch:
    @patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP)
    @patch("mat_vis_client.client._get", return_value=TINY_PNG)
    def test_prefetch_downloads_all(self, mock_http, mock_json, mock_client):
        progress = []
        n = mock_client.prefetch(
            "ambientcg",
            "1k",
            on_progress=lambda mid, i, total: progress.append((mid, i, total)),
        )
        assert n == 2  # Rock064 and Metal032
        assert len(progress) == 2
        assert progress[-1][1] == 2  # last index
        assert progress[-1][2] == 2  # total

    @patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP)
    @patch("mat_vis_client.client._get", return_value=TINY_PNG)
    def test_fetch_all_textures(self, mock_http, mock_json, mock_client):
        textures = mock_client.fetch_all_textures("ambientcg", "Rock064", "1k")
        assert set(textures.keys()) == {"color", "normal", "roughness"}
        for ch, data in textures.items():
            assert data[:4] == b"\x89PNG", f"{ch} is not PNG"


# ── Adapter helper tests ───────────────────────────────────────


class TestAdapterHelpers:
    def test_color_hex_to_int(self):
        assert _color_hex_to_int("#A0522D") == 0xA0522D
        assert _color_hex_to_int("#000000") == 0
        assert _color_hex_to_int("#FFFFFF") == 0xFFFFFF

    def test_color_hex_to_rgba(self):
        rgba = _color_hex_to_rgba("#FF0000")
        assert rgba == [1.0, 0.0, 0.0, 1.0]

    def test_to_data_uri(self):
        uri = _to_data_uri(b"\x89PNG")
        assert uri.startswith("data:image/png;base64,")
        assert "iVBO" in uri  # base64 of \x89P


# ── Three.js adapter tests ─────────────────────────────────────


class TestToThreejs:
    def test_scalars_only(self):
        result = to_threejs({"metalness": 1.0, "roughness": 0.3, "color_hex": "#C0C0C0"})
        assert result["type"] == "MeshPhysicalMaterial"
        assert result["metalness"] == 1.0
        assert result["roughness"] == 0.3
        assert result["color"] == 0xC0C0C0

    def test_with_textures(self):
        result = to_threejs(
            {"metalness": 0.5},
            {"color": TINY_PNG, "normal": TINY_PNG},
        )
        assert "map" in result
        assert result["map"].startswith("data:image/png;base64,")
        assert "normalMap" in result

    def test_empty_scalars(self):
        result = to_threejs({})
        assert result == {"type": "MeshPhysicalMaterial"}

    def test_none_scalars_skipped(self):
        result = to_threejs({"metalness": None, "roughness": 0.5})
        assert "metalness" not in result
        assert result["roughness"] == 0.5

    def test_ior_and_transmission(self):
        result = to_threejs({"ior": 1.5, "transmission": 0.8})
        assert result["ior"] == 1.5
        assert result["transmission"] == 0.8

    def test_all_texture_channels(self):
        textures = {
            ch: TINY_PNG
            for ch in [
                "color",
                "normal",
                "roughness",
                "metalness",
                "ao",
                "displacement",
                "emission",
            ]
        }
        result = to_threejs({}, textures)
        assert "map" in result
        assert "normalMap" in result
        assert "roughnessMap" in result
        assert "metalnessMap" in result
        assert "aoMap" in result
        assert "displacementMap" in result
        assert "emissiveMap" in result


# ── glTF adapter tests ─────────────────────────────────────────


class TestToGltf:
    def test_scalars_only(self):
        result = to_gltf({"metalness": 1.0, "roughness": 0.3, "color_hex": "#FF0000"})
        pbr = result["pbrMetallicRoughness"]
        assert pbr["metallicFactor"] == 1.0
        assert pbr["roughnessFactor"] == 0.3
        assert pbr["baseColorFactor"] == [1.0, 0.0, 0.0, 1.0]

    def test_with_textures(self):
        result = to_gltf(
            {},
            {"color": TINY_PNG, "normal": TINY_PNG},
        )
        pbr = result["pbrMetallicRoughness"]
        assert "baseColorTexture" in pbr
        assert "normalTexture" in result

    def test_ior_extension(self):
        result = to_gltf({"ior": 1.5})
        assert result["extensions"]["KHR_materials_ior"]["ior"] == 1.5

    def test_transmission_extension(self):
        result = to_gltf({"transmission": 0.8})
        ext = result["extensions"]["KHR_materials_transmission"]
        assert ext["transmissionFactor"] == 0.8

    def test_packed_texture_note(self):
        result = to_gltf(
            {},
            {"metalness": TINY_PNG, "roughness": TINY_PNG},
        )
        pbr = result["pbrMetallicRoughness"]
        assert "_note_metallicRoughnessTexture" in pbr

    def test_empty_scalars(self):
        result = to_gltf({})
        assert result == {"pbrMetallicRoughness": {}}


# ── MaterialX adapter tests ────────────────────────────────────


class TestExportMtlx:
    def test_scalars_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_mtlx(
                {"metalness": 1.0, "roughness": 0.3, "color_hex": "#C0C0C0"},
                output_dir=tmp,
                material_name="TestMat",
            )
            assert path.exists()
            assert path.suffix == ".mtlx"

            content = path.read_text()
            assert "standard_surface" in content
            assert 'name="metalness"' in content
            assert 'name="specular_roughness"' in content
            assert 'name="base_color"' in content

    def test_with_textures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_mtlx(
                {"metalness": 0.5},
                {"color": TINY_PNG, "normal": TINY_PNG},
                output_dir=tmp,
                material_name="TexMat",
            )
            assert path.exists()

            # Check PNG files were written
            assert (Path(tmp) / "TexMat_color.png").exists()
            assert (Path(tmp) / "TexMat_normal.png").exists()

            content = path.read_text()
            assert "tiledimage" in content
            assert "TexMat_color.png" in content

    def test_creates_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "sub" / "dir"
            path = export_mtlx({}, output_dir=nested)
            assert path.exists()
            assert nested.is_dir()

    def test_material_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_mtlx({}, output_dir=tmp, material_name="MyMat")
            assert path.name == "MyMat.mtlx"

            content = path.read_text()
            assert 'name="MyMat"' in content
            assert 'name="SR_MyMat"' in content


# ── Live tests (network required) ──────────────────────────────

live = pytest.mark.skipif(
    os.environ.get("MAT_VIS_SKIP_LIVE_TESTS") == "1",
    reason="MAT_VIS_SKIP_LIVE_TESTS=1",
)


@pytest.fixture
def live_client():
    """Client pointed at v2026.04.0 with temp cache."""
    with tempfile.TemporaryDirectory() as tmp:
        yield MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))


@live
class TestLiveManifest:
    def test_fetch_manifest(self, live_client):
        m = live_client.manifest
        assert m["version"] == 1
        assert "tiers" in m

    def test_tiers(self, live_client):
        tiers = live_client.tiers()
        assert "1k" in tiers

    def test_sources(self, live_client):
        sources = live_client.sources("1k")
        assert "ambientcg" in sources


@live
class TestLiveRowmap:
    def test_fetch_rowmap(self, live_client):
        rm = live_client.rowmap("ambientcg", "1k")
        assert "materials" in rm
        assert len(rm["materials"]) > 0

    def test_materials_list(self, live_client):
        mats = live_client.materials("ambientcg", "1k")
        assert len(mats) > 0
        assert all(isinstance(m, str) for m in mats)

    def test_channels(self, live_client):
        mats = live_client.materials("ambientcg", "1k")
        channels = live_client.channels("ambientcg", mats[0], "1k")
        assert "color" in channels


@live
class TestLiveFetchTexture:
    def test_fetch_returns_png(self, live_client):
        mats = live_client.materials("ambientcg", "1k")
        data = live_client.fetch_texture("ambientcg", mats[0], "color", "1k")
        assert data[:4] == b"\x89PNG"
        assert len(data) > 1000

    def test_fetch_caches_locally(self, live_client):
        mats = live_client.materials("ambientcg", "1k")
        mid = mats[0]
        data1 = live_client.fetch_texture("ambientcg", mid, "color", "1k")
        data2 = live_client.fetch_texture("ambientcg", mid, "color", "1k")
        assert data1 == data2

    def test_fetch_multiple_channels(self, live_client):
        mats = live_client.materials("ambientcg", "1k")
        mid = mats[0]
        channels = live_client.channels("ambientcg", mid, "1k")
        for ch in channels[:3]:
            data = live_client.fetch_texture("ambientcg", mid, ch, "1k")
            assert data[:4] == b"\x89PNG", f"{mid}/{ch} is not PNG"

    def test_fetch_nonexistent_material_raises(self, live_client):
        with pytest.raises(KeyError):
            live_client.fetch_texture("ambientcg", "NONEXISTENT_XYZ", "color", "1k")
