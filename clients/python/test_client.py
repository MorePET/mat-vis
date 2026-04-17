"""Tests for the Python reference client against live release data."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from mat_vis_client import MatVisClient  # noqa: E402

# Skip all tests if no network or release not available
pytestmark = pytest.mark.skipif(
    os.environ.get("MAT_VIS_SKIP_LIVE_TESTS") == "1",
    reason="MAT_VIS_SKIP_LIVE_TESTS=1",
)


@pytest.fixture
def client():
    """Client pointed at v2026.04.0 pre-release with temp cache."""
    with tempfile.TemporaryDirectory() as tmp:
        yield MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))


class TestManifest:
    def test_fetch_manifest(self, client):
        m = client.manifest
        assert m["version"] == 1
        assert "tiers" in m

    def test_tiers(self, client):
        tiers = client.tiers()
        assert "1k" in tiers

    def test_sources(self, client):
        sources = client.sources("1k")
        assert "ambientcg" in sources


class TestRowmap:
    def test_fetch_rowmap(self, client):
        rm = client.rowmap("ambientcg", "1k")
        assert "materials" in rm
        assert len(rm["materials"]) > 0

    def test_materials_list(self, client):
        mats = client.materials("ambientcg", "1k")
        assert len(mats) > 0
        assert all(isinstance(m, str) for m in mats)

    def test_channels(self, client):
        mats = client.materials("ambientcg", "1k")
        channels = client.channels("ambientcg", mats[0], "1k")
        assert "color" in channels


class TestFetchTexture:
    def test_fetch_returns_png(self, client):
        mats = client.materials("ambientcg", "1k")
        data = client.fetch_texture("ambientcg", mats[0], "color", "1k")
        assert data[:4] == b"\x89PNG"
        assert len(data) > 1000  # not trivially small

    def test_fetch_caches_locally(self, client):
        mats = client.materials("ambientcg", "1k")
        mid = mats[0]

        # First fetch — from network
        data1 = client.fetch_texture("ambientcg", mid, "color", "1k")

        # Second fetch — from cache
        data2 = client.fetch_texture("ambientcg", mid, "color", "1k")

        assert data1 == data2

    def test_fetch_multiple_channels(self, client):
        mats = client.materials("ambientcg", "1k")
        mid = mats[0]
        channels = client.channels("ambientcg", mid, "1k")

        for ch in channels[:3]:  # test first 3 channels
            data = client.fetch_texture("ambientcg", mid, ch, "1k")
            assert data[:4] == b"\x89PNG", f"{mid}/{ch} is not PNG"

    def test_fetch_nonexistent_material_raises(self, client):
        with pytest.raises(KeyError):
            client.fetch_texture("ambientcg", "NONEXISTENT_XYZ", "color", "1k")
