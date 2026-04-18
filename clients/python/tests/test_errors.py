"""Tests for typed mat_vis_client errors (#85 item 2).

Every error surfaced to callers should be a ``MatVisError`` subclass —
no bare ``urllib.error.HTTPError`` / ``urllib.error.URLError`` leakage.
Error types carry structured attributes (url, code, kind, key, available)
so callers can branch without string-matching messages.
"""

from __future__ import annotations

from unittest.mock import patch
import urllib.error

import pytest

from mat_vis_client import MatVisError, RateLimitError
from mat_vis_client.client import _lookup


# ── Error hierarchy ────────────────────────────────────────────


def test_not_found_error_is_matvis_error():
    from mat_vis_client import NotFoundError

    assert issubclass(NotFoundError, MatVisError)


def test_material_not_found_error_hierarchy():
    from mat_vis_client import MaterialNotFoundError, NotFoundError

    assert issubclass(MaterialNotFoundError, NotFoundError)


def test_tier_source_channel_not_found_hierarchy():
    from mat_vis_client import (
        ChannelNotFoundError,
        NotFoundError,
        SourceNotFoundError,
        TierNotFoundError,
    )

    assert issubclass(SourceNotFoundError, NotFoundError)
    assert issubclass(TierNotFoundError, NotFoundError)
    assert issubclass(ChannelNotFoundError, NotFoundError)


def test_http_fetch_error_is_matvis_error():
    from mat_vis_client import HTTPFetchError

    assert issubclass(HTTPFetchError, MatVisError)


def test_network_error_is_matvis_error():
    from mat_vis_client import NetworkError

    assert issubclass(NetworkError, MatVisError)


# ── NotFoundError structure ────────────────────────────────────


def test_material_not_found_carries_structured_fields():
    from mat_vis_client import MaterialNotFoundError

    err = MaterialNotFoundError(
        key="Rock999", available=["Rock064", "Rock063"], context="ambientcg/1k"
    )
    assert err.key == "Rock999"
    assert err.available == ["Rock064", "Rock063"]
    assert err.context == "ambientcg/1k"
    # Message includes available list (actionable)
    msg = str(err)
    assert "Rock999" in msg
    assert "Rock064" in msg  # "did you mean" hint


# ── _lookup raises typed subclasses by kind ────────────────────


def test_lookup_raises_material_not_found_for_material_kind():
    from mat_vis_client import MaterialNotFoundError

    with pytest.raises(MaterialNotFoundError) as exc:
        _lookup({"Rock064": {}}, "Rock999", kind="material", context="ambientcg/1k")
    assert exc.value.key == "Rock999"
    assert "Rock064" in exc.value.available


def test_lookup_raises_source_not_found_for_source_kind():
    from mat_vis_client import SourceNotFoundError

    with pytest.raises(SourceNotFoundError):
        _lookup({"ambientcg": {}}, "polyhaven", kind="source")


def test_lookup_raises_tier_not_found_for_tier_kind():
    from mat_vis_client import TierNotFoundError

    with pytest.raises(TierNotFoundError):
        _lookup({"1k": {}}, "8k", kind="tier")


def test_lookup_raises_channel_not_found_for_channel_kind():
    from mat_vis_client import ChannelNotFoundError

    with pytest.raises(ChannelNotFoundError):
        _lookup({"color": {}}, "colosr", kind="channel", context="Rock064")


def test_lookup_falls_back_to_matvis_error_for_unknown_kind():
    """Backwards compat: unknown kinds still raise MatVisError."""
    with pytest.raises(MatVisError):
        _lookup({"a": 1}, "b", kind="widget")


# ── HTTP errors are wrapped ────────────────────────────────────


def test_get_wraps_http_error_into_http_fetch_error():
    """_get() must translate urllib.HTTPError into HTTPFetchError for non-rate-limit codes."""
    from mat_vis_client import HTTPFetchError
    from mat_vis_client.client import _get

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(HTTPFetchError) as exc:
            _get("https://example.com/missing.png")
        assert exc.value.code == 404
        assert exc.value.url == "https://example.com/missing.png"


def test_get_wraps_url_error_into_network_error():
    """_get() must translate urllib.URLError (after retries) into NetworkError."""
    from mat_vis_client import NetworkError
    from mat_vis_client.client import _get

    def fake_urlopen(req, timeout=60):
        raise urllib.error.URLError("connection reset")

    # patch sleep so retries are instant
    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("time.sleep"),
    ):
        with pytest.raises(NetworkError):
            _get("https://example.com/x")


def test_rate_limit_still_raises_rate_limit_error():
    """Rate-limited 429 after retries → RateLimitError (unchanged behavior)."""
    from mat_vis_client.client import _get

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {"Retry-After": "1"}, None
        )

    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("time.sleep"),
    ):
        with pytest.raises(RateLimitError):
            _get("https://example.com/x")


# ── fetch_texture surfaces typed errors ────────────────────────


def test_fetch_texture_404_raises_material_not_found_or_http_fetch_error():
    """A 404 during fetch must surface as HTTPFetchError (not urllib leakage)."""
    from mat_vis_client import HTTPFetchError, MatVisClient

    MOCK_MANIFEST = {
        "schema_version": 1,
        "release_tag": "v2026.04.0",
        "tiers": {
            "1k": {
                "base_url": "https://example.com/",
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

    client = MatVisClient(cache_dir="/tmp/mat-vis-test-errors", update_check=False)
    # Inject manifest + rowmap
    client._manifest = MOCK_MANIFEST
    client._rowmap_cache = {("ambientcg", "1k"): MOCK_ROWMAP}

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("time.sleep"),
    ):
        with pytest.raises(HTTPFetchError) as exc:
            client.fetch_texture("ambientcg", "Rock064", "color", tier="1k")
        # Must NOT be a bare urllib error
        assert not isinstance(exc.value, urllib.error.HTTPError)
        # But HTTPFetchError carries the code
        assert exc.value.code == 404
