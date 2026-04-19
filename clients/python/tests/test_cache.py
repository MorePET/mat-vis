"""Tests for cache modes and tag-keyed invalidation (#85 item 3).

Two ADR-0004 gaps are closed here:

1. ``cache=False`` mode — opt out of the on-disk cache entirely so no
   reads hit the cache dir and no writes land on disk. Required for
   ephemeral/stateless environments (CI, notebooks).

2. Tag-keyed cache paths — data fetched for release v1 must not be
   served when the client is asked for release v2. Before this, a
   single ``~/.cache/mat-vis/`` held textures for whichever tag was
   fetched first, causing silent data corruption on tag bumps.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mat_vis_client import MatVisClient


MOCK_MANIFEST_V1 = {
    "schema_version": 1,
    "release_tag": "v2026.04.0",
    "tiers": {
        "1k": {
            "base_url": "https://example.com/v2026.04.0/",
            "sources": {
                "ambientcg": {
                    "parquet_files": ["ambientcg-1k.parquet"],
                    "rowmap_file": "ambientcg-1k-rowmap.json",
                },
            },
        }
    },
}

MOCK_MANIFEST_V2 = {
    "schema_version": 1,
    "release_tag": "v2026.05.0",
    "tiers": {
        "1k": {
            "base_url": "https://example.com/v2026.05.0/",
            "sources": {
                "ambientcg": {
                    "parquet_files": ["ambientcg-1k.parquet"],
                    "rowmap_file": "ambientcg-1k-rowmap.json",
                },
            },
        }
    },
}

MOCK_ROWMAP = {
    "parquet_file": "ambientcg-1k.parquet",
    "materials": {
        "Rock064": {
            "color": {
                "offset": 0,
                "length": 100,
                "parquet_file": "ambientcg-1k.parquet",
            }
        }
    },
}

TINY_PNG_V1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

TINY_PNG_V2 = b"\x89PNG" + b"v2_different_bytes_but_still_png_prefix" + b"\xaeB`\x82"


@pytest.fixture
def tmp_cache():
    tmp = Path(tempfile.mkdtemp(prefix="mat-vis-test-cache-"))
    yield tmp
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


# ── cache=False ────────────────────────────────────────────────


def test_cache_false_kwarg_accepted(tmp_cache):
    """MatVisClient must accept cache=False kwarg."""
    c = MatVisClient(cache_dir=tmp_cache, cache=False)
    assert c._cache is False


def test_cache_true_is_default(tmp_cache):
    c = MatVisClient(cache_dir=tmp_cache)
    assert c._cache is True


def test_cache_false_does_not_write_manifest_to_disk(tmp_cache):
    """With cache=False, fetching manifest must not create a .manifest.json."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0", cache=False)
    with patch("mat_vis_client.client._get_json", return_value=MOCK_MANIFEST_V1):
        _ = c.manifest
    # No cached manifest anywhere under tmp_cache
    matches = list(tmp_cache.rglob(".manifest.json"))
    assert matches == [], f"expected no manifest cache, found: {matches}"


def test_cache_false_fetches_manifest_every_time(tmp_cache):
    """With cache=False, manifest is fetched on every .manifest access."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0", cache=False)
    with patch("mat_vis_client.client._get_json", return_value=MOCK_MANIFEST_V1) as mock_get:
        _ = c.manifest
        # Re-reset the in-memory cache to force another fetch
        c._manifest = None
        _ = c.manifest
        assert mock_get.call_count == 2


def test_cache_false_skips_texture_disk_write(tmp_cache):
    """Fetching a texture with cache=False writes no PNG to disk."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0", cache=False)
    c._manifest = MOCK_MANIFEST_V1

    def fake_get(url, headers=None, return_final_url=False):
        if return_final_url:
            return TINY_PNG_V1, url
        return TINY_PNG_V1

    with (
        patch("mat_vis_client.client._get", side_effect=fake_get),
        patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP),
    ):
        data = c.fetch_texture("ambientcg", "Rock064", "color", tier="1k")
    assert data == TINY_PNG_V1
    pngs = list(tmp_cache.rglob("*.png"))
    assert pngs == [], f"expected no cached pngs, found: {pngs}"


# ── tag-keyed invalidation ─────────────────────────────────────


def test_cache_path_includes_tag(tmp_cache):
    """Textures for tag v1 live under a different subtree than tag v2."""
    c1 = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    c2 = MatVisClient(cache_dir=tmp_cache, tag="v2026.05.0")
    assert c1._cache_scope != c2._cache_scope
    assert "v2026.04.0" in str(c1._cache_scope)
    assert "v2026.05.0" in str(c2._cache_scope)


def test_tag_switch_does_not_return_stale_bytes(tmp_cache):
    """Fetching texture under v1 then under v2 must NOT return v1's bytes."""
    # Populate v1 cache
    c1 = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    c1._manifest = MOCK_MANIFEST_V1

    def fake_get_v1(url, headers=None, return_final_url=False):
        if return_final_url:
            return TINY_PNG_V1, url
        return TINY_PNG_V1

    with (
        patch("mat_vis_client.client._get", side_effect=fake_get_v1),
        patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP),
    ):
        got_v1 = c1.fetch_texture("ambientcg", "Rock064", "color", tier="1k")
    assert got_v1 == TINY_PNG_V1

    # Now ask v2: must NOT reuse v1 bytes from cache
    c2 = MatVisClient(cache_dir=tmp_cache, tag="v2026.05.0")
    c2._manifest = MOCK_MANIFEST_V2

    def fake_get_v2(url, headers=None, return_final_url=False):
        if return_final_url:
            return TINY_PNG_V2, url
        return TINY_PNG_V2

    with (
        patch("mat_vis_client.client._get", side_effect=fake_get_v2),
        patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP),
    ):
        got_v2 = c2.fetch_texture("ambientcg", "Rock064", "color", tier="1k")
    assert got_v2 == TINY_PNG_V2, "stale cross-tag cache hit"


def test_same_tag_reuses_cache(tmp_cache):
    """Sanity: same tag, second fetch must use on-disk cache (no network)."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    c._manifest = MOCK_MANIFEST_V1

    def fake_get(url, headers=None, return_final_url=False):
        if return_final_url:
            return TINY_PNG_V1, url
        return TINY_PNG_V1

    with (
        patch("mat_vis_client.client._get", side_effect=fake_get) as mock_get,
        patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP),
    ):
        c.fetch_texture("ambientcg", "Rock064", "color", tier="1k")
        c.fetch_texture("ambientcg", "Rock064", "color", tier="1k")
        # Second call hits disk cache → only one _get call total
        assert mock_get.call_count == 1
