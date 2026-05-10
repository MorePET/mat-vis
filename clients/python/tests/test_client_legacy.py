"""Legacy half of the Python reference client suite (#274).

Pre-#274 this lived as ``clients/python/test_client.py`` and was
collected by an explicit ``pytest test_client.py -v`` arg from the
Dagger ``test_client_python`` function. Its companion at
``tests/test_client.py`` was collected by the standard pytest
``testpaths`` config. The two suites had disjoint coverage and the
nested ``LIVE_TAG`` had drifted to ``v2026.04.1`` after the prod bump.

#274 consolidated both under ``tests/`` so a single ``pytest`` run
collects everything and a single ``LIVE_TAG`` (``tests/_live.py``)
governs the live skip-by-default suite. Class names overlap with
``test_client.py`` (``TestLiveManifest`` etc.) — they remain distinct
under pytest because the module path is part of the node ID. No tests
were renamed during the move.

Unit tests (mocked) run unconditionally.
Live tests hit the real release and are skipped unless
``MAT_VIS_LIVE_TESTS=1``.
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


def _mock_get(*args, **kwargs):
    """Test double for client._get — mirrors its ``return_final_url`` contract.

    Real ``_get`` returns ``bytes`` normally and ``(bytes, url)`` when
    ``return_final_url=True``; this helper matches both. Returns
    :data:`TINY_PNG` as the byte payload.
    """
    if kwargs.get("return_final_url"):
        return TINY_PNG, args[0] if args else ""
    return TINY_PNG


MOCK_MANIFEST = {
    "schema_version": 3,
    "release_tag": "v2026.04.1",
    "sources": {
        "ambientcg": {
            "catalog": "ambientcg.json",
            "materials_count": 3,
            "tiers": {"1k": {"complete": True}, "2k": {"complete": True}},
        },
        "polyhaven": {
            "catalog": "polyhaven.json",
            "materials_count": 1,
            "tiers": {"1k": {"complete": True}},
        },
        "gpuopen": {
            "catalog": "gpuopen.json",
            "materials_count": 1,
            "tiers": {"1k": {"complete": True}},
        },
    },
}


def _v3_entry(
    mid: str,
    name: str,
    category: str,
    *,
    roughness: float,
    metalness: float,
    ior: float,
    available_tiers: list[str],
    maps: list[str],
    updated: str,
) -> dict:
    """ADR-0011 v3-shaped catalog entry: semantic fields live under mat_vis."""
    return {
        "id": mid,
        "source": "ambientcg",
        "mat_vis": {
            "name": name,
            "category": category,
            "tags": [],
            "description": None,
            "physical": {"dimensions_m": None, "max_resolution_px": None},
            "pbr": {
                "color_rgb": None,
                "roughness": roughness,
                "metalness": metalness,
                "ior": ior,
                "specular_f0": None,
                "transmission": None,
                "complex_ior": None,
            },
            "attribution": {
                "authors": [],
                "license_spdx": "CC0-1.0",
                "source_url": f"https://ambientcg.com/view?id={mid}",
            },
            "dates": {"published": updated, "updated": updated},
            "upstream_id": mid,
        },
        "upstream": {
            "source": "ambientcg",
            "schema_version": 1,
            "fetched_at": "2026-04-24T00:00:00Z",
            "raw": {},
        },
        "available_tiers": available_tiers,
        "maps": maps,
        "texture_hashes": {},
        "status": "ok",
        "needs_mtlx_bake": False,
    }


MOCK_INDEX_AMBIENTCG = [
    _v3_entry(
        "Rock064",
        "Rough Granite",
        "stone",
        roughness=0.8,
        metalness=0.0,
        ior=1.5,
        available_tiers=["1k", "2k"],
        maps=["color", "normal", "roughness"],
        updated="2025-01-15",
    ),
    _v3_entry(
        "Metal032",
        "Brushed Steel",
        "metal",
        roughness=0.3,
        metalness=1.0,
        ior=2.5,
        available_tiers=["1k"],
        maps=["color", "metalness", "roughness"],
        updated="2025-02-10",
    ),
    _v3_entry(
        "Wood045",
        "Oak Planks",
        "wood",
        roughness=0.6,
        metalness=0.0,
        ior=1.5,
        available_tiers=["1k", "2k", "4k"],
        maps=["color", "normal", "roughness", "ao"],
        updated="2025-03-01",
    ),
]


@pytest.fixture
def mock_client():
    """Client with mocked HTTP and temp cache."""
    with tempfile.TemporaryDirectory() as tmp:
        client = MatVisClient(tag="v2026.04.1", cache_dir=Path(tmp))
        # Pre-populate manifest cache at the tag-scoped path. Issue #258
        # added an ETag sibling — without it the conditional GET on
        # first .manifest access would fall back to an unconditional
        # fetch and clobber our test mocks. Pre-set the in-memory
        # _manifest too so no HTTP is issued at all.
        # mat-vis#384: layout is now
        # <cache_dir>/<client-version>/<repo-slug>/<tag>/.
        scope = client._cache_scope
        scope.mkdir(parents=True, exist_ok=True)
        (scope / ".manifest.json").write_text(json.dumps(MOCK_MANIFEST))
        (scope / ".manifest.etag").write_text('"mock"')
        client._manifest = MOCK_MANIFEST
        # Suppress the background update-check HTTP calls that would
        # otherwise consume our mocked _get_json side_effect iterations.
        client._update_warned = True
        yield client


@pytest.fixture
def mock_search_client():
    """Client with a richer manifest (multiple rowmaps per source) so
    search() discovers the full category set. Used by search tests that
    reference categories not in the default single-rowmap fixture."""
    # v0.6.0: no per-category partitioning (ADR-0007). The "rich"
    # variant just raises the materials_count to match the full catalog
    # used by search tests; category breadth comes from the catalog
    # itself, not the manifest.
    rich_manifest = json.loads(json.dumps(MOCK_MANIFEST))  # deep copy
    rich_manifest["sources"]["ambientcg"]["materials_count"] = 3
    with tempfile.TemporaryDirectory() as tmp:
        client = MatVisClient(tag="v2026.04.1", cache_dir=Path(tmp))
        # mat-vis#384: layout includes repo slug.
        scope = client._cache_scope
        scope.mkdir(parents=True, exist_ok=True)
        (scope / ".manifest.json").write_text(json.dumps(rich_manifest))
        (scope / ".manifest.etag").write_text('"mock"')
        client._manifest = rich_manifest
        client._update_warned = True
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
        assert m["schema_version"] == 3  # per-file substrate (#186 / ADR-0012)
        assert "sources" in m

    def test_tiers(self, mock_client):
        # ambientcg has 1k+2k staged in MOCK_MANIFEST; polyhaven/gpuopen 1k.
        assert mock_client.tiers() == ["1k", "2k"]

    def test_sources(self, mock_client):
        sources = mock_client.sources("1k")
        assert "ambientcg" in sources
        assert "polyhaven" in sources


# ── #64 update-check DX: logging + TTY gating ──────────────────


def _fresh_client(cache_dir: Path) -> MatVisClient:
    """Client with MOCK_MANIFEST pre-cached but the update-check flag
    NOT suppressed — lets tests exercise the TTY / env-var gating path.
    Issue #258: pair the cached body with an ETag; the tests below
    additionally patch ``_get_with_etag`` to return 304-equivalent so
    the conditional GET on first ``manifest`` access doesn't escape.
    """
    client = MatVisClient(tag="v2026.04.1", cache_dir=cache_dir)
    # mat-vis#384: layout includes repo slug.
    scoped = client._cache_scope
    scoped.mkdir(parents=True, exist_ok=True)
    (scoped / ".manifest.json").write_text(json.dumps(MOCK_MANIFEST))
    (scoped / ".manifest.etag").write_text('"mock"')
    return client


class TestUpdateCheckLogging:
    """#64 — library-friendly update notices via logging, not stderr."""

    def test_no_stderr_on_non_tty(self, capsys):
        """Library import (non-TTY stderr) must not write to stderr."""
        with tempfile.TemporaryDirectory() as tmp:
            client = _fresh_client(Path(tmp))
            with (
                patch("sys.stderr.isatty", return_value=False),
                patch.dict(os.environ, {}, clear=False),
            ):
                # Make sure neither env var is forcing behavior.
                os.environ.pop("MAT_VIS_NO_UPDATE_CHECK", None)
                os.environ.pop("MAT_VIS_UPDATE_CHECK", None)
                # Re-read module constants since they're captured at import.
                import mat_vis_client.client as mc

                with (
                    patch.object(mc, "UPDATE_CHECK_DISABLED", False),
                    patch.object(mc, "UPDATE_CHECK_FORCED", False),
                    # Issue #258: 304-equivalent stub so the conditional
                    # GET in `manifest` doesn't escape to real HTTP.
                    patch(
                        "mat_vis_client.client._get_with_etag",
                        return_value=(None, '"mock"'),
                    ),
                    patch.object(
                        client,
                        "check_updates",
                        return_value={
                            "data": {
                                "current": "v2026.04.0",
                                "latest": "v2026.05.0",
                                "newer_available": True,
                            },
                            "client": {
                                "current": "0.2.0",
                                "latest": "0.2.1",
                                "newer_available": True,
                            },
                        },
                    ),
                ):
                    _ = client.manifest  # triggers _maybe_warn_updates

            captured = capsys.readouterr()
            assert captured.err == "", f"Expected no stderr output, got: {captured.err!r}"

    def test_log_info_on_tty(self, caplog):
        """TTY stderr + newer available → one INFO record per kind, via the
        ``mat-vis-client`` logger. No stderr bleeding.
        """
        import mat_vis_client.client as mc

        with tempfile.TemporaryDirectory() as tmp:
            client = _fresh_client(Path(tmp))
            with (
                patch("sys.stderr.isatty", return_value=True),
                patch.object(mc, "UPDATE_CHECK_DISABLED", False),
                patch.object(mc, "UPDATE_CHECK_FORCED", False),
                patch(
                    "mat_vis_client.client._get_with_etag",
                    return_value=(None, '"mock"'),
                ),
                patch.object(
                    client,
                    "check_updates",
                    return_value={
                        "data": {
                            "current": "v2026.04.0",
                            "latest": "v2026.05.0",
                            "newer_available": True,
                        },
                        "client": {
                            "current": "0.2.0",
                            "latest": "0.2.1",
                            "newer_available": True,
                        },
                    },
                ),
                caplog.at_level("INFO", logger="mat-vis-client"),
            ):
                _ = client.manifest

            messages = [r.getMessage() for r in caplog.records if r.name == "mat-vis-client"]
            assert any("newer data release" in m for m in messages), messages
            assert any("newer version" in m for m in messages), messages

    def test_force_check_env_var_overrides_non_tty(self, caplog):
        """``MAT_VIS_UPDATE_CHECK=1`` forces the check even without a TTY."""
        import mat_vis_client.client as mc

        with tempfile.TemporaryDirectory() as tmp:
            client = _fresh_client(Path(tmp))
            with (
                patch("sys.stderr.isatty", return_value=False),
                patch.object(mc, "UPDATE_CHECK_DISABLED", False),
                patch.object(mc, "UPDATE_CHECK_FORCED", True),
                patch(
                    "mat_vis_client.client._get_with_etag",
                    return_value=(None, '"mock"'),
                ),
                patch.object(
                    client,
                    "check_updates",
                    return_value={
                        "data": {
                            "current": "v2026.04.0",
                            "latest": "v2026.05.0",
                            "newer_available": True,
                        },
                        "client": {
                            "current": None,
                            "latest": None,
                            "newer_available": False,
                        },
                    },
                ),
                caplog.at_level("INFO", logger="mat-vis-client"),
            ):
                _ = client.manifest

            messages = [r.getMessage() for r in caplog.records if r.name == "mat-vis-client"]
            assert any("newer data release" in m for m in messages), messages

    def test_opt_out_env_var_wins_over_force(self, caplog, capsys):
        """``MAT_VIS_NO_UPDATE_CHECK=1`` takes precedence over everything."""
        import mat_vis_client.client as mc

        with tempfile.TemporaryDirectory() as tmp:
            client = _fresh_client(Path(tmp))
            with (
                patch("sys.stderr.isatty", return_value=True),
                patch.object(mc, "UPDATE_CHECK_DISABLED", True),
                patch.object(mc, "UPDATE_CHECK_FORCED", True),
                patch(
                    "mat_vis_client.client._get_with_etag",
                    return_value=(None, '"mock"'),
                ),
                patch.object(client, "check_updates") as mock_chk,
                caplog.at_level("INFO", logger="mat-vis-client"),
            ):
                _ = client.manifest
                # We never even call check_updates when disabled.
                assert mock_chk.call_count == 0

            assert [r for r in caplog.records if r.name == "mat-vis-client"] == []
            assert capsys.readouterr().err == ""


# ── #69 schema_version strictness ──────────────────────────────


class TestSchemaVersionStrict:
    """#69 — client requires ``schema_version``; no legacy fallback."""

    def _write_manifest(self, tmp: Path, data: dict) -> Path:
        # mat-vis#384: layout includes repo slug.
        scoped = Path(tmp) / "v0.6" / "gerchowl__mat-vis" / "v2026.04.0"
        scoped.mkdir(parents=True, exist_ok=True)
        mf = scoped / ".manifest.json"
        mf.write_text(json.dumps(data))
        # Issue #258: pair the body with an ETag so the manifest
        # property's conditional GET can short-circuit (304) instead of
        # falling back to an unconditional refetch and clobbering our
        # poisoned-payload test fixture.
        (scoped / ".manifest.etag").write_text('"mock"')
        return mf

    def test_rejects_manifest_without_schema_version(self):
        """A manifest with only ``version: 1`` raises RuntimeError."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            legacy = {k: v for k, v in MOCK_MANIFEST.items() if k != "schema_version"}
            assert "schema_version" not in legacy
            self._write_manifest(tmp, legacy)
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(None, '"mock"'),
            ):
                with pytest.raises(RuntimeError, match="schema_version"):
                    _ = client.manifest

    def test_error_message_mentions_cache_clear(self):
        """Recovery path is surfaced in the error message."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            self._write_manifest(tmp, {"version": 1})
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(None, '"mock"'),
            ):
                with pytest.raises(RuntimeError) as excinfo:
                    _ = client.manifest
            msg = str(excinfo.value)
            assert "cache clear" in msg
            assert ".manifest.json" in msg

    def test_rejects_incompatible_schema_version(self):
        """A manifest with a future ``schema_version`` still raises."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            future = {**MOCK_MANIFEST, "schema_version": 99}
            self._write_manifest(tmp, future)
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(None, '"mock"'),
            ):
                with pytest.raises(RuntimeError, match="does not support"):
                    _ = client.manifest


class TestClientCatalogQueries:
    """Per-file substrate (#186): materials/channels are read from the
    v3 catalog, not a separate rowmap. Lock the catalog-driven contract
    as a regression gate."""

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    def test_materials_filters_by_available_tiers(self, mock_get, mock_client):
        """Only materials whose available_tiers contains the queried tier."""
        # 1k: Rock064, Metal032, Wood045 all qualify
        mats_1k = mock_client.materials("ambientcg", "1k")
        assert sorted(mats_1k) == ["Metal032", "Rock064", "Wood045"]
        # 2k: Metal032 is 1k-only → excluded
        mats_2k = mock_client.materials("ambientcg", "2k")
        assert sorted(mats_2k) == ["Rock064", "Wood045"]

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    def test_channels_from_catalog_maps(self, mock_get, mock_client):
        """channels() reads the catalog entry's `maps` list."""
        chs = mock_client.channels("ambientcg", "Rock064", "1k")
        assert chs == ["color", "normal", "roughness"]


class TestClientSearch:
    @patch("mat_vis_client.client._get_json")
    def test_search_by_category(self, mock_get, mock_search_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_search_client.search("metal", source="ambientcg")
        assert len(results) == 1
        assert results[0]["id"] == "Metal032"

    @patch("mat_vis_client.client._get_json")
    def test_search_by_roughness_range(self, mock_get, mock_search_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_search_client.search(roughness_range=(0.5, 0.9), source="ambientcg")
        # Rock064 (0.8) and Wood045 (0.6) match
        ids = {r["id"] for r in results}
        assert ids == {"Rock064", "Wood045"}

    @patch("mat_vis_client.client._get_json")
    def test_search_by_metalness_range(self, mock_get, mock_search_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_search_client.search(metalness_range=(0.9, 1.0), source="ambientcg")
        assert len(results) == 1
        assert results[0]["id"] == "Metal032"

    @patch("mat_vis_client.client._get_json")
    def test_search_combined_filters(self, mock_get, mock_search_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_search_client.search(
            "stone",
            roughness_range=(0.5, 1.0),
            source="ambientcg",
        )
        assert len(results) == 1
        assert results[0]["id"] == "Rock064"

    @patch("mat_vis_client.client._get_json")
    def test_search_tier_filter(self, mock_get, mock_search_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        # Metal032 is only available in 1k
        results = mock_search_client.search("metal", source="ambientcg", tier="2k")
        assert len(results) == 0

    @patch("mat_vis_client.client._get_json")
    def test_search_no_filters_returns_all(self, mock_get, mock_search_client):
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_search_client.search(source="ambientcg")
        assert len(results) == 3

    @patch("mat_vis_client.client._get_json")
    def test_search_invalid_category_returns_empty(self, mock_get, mock_client, caplog):
        """Invalid category soft-warns and returns empty rather than raising.

        Raising would force consumers to validate against a moving-target
        category set that's discovered from the manifest. An empty list
        is the honest answer for "find materials in a category that has
        none" and is friendlier to tooling (no exception handling required).
        """
        import logging

        # v0.6.0 derives categories from per-source catalog entries; mock
        # each `.index()` call with the fixture.
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        with caplog.at_level(logging.WARNING, logger="mat-vis-client"):
            results = mock_client.search("invalid_category")
        assert results == []
        assert any("invalid_category" in rec.message for rec in caplog.records)


class TestClientPrefetch:
    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_prefetch_downloads_all(self, mock_http, mock_json, mock_client):
        """All materials with `tier in available_tiers` get prefetched."""
        progress = []
        n = mock_client.prefetch(
            "ambientcg",
            "1k",
            on_progress=lambda mid, i, total: progress.append((mid, i, total)),
        )
        assert n == 3  # Rock064, Metal032, Wood045 all have 1k
        assert len(progress) == 3
        assert progress[-1][1] == 3
        assert progress[-1][2] == 3

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_fetch_all_textures(self, mock_http, mock_json, mock_client):
        textures = mock_client.fetch_all_textures("ambientcg", "Rock064", "1k")
        assert set(textures.keys()) == {"color", "normal", "roughness"}
        for ch, data in textures.items():
            assert data[:4] == b"\x89PNG", f"{ch} is not PNG"


class TestReadmeExamplesRun:
    """Item F regression: the README's advertised client.search call must
    run as written. Previously shipped ``client.search("marble")`` — the
    positional arg was accepted but ``marble`` isn't a canonical category,
    so the example silently returned 0 results.
    """

    @patch("mat_vis_client.client._get_json")
    def test_search_by_category_and_roughness_range(self, mock_get, mock_search_client):
        # The new README example form — kwargs, canonical category, and
        # a scalar range. Must run and return results.
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        results = mock_search_client.search(category="stone", roughness_range=(0.4, 0.9))
        assert isinstance(results, list)
        ids = {r["id"] for r in results}
        assert "Rock064" in ids, f"stone search should find Rock064; got {ids}"


class TestRateLimitRetry:
    """Item I: retry branches in ``_get`` for rate-limit variants and
    URLError (network-level failures). Current client retries:

      * HTTP 429 / 503 — always
      * HTTP 403 with ``X-RateLimit-Remaining: 0`` header
      * HTTP 403 with "rate limit" in body
      * ``URLError`` — any network-level (DNS / reset / timeout)

    Non-rate-limit 4xx/5xx pass through unchanged."""

    @staticmethod
    def _http_error(code: int, *, headers=None, body: bytes = b""):
        import io
        from urllib.error import HTTPError

        return HTTPError("http://test/x", code, "err", headers or {}, io.BytesIO(body))

    @staticmethod
    def _ok_response(data: bytes = b"OK"):
        # Minimal urllib response: context manager + .read() + .url.
        class _Resp:
            url = "http://final/x"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return data

        return _Resp()

    @patch("mat_vis_client.client.time.sleep")  # no real waits
    @patch("mat_vis_client.client.urllib.request.urlopen")
    def test_403_with_ratelimit_remaining_zero_retries(self, mock_open, _sleep):
        from mat_vis_client.client import _get

        mock_open.side_effect = [
            self._http_error(403, headers={"X-RateLimit-Remaining": "0"}),
            self._ok_response(b"yay"),
        ]
        assert _get("http://test/x") == b"yay"
        assert mock_open.call_count == 2

    @patch("mat_vis_client.client.time.sleep")
    @patch("mat_vis_client.client.urllib.request.urlopen")
    def test_403_with_rate_limit_body_retries(self, mock_open, _sleep):
        from mat_vis_client.client import _get

        mock_open.side_effect = [
            self._http_error(403, headers={}, body=b"API rate limit exceeded"),
            self._ok_response(b"ok"),
        ]
        assert _get("http://test/x") == b"ok"
        assert mock_open.call_count == 2

    @patch("mat_vis_client.client.time.sleep")
    @patch("mat_vis_client.client.urllib.request.urlopen")
    def test_503_retries(self, mock_open, _sleep):
        from mat_vis_client.client import _get

        mock_open.side_effect = [
            self._http_error(503),
            self._ok_response(b"ok"),
        ]
        assert _get("http://test/x") == b"ok"

    @patch("mat_vis_client.client.time.sleep")
    @patch("mat_vis_client.client.urllib.request.urlopen")
    def test_504_gateway_timeout_retries(self, mock_open, _sleep):
        """Regression: GitHub Releases edge regularly returns 504 under load.
        Pre-0.4.0 these propagated as hard errors; now they retry like 503."""
        from mat_vis_client.client import _get

        mock_open.side_effect = [
            self._http_error(504),
            self._ok_response(b"ok"),
        ]
        assert _get("http://test/x") == b"ok"

    @patch("mat_vis_client.client.time.sleep")
    @patch("mat_vis_client.client.urllib.request.urlopen")
    def test_502_bad_gateway_retries(self, mock_open, _sleep):
        from mat_vis_client.client import _get

        mock_open.side_effect = [
            self._http_error(502),
            self._ok_response(b"ok"),
        ]
        assert _get("http://test/x") == b"ok"

    @patch("mat_vis_client.client.time.sleep")
    @patch("mat_vis_client.client.urllib.request.urlopen")
    def test_urlerror_retries(self, mock_open, _sleep):
        from urllib.error import URLError

        from mat_vis_client.client import _get

        mock_open.side_effect = [
            URLError("connection reset"),
            self._ok_response(b"ok"),
        ]
        assert _get("http://test/x") == b"ok"

    @patch("mat_vis_client.client.time.sleep")
    @patch("mat_vis_client.client.urllib.request.urlopen")
    def test_non_ratelimit_403_is_not_retried(self, mock_open, _sleep):
        from mat_vis_client.client import _get, HTTPFetchError

        # Plain 403 with no rate-limit signal → typed HTTPFetchError (0.5+),
        # no retry. Used to raise urllib.HTTPError directly; now wrapped.
        mock_open.side_effect = [self._http_error(403)]
        with pytest.raises(HTTPFetchError):
            _get("http://test/x")
        assert mock_open.call_count == 1


class TestMtlxOriginalFetchError:
    """Item J: MtlxSource.original silently caches ``{}`` on fetch failure
    (intentional — per-call retries would hammer a broken endpoint). Pin
    the behavior in a test so the contract is documented."""

    @patch("mat_vis_client.client._get_json")
    def test_fetch_error_caches_empty_map_and_returns_none(self, mock_get_json, mock_client):
        from urllib.error import URLError

        mock_get_json.side_effect = URLError("boom")

        src = mock_client.mtlx("gpuopen", "any-id", "1k")
        # .original() checks presence against the upstream map; fetch fails
        # → empty map cached → returned as no-original-available.
        assert src.original() is None

        # Second call uses the cache — no new network hit.
        assert src.original() is None
        assert mock_get_json.call_count == 1


class TestFriendlyNotFoundErrors:
    """Item G: missing tier / source / material / channel must raise
    MatVisError with an Available-list suggestion, not a bare KeyError.
    Per-file substrate (#186) reads from the v3 catalog instead of a
    rowmap, but the friendly-error contract is unchanged."""

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_unknown_tier_suggests_available(self, mock_http, mock_json, mock_client):
        from mat_vis_client import MatVisError

        # MOCK_MANIFEST advertises 1k + 2k for ambientcg; "16k" is unknown.
        with pytest.raises(MatVisError, match=r"tier '16k' not found"):
            mock_client.fetch_texture("ambientcg", "Rock064", "color", "16k")

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_unknown_source_suggests_available(self, mock_http, mock_json, mock_client):
        from mat_vis_client import MatVisError

        with pytest.raises(MatVisError, match=r"source 'nope' not found"):
            mock_client.fetch_texture("nope", "Rock064", "color", "1k")

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_unknown_material_suggests_available(self, mock_http, mock_json, mock_client):
        from mat_vis_client import MatVisError

        with pytest.raises(MatVisError) as exc:
            mock_client.fetch_texture("ambientcg", "DOES_NOT_EXIST", "color", "1k")
        msg = str(exc.value)
        assert "material 'DOES_NOT_EXIST' not found" in msg
        assert "ambientcg/1k" in msg
        # #286: available list now shows human names (with id fallback)
        # — these v3 entries carry display names so we get those.
        assert "Rough Granite" in msg
        assert "Brushed Steel" in msg

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_unknown_channel_suggests_available(self, mock_http, mock_json, mock_client):
        from mat_vis_client import MatVisError

        # Rock064's catalog `maps` is [color, normal, roughness] — no "displacement".
        with pytest.raises(MatVisError) as exc:
            mock_client.fetch_texture("ambientcg", "Rock064", "displacement", "1k")
        msg = str(exc.value)
        assert "channel 'displacement' not found" in msg
        assert "ambientcg/1k/Rock064" in msg
        assert "color" in msg
        assert "normal" in msg


class TestTierCompleteSentinel:
    """Per-file substrate atomicity gate (#186 / ADR-0012).

    The .tier_complete sentinel is the final commit per tier. Probing
    it on the first fetch_texture for a (source, tier) means a partial
    bake never serves half-baked bytes."""

    def test_missing_sentinel_raises(self, mock_client):
        """If the HEAD probe fails, fetch_texture raises a friendly error."""
        from mat_vis_client import MatVisError

        # Catalog returns OK; sentinel HEAD raises (simulating a partial
        # bake where the .tier_complete commit hasn't landed).
        def _get(url, **_kw):
            if url.endswith("/.tier_complete"):
                raise OSError("404")
            return TINY_PNG

        with (
            patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG),
            patch("mat_vis_client.client._get", side_effect=_get),
        ):
            with pytest.raises(MatVisError, match="not atomically complete"):
                mock_client.fetch_texture("ambientcg", "Rock064", "color", "1k")

    def test_sentinel_probe_cached_per_tier(self, mock_client):
        """Two fetches in the same (source, tier) probe the sentinel once."""
        sentinel_calls = 0

        def _get(url, **_kw):
            nonlocal sentinel_calls
            if url.endswith("/.tier_complete"):
                sentinel_calls += 1
            return TINY_PNG

        with (
            patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG),
            patch("mat_vis_client.client._get", side_effect=_get),
        ):
            mock_client.fetch_texture("ambientcg", "Rock064", "color", "1k")
            mock_client.fetch_texture("ambientcg", "Rock064", "normal", "1k")

        assert sentinel_calls == 1, (
            f"sentinel HEAD must be cached per (source, tier); got {sentinel_calls} probes"
        )


class TestPerFileFetchUrlShape:
    """Lock the per-file URL contract — clients depend on this exact
    layout post-#186. Any future refactor that changes the URL must
    update this gate explicitly."""

    def test_url_is_source_tier_mid_channel_png(self, mock_client):
        captured: list[str] = []

        def _get(url, **_kw):
            captured.append(url)
            if url.endswith("/.tier_complete"):
                return b"v0.0.0\n"
            return TINY_PNG

        with (
            patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG),
            patch("mat_vis_client.client._get", side_effect=_get),
        ):
            mock_client.fetch_texture("ambientcg", "Rock064", "color", "1k")

        png_urls = [u for u in captured if u.endswith(".png")]
        assert len(png_urls) == 1, f"expected one PNG GET, got {len(png_urls)}: {captured}"
        assert png_urls[0].endswith("/ambientcg/1k/Rock064/color.png")

    def test_falls_back_to_ktx2_on_404(self, mock_client):
        """Channels available only in KTX2 form (e.g. derived ktx2-1k tiers)."""
        captured: list[str] = []

        def _get(url, **_kw):
            captured.append(url)
            if url.endswith("/.tier_complete"):
                return b"v0.0.0\n"
            if url.endswith(".png"):
                raise OSError("404")  # PNG missing → fall back to KTX2
            return b"\xabKTX 20\xbb\r\n\x1a\n" + b"\x00" * 100

        with (
            patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG),
            patch("mat_vis_client.client._get", side_effect=_get),
        ):
            data = mock_client.fetch_texture("ambientcg", "Rock064", "color", "1k")

        assert data.startswith(b"\xabKTX 20\xbb\r\n\x1a\n")
        png_urls = [u for u in captured if u.endswith(".png")]
        ktx2_urls = [u for u in captured if u.endswith(".ktx2")]
        assert len(png_urls) == 1 and len(ktx2_urls) == 1


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
        result = to_threejs(
            {"metalness": 1.0, "roughness": 0.3, "color_hex": "#C0C0C0"},
            color_format="int",
        )
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
        # mat-vis#290: ior=1.5 (the spec default) is now suppressed —
        # use a non-default value to verify the extension is emitted.
        result = to_gltf({"ior": 1.6})
        assert result["extensions"]["KHR_materials_ior"]["ior"] == 1.6

    def test_transmission_extension(self):
        result = to_gltf({"transmission": 0.8})
        ext = result["extensions"]["KHR_materials_transmission"]
        assert ext["transmissionFactor"] == 0.8

    def test_packed_texture_note(self):
        # #91 renamed the marker; when Pillow is installed the packing
        # path runs and no marker is emitted. Coverage for the no-Pillow
        # fallback lives in tests/test_adapters.py.
        from mat_vis_client.adapters import Image as _PIL

        if _PIL is not None:
            import pytest

            pytest.skip("Pillow installed: packing path covered in test_adapters.py")
        result = to_gltf(
            {},
            {"metalness": TINY_PNG, "roughness": TINY_PNG},
        )
        pbr = result["pbrMetallicRoughness"]
        assert "_note_no_pillow" in pbr

    def test_empty_scalars(self):
        result = to_gltf({})
        assert result == {"pbrMetallicRoughness": {}}


# ── MaterialX adapter tests ────────────────────────────────────


class TestExportMtlx:
    def test_scalars_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_mtlx(
                {"roughness": 0.3, "ior": 1.5},
                output_dir=tmp,
                material_name="TestMat",
            )
            assert path.exists()
            assert path.suffix == ".mtlx"

            content = path.read_text()
            assert "UsdPreviewSurface" in content
            assert 'name="roughness"' in content
            assert 'name="ior"' in content

    def test_with_textures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_mtlx(
                {},
                {"color": TINY_PNG, "normal": TINY_PNG},
                output_dir=tmp,
                material_name="TexMat",
            )
            assert path.exists()

            # Check PNG files were written
            assert (Path(tmp) / "TexMat_color.png").exists()
            assert (Path(tmp) / "TexMat_normal.png").exists()

            content = path.read_text()
            assert "<image " in content
            assert "<nodegraph " in content
            assert "TexMat_color.png" in content
            # Normal maps should have a normalmap node
            assert "<normalmap " in content

    def test_texture_dir_mode(self):
        """Test generating mtlx from existing texture files on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            tex_dir = Path(tmp) / "textures"
            tex_dir.mkdir()
            (tex_dir / "color.png").write_bytes(TINY_PNG)
            (tex_dir / "roughness.png").write_bytes(TINY_PNG)

            path = export_mtlx(
                {},
                output_dir=tmp,
                material_name="DirMat",
                texture_dir=str(tex_dir),
                channels=["color", "roughness"],
            )
            assert path.exists()
            content = path.read_text()
            assert "color.png" in content
            assert "roughness.png" in content
            # Should NOT write new PNG files to output_dir
            assert not (Path(tmp) / "DirMat_color.png").exists()

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
            assert "UsdPreviewSurface" in content

    def test_srgb_colorspace_on_color(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_mtlx(
                {},
                {"color": TINY_PNG, "emission": TINY_PNG, "roughness": TINY_PNG},
                output_dir=tmp,
                material_name="CS",
            )
            content = path.read_text()
            # color and emission get srgb_texture, roughness does not
            assert content.count('colorspace="srgb_texture"') == 2

    def test_scalar_fallback_when_no_texture(self):
        """Scalar roughness should appear only when no roughness texture."""
        with tempfile.TemporaryDirectory() as tmp:
            # With texture: no scalar
            path = export_mtlx(
                {"roughness": 0.5},
                {"roughness": TINY_PNG},
                output_dir=tmp,
                material_name="WithTex",
            )
            content = path.read_text()
            # roughness input should connect to nodegraph, not be a scalar value
            assert 'output="out_roughness"' in content

            # Without texture: scalar
            path2 = export_mtlx(
                {"roughness": 0.5},
                {},
                output_dir=tmp,
                material_name="NoTex",
            )
            content2 = path2.read_text()
            assert 'value="0.5"' in content2


# ── Client materialize + to_mtlx tests ───────────────────────


class TestMaterialize:
    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_materialize_writes_pngs(self, mock_http, mock_json, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            tex_dir = mock_client.materialize("ambientcg", "Rock064", "1k", tmp)
            assert tex_dir.is_dir()
            assert (tex_dir / "color.png").exists()
            assert (tex_dir / "normal.png").exists()
            assert (tex_dir / "roughness.png").exists()
            assert (tex_dir / "color.png").read_bytes()[:4] == b"\x89PNG"

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_materialize_skips_existing(self, mock_http, mock_json, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            # First call writes
            mock_client.materialize("ambientcg", "Rock064", "1k", tmp)
            call_count_1 = mock_http.call_count

            # Second call should skip (files exist)
            mock_client.materialize("ambientcg", "Rock064", "1k", tmp)
            assert mock_http.call_count == call_count_1  # no new HTTP calls


# ── MtlxSource façade tests ────────────────────────────────────


class TestMtlxSource:
    """Tests for the dotted client.mtlx(...).xml / .export / .original API."""

    def test_synthesized_creation_is_lazy(self, mock_client):
        """Creating the façade must not trigger any HTTP calls."""
        with (
            patch("mat_vis_client.client._get_json") as mock_json,
            patch("mat_vis_client.client._get") as mock_get,
        ):
            source = mock_client.mtlx("ambientcg", "Rock064", "1k")
            assert source.source == "ambientcg"
            assert source.material_id == "Rock064"
            assert source.tier == "1k"
            assert source.is_original is False
            assert mock_json.call_count == 0
            assert mock_get.call_count == 0

    @patch("mat_vis_client.client._get_json")
    def test_synthesized_xml_does_not_fetch_pngs(self, mock_json, mock_client):
        """.xml only needs the rowmap + index, no texture byte fetches."""
        mock_json.return_value = MOCK_INDEX_AMBIENTCG
        with patch("mat_vis_client.client._get") as mock_get:
            xml = mock_client.mtlx("ambientcg", "Rock064", "1k").xml()
            # No _get (which fetches PNG bytes) should have been called.
            assert mock_get.call_count == 0

        assert xml.startswith("<?xml")
        assert 'version="1.38"' in xml
        assert "UsdPreviewSurface" in xml
        assert "color.png" in xml

    @patch("mat_vis_client.client._get_json")
    def test_synthesized_xml_is_cached(self, mock_json, mock_client):
        """Second .xml access returns the cached string."""
        mock_json.return_value = MOCK_INDEX_AMBIENTCG
        source = mock_client.mtlx("ambientcg", "Rock064", "1k")
        xml1 = source.xml()
        xml2 = source.xml()
        assert xml1 is xml2

    @patch("mat_vis_client.client._get_json")
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_synthesized_export_writes_files(self, mock_http, mock_json, mock_client):
        """.export(path) writes channel PNGs + a .mtlx file."""
        mock_json.return_value = MOCK_INDEX_AMBIENTCG
        with tempfile.TemporaryDirectory() as tmp:
            mtlx_path = mock_client.mtlx("ambientcg", "Rock064", "1k").export(tmp)
            assert mtlx_path.exists()
            assert mtlx_path.suffix == ".mtlx"
            assert mtlx_path.parent.name == "Rock064"
            # PNG channels written
            assert (mtlx_path.parent / "color.png").exists()
            assert (mtlx_path.parent / "normal.png").exists()
            assert (mtlx_path.parent / "roughness.png").exists()
            # Document references them
            assert "color.png" in mtlx_path.read_text()

    @patch("mat_vis_client.client._get_json")
    def test_original_returns_none_for_ambientcg(self, mock_json, mock_client):
        """.original is None when the source has no upstream mtlx map."""
        mock_json.side_effect = Exception("404")
        source = mock_client.mtlx("ambientcg", "Rock064", "1k")
        assert source.original() is None

    @patch("mat_vis_client.client._get_json")
    def test_original_returns_none_for_unknown_material(self, mock_json, mock_client):
        """.original is None when the material isn't in the upstream map."""
        mock_json.return_value = {"other-uuid": "<materialx version='1.38'/>"}
        source = mock_client.mtlx("gpuopen", "nonexistent-uuid", "1k")
        assert source.original() is None

    @patch("mat_vis_client.client._get_json")
    def test_original_returns_mtlxsource_for_gpuopen(self, mock_json, mock_client):
        """.original returns a new MtlxSource when upstream exists."""
        upstream_xml = '<?xml version="1.0"?><materialx version="1.38"><nodegraph/></materialx>'
        mock_json.return_value = {"test-uuid": upstream_xml}
        source = mock_client.mtlx("gpuopen", "test-uuid", "1k")
        orig = source.original()
        assert orig is not None
        assert orig.is_original is True
        assert orig.source == "gpuopen"
        assert orig.material_id == "test-uuid"

    @patch("mat_vis_client.client._get_json")
    def test_original_xml_returns_raw_upstream(self, mock_json, mock_client):
        """.original.xml returns the raw upstream XML, not rewritten."""
        upstream_xml = (
            '<?xml version="1.0"?><materialx version="1.38">'
            '<image name="img1"><input name="file" value="BaseColor.png"/></image>'
            "</materialx>"
        )
        mock_json.return_value = {"test-uuid": upstream_xml}
        xml = mock_client.mtlx("gpuopen", "test-uuid", "1k").original().xml()
        assert xml == upstream_xml

    def test_original_on_original_returns_none(self, mock_client):
        """Calling .original on an already-original source returns None."""
        from mat_vis_client import MtlxSource

        fake_original = MtlxSource(mock_client, "gpuopen", "x", "1k", is_original=True)
        assert fake_original.original() is None

    @patch("mat_vis_client.client._get_json")
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_original_export_rewrites_paths(self, mock_http, mock_json, mock_client):
        """.original.export(path) writes upstream XML with local texture paths."""
        upstream_xml = (
            '<?xml version="1.0"?><materialx version="1.38">'
            '<image name="img1"><input name="file" value="BaseColor.png"/></image>'
            '<image name="img2"><input name="file" value="Roughness.png"/></image>'
            "</materialx>"
        )
        # Calls in order: mtlx-originals map, then catalog (first
        # materialize/channels call hits _load_index_raw via the catalog).
        # Build a minimal v3 catalog entry for the gpuopen UUID.
        gpuopen_catalog = [
            _v3_entry(
                "test-uuid",
                "Test Material",
                "metal",
                roughness=0.5,
                metalness=1.0,
                ior=1.5,
                available_tiers=["1k"],
                maps=["color", "roughness"],
                updated="2025-01-01",
            ),
        ]
        # _v3_entry pins source="ambientcg" — fix it for this gpuopen test.
        gpuopen_catalog[0]["source"] = "gpuopen"
        gpuopen_catalog[0]["upstream"]["source"] = "gpuopen"
        mock_json.side_effect = [
            {"test-uuid": upstream_xml},
            gpuopen_catalog,
        ]
        with tempfile.TemporaryDirectory() as tmp:
            orig = mock_client.mtlx("gpuopen", "test-uuid", "1k").original()
            assert orig is not None
            mtlx_path = orig.export(tmp)
            content = mtlx_path.read_text()
            # Upstream filenames replaced with local paths
            assert "BaseColor.png" not in content
            assert "Roughness.png" not in content
            # gpuopen rowmap fixture exposes color + roughness channels.
            assert "color.png" in content
            assert "roughness.png" in content

    def test_original_check_caches_at_client_level(self, mock_client):
        """Repeated .original checks hit the client cache, not the network."""
        with patch("mat_vis_client.client._get_json") as mock_json:
            mock_json.return_value = {"test-uuid": "<materialx/>"}
            s1 = mock_client.mtlx("gpuopen", "test-uuid", "1k")
            s2 = mock_client.mtlx("gpuopen", "another-uuid", "1k")
            assert s1.original() is not None
            assert s2.original() is None
            # Only one network call for the whole source's map, shared by both.
            assert mock_json.call_count == 1


# ── Live tests (network required) ──────────────────────────────
#
# The ``live`` marker and ``LIVE_TAG`` constant live in
# ``tests/_live.py``; the ``live_client`` fixture is in
# ``tests/conftest.py``. Both modules in this dir share them. See #274.


from tests._live import LIVE_TAG, live  # noqa: E402

LIVE_SOURCE = "polyhaven"
LIVE_TIER = "1k"


@live
class TestLiveManifest:
    def test_fetch_manifest(self, live_client):
        m = live_client.manifest
        # Per-file substrate emits schema_version 3 (#186 / ADR-0012).
        # Old tar-substrate tags still emit 2 — accept both for backward
        # compat through the v0.6.x release cycle.
        assert m["schema_version"] in (2, 3)
        assert "sources" in m

    def test_tiers(self, live_client):
        tiers = live_client.tiers()
        assert LIVE_TIER in tiers

    def test_sources(self, live_client):
        sources = live_client.sources(LIVE_TIER)
        assert LIVE_SOURCE in sources


@live
class TestLiveCatalog:
    def test_materials_list(self, live_client):
        mats = live_client.materials(LIVE_SOURCE, LIVE_TIER)
        assert len(mats) > 0
        assert all(isinstance(m, str) for m in mats)

    def test_channels(self, live_client):
        mats = live_client.materials(LIVE_SOURCE, LIVE_TIER)
        channels = live_client.channels(LIVE_SOURCE, mats[0], LIVE_TIER)
        assert "color" in channels


@live
class TestLiveFetchTexture:
    def test_fetch_returns_png(self, live_client):
        mats = live_client.materials(LIVE_SOURCE, LIVE_TIER)
        data = live_client.fetch_texture(LIVE_SOURCE, mats[0], "color", LIVE_TIER)
        assert data[:4] == b"\x89PNG"
        assert len(data) > 1000

    def test_fetch_caches_locally(self, live_client):
        mats = live_client.materials(LIVE_SOURCE, LIVE_TIER)
        mid = mats[0]
        data1 = live_client.fetch_texture(LIVE_SOURCE, mid, "color", LIVE_TIER)
        data2 = live_client.fetch_texture(LIVE_SOURCE, mid, "color", LIVE_TIER)
        assert data1 == data2

    def test_fetch_multiple_channels(self, live_client):
        mats = live_client.materials(LIVE_SOURCE, LIVE_TIER)
        mid = mats[0]
        channels = live_client.channels(LIVE_SOURCE, mid, LIVE_TIER)
        for ch in channels[:3]:
            data = live_client.fetch_texture(LIVE_SOURCE, mid, ch, LIVE_TIER)
            assert data[:4] == b"\x89PNG", f"{mid}/{ch} is not PNG"

    def test_fetch_nonexistent_material_raises(self, live_client):
        from mat_vis_client import MatVisError

        with pytest.raises(MatVisError, match="material 'NONEXISTENT_XYZ' not found"):
            live_client.fetch_texture(LIVE_SOURCE, "NONEXISTENT_XYZ", "color", LIVE_TIER)


# ── Phase 2 proof (physicallybased scalar catalog, live on HF) ──


@live
def test_proof_phase_2_fetch_physicallybased_index_from_hf():
    """Scalar catalog for physicallybased is fetchable from HF substrate.

    Uses ``LIVE_TAG`` (env-overridable) so this stays current as
    the prod release tag rolls forward; #250 surfaced a hardcoded
    v2026.04.1 here that broke once that tag was sunset.
    """
    with tempfile.TemporaryDirectory() as tmp:
        client = MatVisClient(tag=LIVE_TAG, cache_dir=Path(tmp))
        idx = client.index("physicallybased")
    assert len(idx) >= 50, f"expected ≥50 PB entries, got {len(idx)}"
    assert all("id" in e and "source" in e for e in idx)
    assert all(e["source"] == "physicallybased" for e in idx)


class TestFetchTextureMagicAccepts:
    """Regression: fetch_texture must accept both PNG and KTX2 payloads.

    The 0.6.0 client initially hardcoded a PNG magic check that rejected
    KTX2 tiers — a live smoke bake to gerchowl/mat-vis-tst caught it.
    Per-file substrate (#186): magic-byte check still applies after the
    plain GET; cache key now uses the actual extension on disk.
    """

    def _make_client(self, tmp: Path, tier: str) -> MatVisClient:
        manifest = {
            "schema_version": 3,
            "release_tag": "test",
            "sources": {
                "polyhaven": {
                    "catalog": "polyhaven.json",
                    "materials_count": 1,
                    "tiers": {tier: {"complete": True}},
                },
            },
        }
        client = MatVisClient(tag="test", cache_dir=tmp)
        (tmp / "test").mkdir(parents=True, exist_ok=True)
        (tmp / "test" / ".manifest.json").write_text(json.dumps(manifest))
        (tmp / "test" / ".manifest.etag").write_text('"mock"')
        # Skip the conditional GET in `manifest` (#258) by pre-setting
        # the in-memory cache.
        client._manifest = manifest
        client._update_warned = True
        # Pre-load the in-memory catalog so _resolve_material_id finds "M".
        client._indexes["polyhaven"] = [
            _v3_entry(
                "M",
                "M",
                "other",
                roughness=0.5,
                metalness=0.0,
                ior=1.5,
                available_tiers=[tier],
                maps=["color"],
                updated="2025-01-01",
            ),
        ]
        client._indexes["polyhaven"][0]["source"] = "polyhaven"
        return client

    def test_accepts_png(self, tmp_path):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        client = self._make_client(tmp_path, "1k")
        with patch("mat_vis_client.client._get", return_value=png):
            data = client.fetch_texture("polyhaven", "M", "color", "1k")
        assert data == png

    def test_accepts_ktx2(self, tmp_path):
        ktx2 = b"\xabKTX 20\xbb\r\n\x1a\n" + b"\x00" * 100
        client = self._make_client(tmp_path, "ktx2-1k")

        def _get(url, **_kw):
            # PNG path 404s → fall back to KTX2.
            if url.endswith(".png"):
                raise OSError("404")
            return ktx2

        with patch("mat_vis_client.client._get", side_effect=_get):
            data = client.fetch_texture("polyhaven", "M", "color", "ktx2-1k")
        assert data == ktx2

    def test_rejects_non_png_non_ktx2(self, tmp_path):
        junk = b"NOPE" + b"\x00" * 100
        client = self._make_client(tmp_path, "1k")
        with patch("mat_vis_client.client._get", return_value=junk):
            with pytest.raises(ValueError, match="Expected PNG or KTX2"):
                client.fetch_texture("polyhaven", "M", "color", "1k")
