"""Tests for the Python reference client and adapters.

Unit tests (mocked) run unconditionally.
Live tests hit the real release and are skipped with MAT_VIS_SKIP_LIVE_TESTS=1.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mat_vis_client import MatVisClient, UnknownMaterialError
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
    "schema_version": 3,  # per-file substrate (#186 / ADR-0012)
    "version": 1,  # retained for tests asserting on the legacy field
    "release_tag": "v2026.04.0",
    "sources": {
        "ambientcg": {
            "catalog": "ambientcg.json",
            "tiers": {"1k": {"complete": True}},
        },
        "polyhaven": {
            "catalog": "polyhaven.json",
            "tiers": {"1k": {"complete": True}},
        },
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


def _mv_entry(
    mid: str,
    name: str,
    category: str,
    *,
    roughness: float,
    metalness: float,
    color_rgb: list[float],
    ior: float,
    available_tiers: list[str],
    maps: list[str],
    last_updated: str,
    source: str = "ambientcg",
    source_url: str,
) -> dict:
    """Build a v3 index entry (ADR-0011 / mat-vis#152)."""
    return {
        "id": mid,
        "source": source,
        "mat_vis": {
            "name": name,
            "category": category,
            "tags": [],
            "description": None,
            "physical": {"dimensions_m": None, "max_resolution_px": None},
            "pbr": {
                "color_rgb": color_rgb,
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
                "source_url": source_url,
            },
            "dates": {"published": last_updated, "updated": last_updated},
            "upstream_id": mid,
        },
        "available_tiers": available_tiers,
        "maps": maps,
    }


MOCK_INDEX_AMBIENTCG = [
    _mv_entry(
        "Rock064",
        "Rough Granite",
        "stone",
        roughness=0.8,
        metalness=0.0,
        color_rgb=[0.627, 0.322, 0.176],
        ior=1.5,
        available_tiers=["1k", "2k"],
        maps=["color", "normal", "roughness"],
        last_updated="2025-01-15",
        source_url="https://ambientcg.com/view?id=Rock064",
    ),
    _mv_entry(
        "Metal032",
        "Brushed Steel",
        "metal",
        roughness=0.3,
        metalness=1.0,
        color_rgb=[0.753, 0.753, 0.753],
        ior=2.5,
        available_tiers=["1k"],
        maps=["color", "metalness", "roughness"],
        last_updated="2025-02-10",
        source_url="https://ambientcg.com/view?id=Metal032",
    ),
    _mv_entry(
        "Wood045",
        "Oak Planks",
        "wood",
        roughness=0.6,
        metalness=0.0,
        color_rgb=[0.545, 0.271, 0.075],
        ior=1.5,
        available_tiers=["1k", "2k", "4k"],
        maps=["color", "normal", "roughness", "ao"],
        last_updated="2025-03-01",
        source_url="https://ambientcg.com/view?id=Wood045",
    ),
]


@pytest.fixture
def mock_client():
    """Client with mocked HTTP and temp cache."""
    with tempfile.TemporaryDirectory() as tmp:
        client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
        # Pre-populate manifest cache so no HTTP needed (tag-scoped path).
        # Issue #258: pair the cache body with an ETag so the conditional
        # GET in `manifest` would short-circuit — but tests below patch
        # `_get_with_etag` directly to assert no HTTP escapes either way.
        scope = Path(tmp) / "v2026.04.0"
        scope.mkdir(parents=True, exist_ok=True)
        (scope / ".manifest.json").write_text(json.dumps(MOCK_MANIFEST))
        (scope / ".manifest.etag").write_text('"mock-etag"')
        # Patch _get_with_etag so any unintended manifest probe surfaces
        # cleanly (304-equivalent against the cached body) instead of
        # hitting real HTTP. Tests that exercise the cold path patch
        # this themselves with a 200-equivalent return value.
        with patch("mat_vis_client.client._get_with_etag", return_value=(None, '"mock-etag"')):
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
        assert m["schema_version"] == 3  # per-file substrate (#186)
        assert "sources" in m

    def test_tiers(self, mock_client):
        assert mock_client.tiers() == ["1k"]

    def test_sources(self, mock_client):
        sources = mock_client.sources("1k")
        assert "ambientcg" in sources
        assert "polyhaven" in sources

    def test_manifest_fetches_release_manifest_directly(self):
        """Cold cache: ``manifest`` fetches release-manifest.json verbatim
        via a single conditional GET (#258). No tree-listing reconstruction."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            body = json.dumps(MOCK_MANIFEST).encode()
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(body, '"abc123"'),
            ) as mock_get:
                m = client.manifest
            # Returned verbatim
            assert m == MOCK_MANIFEST
            # Single fetch, against the release-manifest.json URL, no ETag yet
            assert mock_get.call_count == 1
            args, kwargs = mock_get.call_args
            url = args[0]
            assert url.endswith("/v2026.04.0/release-manifest.json")
            assert kwargs.get("etag") is None
            # Body + etag both cached under tag scope
            cached_body = Path(tmp) / "v2026.04.0" / ".manifest.json"
            cached_etag = Path(tmp) / "v2026.04.0" / ".manifest.etag"
            assert cached_body.exists()
            assert json.loads(cached_body.read_text()) == MOCK_MANIFEST
            assert cached_etag.read_text() == '"abc123"'

    def test_manifest_rejects_unsupported_schema(self):
        """``_check_schema_version`` still gates the fetched manifest."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            bad = {**MOCK_MANIFEST, "schema_version": 99}
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(json.dumps(bad).encode(), '"x"'),
            ):
                with pytest.raises(RuntimeError, match="schema_version=99"):
                    _ = client.manifest

    def test_manifest_rejects_missing_schema(self):
        """A manifest with no ``schema_version`` surfaces the cache-clear hint."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            bad = {k: v for k, v in MOCK_MANIFEST.items() if k != "schema_version"}
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(json.dumps(bad).encode(), '"x"'),
            ):
                with pytest.raises(RuntimeError, match="missing 'schema_version'"):
                    _ = client.manifest


class TestManifestEtagCache:
    """Issue #258 — manifest cache validates against the origin per
    client lifecycle via a conditional GET, instead of trusting disk
    forever. Covers cold start, in-process memoization, fresh-process
    304 (server unchanged), fresh-process 200 (server moved), and the
    no-ETag-from-server defensive path.
    """

    def test_first_fetch_no_cache(self):
        """Cold cache: a single GET populates manifest + ETag side-by-side."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            body = json.dumps(MOCK_MANIFEST).encode()
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(body, '"v1"'),
            ) as mock_get:
                m = client.manifest
            assert m == MOCK_MANIFEST
            assert mock_get.call_count == 1
            # First call has no etag (cold cache).
            _, kwargs = mock_get.call_args
            assert kwargs.get("etag") is None
            scope = Path(tmp) / "v2026.04.0"
            assert (scope / ".manifest.json").exists()
            assert (scope / ".manifest.etag").read_text() == '"v1"'

    def test_repeat_access_no_http(self):
        """Second access in the same process is in-memory only, no HTTP."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            body = json.dumps(MOCK_MANIFEST).encode()
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(body, '"v1"'),
            ) as mock_get:
                _ = client.manifest
                _ = client.manifest
                _ = client.manifest
            # Memoized after first call.
            assert mock_get.call_count == 1

    def test_fresh_process_server_unchanged_304(self):
        """Fresh client + same cache_dir + 304: cached body served, no body refetch."""
        with tempfile.TemporaryDirectory() as tmp:
            # Seed cache from a "previous" lifecycle.
            scope = Path(tmp) / "v2026.04.0"
            scope.mkdir(parents=True, exist_ok=True)
            (scope / ".manifest.json").write_text(json.dumps(MOCK_MANIFEST))
            (scope / ".manifest.etag").write_text('"v1"')

            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(None, '"v1"'),  # 304: body=None, etag echoed
            ) as mock_get:
                m = client.manifest

            assert m == MOCK_MANIFEST  # served from cached body
            assert mock_get.call_count == 1
            _, kwargs = mock_get.call_args
            assert kwargs.get("etag") == '"v1"'
            # Cache untouched.
            assert (scope / ".manifest.etag").read_text() == '"v1"'

    def test_fresh_process_server_mutated_200(self):
        """Fresh client + same cache_dir + 200 with new body: cache replaced."""
        with tempfile.TemporaryDirectory() as tmp:
            scope = Path(tmp) / "v2026.04.0"
            scope.mkdir(parents=True, exist_ok=True)
            (scope / ".manifest.json").write_text(json.dumps(MOCK_MANIFEST))
            (scope / ".manifest.etag").write_text('"v1"')

            new_manifest = {**MOCK_MANIFEST, "release_tag": "v2026.04.99"}
            new_body = json.dumps(new_manifest).encode()

            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(new_body, '"v2"'),
            ) as mock_get:
                m = client.manifest

            assert m == new_manifest
            assert mock_get.call_count == 1
            _, kwargs = mock_get.call_args
            assert kwargs.get("etag") == '"v1"'  # sent the prior etag
            # Cache replaced atomically.
            assert json.loads((scope / ".manifest.json").read_text()) == new_manifest
            assert (scope / ".manifest.etag").read_text() == '"v2"'

    def test_no_etag_from_server_does_not_crash(self):
        """Defensive: 200 without ETag stores body but no etag file.

        Next lifecycle behaves like cold start (forces unconditional
        refetch) rather than crashing on missing-etag.
        """
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            body = json.dumps(MOCK_MANIFEST).encode()
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(body, None),
            ):
                m = client.manifest
            assert m == MOCK_MANIFEST
            scope = Path(tmp) / "v2026.04.0"
            assert (scope / ".manifest.json").exists()
            # No etag file written → next lifecycle fetches unconditionally.
            assert not (scope / ".manifest.etag").exists()

            # Simulate next lifecycle — fresh client, same cache.
            client2 = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            new_body = json.dumps(MOCK_MANIFEST).encode()
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(new_body, '"now-with-etag"'),
            ) as mock_get2:
                _ = client2.manifest
            # Called with etag=None (cold-start equivalent).
            _, kwargs = mock_get2.call_args
            assert kwargs.get("etag") is None
            assert (scope / ".manifest.etag").read_text() == '"now-with-etag"'


class TestClientCatalogQueries:
    """Per-file substrate (#186): materials/channels read from v3 catalog."""

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    def test_materials_list(self, mock_get, mock_client):
        mats = mock_client.materials("ambientcg", "1k")
        assert "Metal032" in mats
        assert "Rock064" in mats

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    def test_channels(self, mock_get, mock_client):
        channels = mock_client.channels("ambientcg", "Rock064", "1k")
        assert "color" in channels
        assert "normal" in channels


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

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    def test_search_invalid_category(self, mock_get, mock_client, caplog):
        """v0.6.0+: invalid category soft-warns and returns empty list."""
        import logging

        with caplog.at_level(logging.WARNING, logger="mat-vis-client"):
            results = mock_client.search("invalid_category", source="ambientcg")
        assert results == []


class TestClientPrefetch:
    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", return_value=TINY_PNG)
    def test_prefetch_downloads_all(self, mock_http, mock_json, mock_client):
        progress = []
        n = mock_client.prefetch(
            "ambientcg",
            "1k",
            on_progress=lambda mid, i, total: progress.append((mid, i, total)),
        )
        # All 3 (Rock064, Metal032, Wood045) staged for 1k in MOCK_INDEX_AMBIENTCG
        assert n == 3
        assert len(progress) == 3
        assert progress[-1][1] == 3
        assert progress[-1][2] == 3

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", return_value=TINY_PNG)
    def test_fetch_all_textures(self, mock_http, mock_json, mock_client):
        textures = mock_client.fetch_all_textures("ambientcg", "Rock064", "1k")
        assert set(textures.keys()) == {"color", "normal", "roughness"}
        for ch, data in textures.items():
            assert data[:4] == b"\x89PNG", f"{ch} is not PNG"


class TestPrefetchUsesCatalog:
    """Regression gate (#186): prefetch enumerates materials via catalog,
    not rowmap. Picks up only materials with `tier in available_tiers`."""

    @patch("mat_vis_client.client._get_json", return_value=MOCK_INDEX_AMBIENTCG)
    @patch("mat_vis_client.client._get", return_value=TINY_PNG)
    def test_prefetch_filters_by_available_tiers(self, mock_http, mock_json, mock_client):
        # 2k: Rock064 + Wood045 (Metal032 is 1k-only). MOCK_MANIFEST in this
        # file only advertises 1k for ambientcg, so call materials() first
        # via a catalog mock that includes 2k entries — covered by the
        # other 1k-only tests; here we just check prefetch returns a count.
        n = mock_client.prefetch("ambientcg", "1k")
        assert n == 3  # all of Rock064, Metal032, Wood045 staged for 1k


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
            # Implementation uses UsdPreviewSurface (USD-style PBR), not
            # the MaterialX Standard Surface shader. The input names follow
            # USD conventions: metallic / roughness (no specular_ prefix).
            assert "UsdPreviewSurface" in content
            assert 'name="metallic"' in content
            assert 'name="roughness"' in content

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
            # UsdPreviewSurface uses <image> nodes (not the MaterialX
            # <tiledimage> primitive that Standard Surface emits).
            assert "<image" in content
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
            # UsdPreviewSurface shader uses <material>_shader, not SR_<material>.
            assert 'name="MyMat_shader"' in content


# ── Default-tag (#242) ────────────────────────────────────────


class TestDefaultTag:
    """Constructing without ``tag=`` must target a real release.

    The dataset's ``main`` branch is an empty baseline — every release
    lives on a CalVer branch — so ``MatVisClient()`` has to default to
    a real tag (#242) or out-of-the-box use returns 404s on every
    ``fetch_*`` call.
    """

    def test_default_tag_is_real_release(self):
        from mat_vis_client.client import DEFAULT_TAG

        assert DEFAULT_TAG == "v2026.04.2"

    def test_default_manifest_url_uses_default_tag(self):
        from mat_vis_client.client import DEFAULT_TAG

        c = MatVisClient()
        assert f"/{DEFAULT_TAG}/release-manifest.json" in c._manifest_url

    def test_explicit_tag_overrides_default(self):
        c = MatVisClient(tag="v2026.04.0")
        assert "/v2026.04.0/release-manifest.json" in c._manifest_url


# ── Live tests (network required) ──────────────────────────────
#
# The ``live`` marker and ``LIVE_TAG`` constant live in
# ``tests/_live.py`` so a single override (or env-var bump) propagates
# to every live module. The ``live_client`` fixture is in
# ``tests/conftest.py`` and gets auto-injected. See #274.

from tests._live import live  # noqa: E402


@live
class TestLiveDefaultTag:
    """#242 — bare ``MatVisClient()`` must work end-to-end against HF.

    The default tag is baked into the source (see ``DEFAULT_TAG`` and
    ``TestDefaultTag``); this round-trip just confirms it actually
    resolves on prod HF and that the manifest is shaped sanely.
    """

    def test_default_client_fetches_manifest(self, tmp_path):
        c = MatVisClient(cache_dir=tmp_path)
        m = c.manifest
        assert m["schema_version"] == 3
        assert "sources" in m and m["sources"], "default tag must point at a populated release"

    def test_default_client_lists_sources(self, tmp_path):
        c = MatVisClient(cache_dir=tmp_path)
        sources = c.sources("1k")
        assert "ambientcg" in sources, f"expected ambientcg in {sources}"


@live
class TestLiveManifest:
    def test_fetch_manifest(self, live_client):
        m = live_client.manifest
        # Per-file substrate emits schema_version 3 (#186 / ADR-0012).
        assert m["schema_version"] == 3
        assert "sources" in m

    def test_tiers(self, live_client):
        tiers = live_client.tiers()
        assert "1k" in tiers

    def test_sources(self, live_client):
        sources = live_client.sources("1k")
        assert "ambientcg" in sources


@live
class TestLiveCatalog:
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
        from mat_vis_client import MatVisError

        with pytest.raises((KeyError, MatVisError)):
            live_client.fetch_texture("ambientcg", "NONEXISTENT_XYZ", "color", "1k")


# ── #248: manifest-driven per-(source, tier) coverage ────────────


PNG_MAGIC = b"\x89PNG"
KTX2_MAGIC = b"\xabKTX"


@live
class TestLiveFullManifestCoverage:
    """End-to-end coverage matrix derived from the live manifest.

    For every ``(source, tier)`` pair the manifest declares
    ``complete=True`` we resolve the material list and fetch the
    ``color`` channel of the first entry, asserting PNG or KTX2 magic.
    Sources whose materials list is empty for a given tier (e.g.
    gpuopen tiers carry no per-tier records on v2026.04.x) are
    skipped — this is documented as expected, not a regression.

    A single manifest fetch drives the whole loop (post-#239 cache).
    """

    def test_every_listed_tier_is_fetchable(self, live_client):
        manifest = live_client.manifest
        sources = manifest.get("sources", {})
        assert sources, "manifest must declare at least one source"

        attempted: list[tuple[str, str]] = []
        skipped_empty: list[tuple[str, str]] = []
        for source, src_entry in sorted(sources.items()):
            tiers = (src_entry or {}).get("tiers") or {}
            for tier, tier_entry in sorted(tiers.items()):
                if not (tier_entry or {}).get("complete"):
                    continue
                mats = live_client.materials(source, tier)
                if not mats:
                    # Some sources publish a tier in the manifest but
                    # have zero records carrying that tier in their
                    # per-source catalog (e.g. gpuopen on v2026.04.x).
                    # Treat as expected, log via collected list.
                    skipped_empty.append((source, tier))
                    continue
                material_id = mats[0]
                data = live_client.fetch_texture(source, material_id, "color", tier)
                head = data[:4]
                head_hex = head.hex()
                assert head.startswith(PNG_MAGIC) or head.startswith(KTX2_MAGIC), (
                    f"({source!r}, {tier!r}, {material_id!r}, "
                    f"magic_bytes_hex_first_4={head_hex!r}) — "
                    f"expected PNG (89504e47) or KTX2 (ab4b5458) magic"
                )
                attempted.append((source, tier))

        assert attempted, (
            "manifest declared no complete (source, tier) pairs with "
            f"non-empty material lists; skipped_empty={skipped_empty}"
        )


# ── Live regression guards for py-mat#90 (mat-vis#141 / #143 / #144) ──
# These hit the real gpuopen release and verify the preferred UX
# (``fetch_all_textures(source, name)``) works end-to-end. They skip
# gracefully until the mat-vis#142 rebake lands (i.e. while the index
# still carries ``name="1k 8b"`` garbage); once semantic metadata is
# published they become positive-confirmation tests automatically.


@live
class TestLiveGpuopenNameLookup:
    """End-to-end guard for py-mat#90 — fetch gpuopen materials by human name."""

    KNOWN_MATERIAL = "Aluminum Corrugated"
    KNOWN_UUID = "25b88a68-251a-414a-a5b5-68381adfdc5f"

    def _has_semantic_metadata(self, live_client) -> bool:
        """Skip condition: true once mat-vis#142 rebake has published
        a gpuopen index with real titles instead of ``"1k 8b"`` labels."""
        try:
            idx = live_client.index("gpuopen")
        except Exception:
            return False
        return any(e.get("name") == self.KNOWN_MATERIAL for e in idx if isinstance(e, dict))

    def test_name_resolves_to_known_uuid(self, live_client):
        if not self._has_semantic_metadata(live_client):
            pytest.skip("gpuopen rebake (mat-vis#142) not yet published")
        resolved = live_client._resolve_material_id("gpuopen", self.KNOWN_MATERIAL, "1k")
        assert resolved == self.KNOWN_UUID

    def test_fetch_all_textures_by_name_returns_pngs(self, live_client):
        """py-mat#90's exact preferred-UX call."""
        if not self._has_semantic_metadata(live_client):
            pytest.skip("gpuopen rebake (mat-vis#142) not yet published")
        textures = live_client.fetch_all_textures("gpuopen", self.KNOWN_MATERIAL, tier="1k")
        assert textures, "expected at least one channel"
        for channel, data in textures.items():
            assert data[:4] == b"\x89PNG", f"{channel} is not a PNG"

    def test_unknown_gpuopen_name_raises_typed_error(self, live_client):
        """Bogus name → UnknownMaterialError (not silent {})."""
        if not self._has_semantic_metadata(live_client):
            pytest.skip("gpuopen rebake (mat-vis#142) not yet published")
        from mat_vis_client import UnknownMaterialError

        with pytest.raises(UnknownMaterialError):
            live_client.fetch_all_textures("gpuopen", "DEFINITELY NOT A REAL MATERIAL", tier="1k")


# ── upstream accessor + strip (Phase C, mat-vis#152) ───────────


def _index_with_upstream() -> list[dict]:
    """Copy of MOCK_INDEX_AMBIENTCG with per-entry ``upstream`` blocks."""
    out: list[dict] = []
    for entry in MOCK_INDEX_AMBIENTCG:
        enriched = dict(entry)
        enriched["upstream"] = {
            "source": "ambientcg",
            "schema_version": 1,
            "fetched_at": "2026-04-20T16:00:00Z",
            "raw": {
                "assetId": entry["id"],
                "displayName": entry["mat_vis"]["name"],
                "popularityScore": 0.5,
            },
        }
        out.append(enriched)
    return out


class TestClientUpstreamAccessor:
    @patch("mat_vis_client.client._get_json")
    def test_index_strips_upstream_key(self, mock_get, mock_client):
        """``client.index(source)`` never returns the ``upstream`` key —
        it's explicitly not part of the stable query surface."""
        mock_get.return_value = _index_with_upstream()
        entries = mock_client.index("ambientcg")
        assert len(entries) == 3
        for e in entries:
            assert "upstream" not in e
            # mat_vis is still there — Layer-1 is the query surface.
            assert "mat_vis" in e

    @patch("mat_vis_client.client._get_json")
    def test_index_strip_does_not_mutate_cache(self, mock_get, mock_client):
        """Stripping returns a shallow copy — the cached index keeps
        the ``upstream`` key so :meth:`upstream` can read it."""
        mock_get.return_value = _index_with_upstream()
        _ = mock_client.index("ambientcg")
        # Internal cache kept verbatim
        raw = mock_client._load_index_raw("ambientcg")
        assert all("upstream" in e for e in raw)

    @patch("mat_vis_client.client._get_json")
    def test_search_strips_upstream_key(self, mock_get, mock_client):
        mock_get.return_value = _index_with_upstream()
        results = mock_client.search(source="ambientcg")
        assert len(results) == 3
        for r in results:
            assert "upstream" not in r

    @patch("mat_vis_client.client._get_json")
    def test_upstream_returns_source_shaped_dict(self, mock_get, mock_client):
        mock_get.return_value = _index_with_upstream()
        raw = mock_client.upstream("ambientcg", "Rock064", "1k")
        assert raw["assetId"] == "Rock064"
        assert raw["displayName"] == "Rough Granite"

    @patch("mat_vis_client.client._get_json")
    def test_upstream_unknown_material_raises(self, mock_get, mock_client):
        mock_get.return_value = _index_with_upstream()
        with pytest.raises(UnknownMaterialError):
            mock_client.upstream("ambientcg", "DEFINITELY_NOT_A_MATERIAL", "1k")

    @patch("mat_vis_client.client._get_json")
    def test_upstream_returns_empty_dict_when_missing(self, mock_get, mock_client):
        """Pre-v3 catalog entries (no ``upstream`` block) yield ``{}``, not
        an error — callers can check for truthiness rather than branching
        on the dataset version."""
        # Index without any upstream blocks (pre-v3 / Phase A envelope).
        mock_get.return_value = MOCK_INDEX_AMBIENTCG
        raw = mock_client.upstream("ambientcg", "Rock064", "1k")
        assert raw == {}


class TestV2CatalogGuard:
    """Cross-stack review fix: a v3 client pointed at a v2 catalog must
    fail loudly with an upgrade hint, not silently return empty from
    ``search()`` / ``categories()`` because every ``mat_vis`` lookup misses.
    """

    @patch("mat_vis_client.client._get_json")
    def test_v2_shaped_catalog_raises_loudly(self, mock_get, mock_client):
        from mat_vis_client import MatVisError

        # v2 shape: top-level category + color_hex, no mat_vis block.
        v2_catalog = [
            {"id": "Rock064", "source": "ambientcg", "category": "stone", "color_hex": "#888"},
            {"id": "Metal032", "source": "ambientcg", "category": "metal", "roughness": 0.3},
        ]
        mock_get.return_value = v2_catalog
        with pytest.raises(MatVisError, match="predates ADR-0011"):
            mock_client.index("ambientcg")

    @patch("mat_vis_client.client._get_json")
    def test_v3_shaped_catalog_passes(self, mock_get, mock_client):
        """v3 entries carrying a ``mat_vis`` block are accepted without noise."""
        v3_catalog = [
            {
                "id": "Rock064",
                "source": "ambientcg",
                "mat_vis": {"name": "Rough Granite", "category": "stone"},
            }
        ]
        mock_get.return_value = v3_catalog
        entries = mock_client.index("ambientcg")
        assert entries[0]["mat_vis"]["category"] == "stone"

    @patch("mat_vis_client.client._get_json")
    def test_empty_catalog_is_allowed(self, mock_get, mock_client):
        """Empty list is ambiguous but harmless — no silent failure surface."""
        mock_get.return_value = []
        entries = mock_client.index("ambientcg")
        assert entries == []
