"""Per-operation ``tag=`` kwarg (#85 item 4).

Methods on MatVisClient must accept an optional ``tag=`` override so a
single client instance can fetch from multiple releases without the
user having to instantiate parallel clients (hf-hub ``revision=`` pattern).

The override shares the parent's cache_dir and cache flag — so the
tag-scoped cache (task 3) still does the right thing.
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
MOCK_MANIFEST_V2 = {**MOCK_MANIFEST_V1, "release_tag": "v2026.05.0"}
MOCK_MANIFEST_V2["tiers"] = {
    "1k": {
        "base_url": "https://example.com/v2026.05.0/",
        "sources": MOCK_MANIFEST_V1["tiers"]["1k"]["sources"],
    }
}

MOCK_ROWMAP = {
    "parquet_file": "ambientcg-1k.parquet",
    "materials": {
        "Rock064": {
            "color": {"offset": 0, "length": 100, "parquet_file": "ambientcg-1k.parquet"},
        }
    },
}

TINY_PNG_V1 = b"\x89PNG" + b"v1" * 40 + b"IEND"
TINY_PNG_V2 = b"\x89PNG" + b"v2" * 40 + b"IEND"


@pytest.fixture
def tmp_cache():
    tmp = Path(tempfile.mkdtemp(prefix="mat-vis-test-op-tag-"))
    yield tmp
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_fetch_texture_accepts_tag_kwarg(tmp_cache):
    """fetch_texture must accept tag= override."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    c._manifest = MOCK_MANIFEST_V1

    def fake_get_json(url):
        # manifest requests include "release-manifest.json"; rowmaps
        # include "rowmap.json"; everything else fallback.
        if "release-manifest.json" in url:
            return MOCK_MANIFEST_V2 if "v2026.05.0" in url else MOCK_MANIFEST_V1
        return MOCK_ROWMAP

    def fake_get(url, headers=None, return_final_url=False):
        png = TINY_PNG_V2 if "v2026.05.0" in url else TINY_PNG_V1
        if return_final_url:
            return png, url
        return png

    with (
        patch("mat_vis_client.client._get", side_effect=fake_get),
        patch("mat_vis_client.client._get_json", side_effect=fake_get_json),
    ):
        data = c.fetch_texture("ambientcg", "Rock064", "color", tier="1k", tag="v2026.05.0")
    assert data == TINY_PNG_V2, "tag override should fetch v2 bytes"


def test_fetch_texture_tag_override_does_not_mutate_client(tmp_cache):
    """Passing tag= must not change the client's default tag."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    c._manifest = MOCK_MANIFEST_V1

    def fake_get_json(url):
        if "release-manifest.json" in url:
            return MOCK_MANIFEST_V2 if "v2026.05.0" in url else MOCK_MANIFEST_V1
        return MOCK_ROWMAP

    def fake_get(url, headers=None, return_final_url=False):
        png = TINY_PNG_V2 if "v2026.05.0" in url else TINY_PNG_V1
        if return_final_url:
            return png, url
        return png

    with (
        patch("mat_vis_client.client._get", side_effect=fake_get),
        patch("mat_vis_client.client._get_json", side_effect=fake_get_json),
    ):
        c.fetch_texture("ambientcg", "Rock064", "color", tier="1k", tag="v2026.05.0")

    assert c._tag == "v2026.04.0", "client's default tag must not change"


def test_at_helper_returns_tag_scoped_client(tmp_cache):
    """client.at(tag) returns an alternate client sharing cache_dir."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    alt = c.at("v2026.05.0")
    assert alt._tag == "v2026.05.0"
    assert alt._cache_dir == c._cache_dir
    assert alt._cache == c._cache


def test_at_helper_caches_alternate_clients(tmp_cache):
    """Repeated .at(tag) calls return the same cached subclient."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    alt1 = c.at("v2026.05.0")
    alt2 = c.at("v2026.05.0")
    assert alt1 is alt2


def test_at_self_returns_self(tmp_cache):
    """.at(current_tag) returns self (no useless subclient)."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    assert c.at("v2026.04.0") is c


def test_prefetch_accepts_tag_kwarg(tmp_cache):
    """prefetch supports tag= override."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    c._manifest = MOCK_MANIFEST_V1

    # Should not raise on the signature — actual behavior tested elsewhere
    import inspect

    sig = inspect.signature(c.prefetch)
    assert "tag" in sig.parameters


def test_search_accepts_tag_kwarg(tmp_cache):
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    import inspect

    sig = inspect.signature(c.search)
    assert "tag" in sig.parameters


def test_mtlx_accepts_tag_kwarg(tmp_cache):
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    import inspect

    sig = inspect.signature(c.mtlx)
    assert "tag" in sig.parameters
