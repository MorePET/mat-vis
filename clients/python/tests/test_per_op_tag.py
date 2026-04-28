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
    "schema_version": 3,  # per-file substrate (#186 / ADR-0012)
    "release_tag": "v2026.04.0",
    "sources": {
        "ambientcg": {
            "catalog": "ambientcg.json",
            "tiers": {"1k": {"complete": True}},
        },
    },
}
MOCK_MANIFEST_V2 = {**MOCK_MANIFEST_V1, "release_tag": "v2026.05.0"}

MOCK_INDEX = [
    {
        "id": "Rock064",
        "source": "ambientcg",
        "mat_vis": {"name": "Rock064", "category": "stone"},
        "available_tiers": ["1k"],
        "maps": ["color"],
    }
]

# Real PNG magic so the magic-byte check inside fetch_texture passes.
TINY_PNG_V1 = b"\x89PNG\r\n\x1a\n" + b"v1_data" + b"\xaeB`\x82"
TINY_PNG_V2 = b"\x89PNG\r\n\x1a\n" + b"v2_data" + b"\xaeB`\x82"


@pytest.fixture
def tmp_cache():
    tmp = Path(tempfile.mkdtemp(prefix="mat-vis-test-op-tag-"))
    yield tmp
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def _prime(client: MatVisClient, manifest: dict) -> None:
    """Pre-seed a per-file client (#186) so fetch_texture skips network for metadata."""
    client._manifest = manifest
    client._indexes = {"ambientcg": MOCK_INDEX}
    client._tier_complete[("ambientcg", "1k")] = True


def test_fetch_texture_accepts_tag_kwarg(tmp_cache):
    """fetch_texture must accept tag= override."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    _prime(c, MOCK_MANIFEST_V1)
    # at() lazy-creates an alternate client sharing the cache dir; pre-prime
    # it so the v2-tag fetch hits our mocks instead of the network.
    alt = c.at("v2026.05.0")
    _prime(alt, MOCK_MANIFEST_V2)

    def fake_get(url, headers=None, return_final_url=False):
        png = TINY_PNG_V2 if "v2026.05.0" in url else TINY_PNG_V1
        if return_final_url:
            return png, url
        return png

    with patch("mat_vis_client.client._get", side_effect=fake_get):
        data = c.fetch_texture("ambientcg", "Rock064", "color", tier="1k", tag="v2026.05.0")
    assert data == TINY_PNG_V2, "tag override should fetch v2 bytes"


def test_fetch_texture_tag_override_does_not_mutate_client(tmp_cache):
    """Passing tag= must not change the client's default tag."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    _prime(c, MOCK_MANIFEST_V1)
    alt = c.at("v2026.05.0")
    _prime(alt, MOCK_MANIFEST_V2)

    def fake_get(url, headers=None, return_final_url=False):
        png = TINY_PNG_V2 if "v2026.05.0" in url else TINY_PNG_V1
        if return_final_url:
            return png, url
        return png

    with patch("mat_vis_client.client._get", side_effect=fake_get):
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
