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
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from mat_vis_client import MatVisClient, _in_range  # noqa: E402
from adapters import (  # noqa: E402
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
    "schema_version": 1,
    "version": 1,  # retained for tests asserting on the legacy field
    "release_tag": "v2026.04.0",
    "tiers": {
        "1k": {
            "base_url": "https://example.com/releases/download/v2026.04.0/",
            "sources": {
                "ambientcg": {
                    # Single rowmap — legacy shape. Search tests that need
                    # broader category discovery override via mock_client_with_categories.
                    "parquet_files": ["mat-vis-ambientcg-1k-stone.parquet"],
                    "rowmap_files": ["ambientcg-1k-stone-rowmap.json"],
                    "rowmap_file": "ambientcg-1k-stone-rowmap.json",
                },
                "polyhaven": {
                    "parquet_files": ["mat-vis-polyhaven-1k-wood.parquet"],
                    "rowmap_files": ["polyhaven-1k-wood-rowmap.json"],
                    "rowmap_file": "polyhaven-1k-wood-rowmap.json",
                },
                "gpuopen": {
                    "parquet_files": ["mat-vis-gpuopen-1k-other.parquet"],
                    "rowmap_files": ["gpuopen-1k-other-rowmap.json"],
                    "rowmap_file": "gpuopen-1k-other-rowmap.json",
                },
            },
        }
    },
}

# Rowmap suitable for the gpuopen "test-uuid" material — mirrors the
# ambientcg fixture's structure so MtlxSource.original.export() has
# concrete channels to rewrite texture paths against.
MOCK_ROWMAP_GPUOPEN = {
    "parquet_file": "gpuopen-1k.parquet",
    "materials": {
        "test-uuid": {
            "color": {"offset": 0, "length": 1024},
            "roughness": {"offset": 1024, "length": 512},
        },
    },
}

MOCK_ROWMAP = {
    "parquet_file": "ambientcg-1k.parquet",
    "materials": {
        "Rock064": {
            "color": {"offset": 0, "length": 1024},
            "normal": {"offset": 1024, "length": 2048},
            "roughness": {"offset": 3072, "length": 512},
        },
        "Metal032": {
            "color": {"offset": 4000, "length": 800},
            "metalness": {"offset": 4800, "length": 600},
            "roughness": {"offset": 5400, "length": 500},
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
        # Suppress the background update-check HTTP calls that would
        # otherwise consume our mocked _get_json side_effect iterations.
        client._update_warned = True
        yield client


@pytest.fixture
def mock_search_client():
    """Client with a richer manifest (multiple rowmaps per source) so
    search() discovers the full category set. Used by search tests that
    reference categories not in the default single-rowmap fixture."""
    rich_manifest = json.loads(json.dumps(MOCK_MANIFEST))  # deep copy
    rich_manifest["tiers"]["1k"]["sources"]["ambientcg"].update(
        parquet_files=[
            "mat-vis-ambientcg-1k-stone.parquet",
            "mat-vis-ambientcg-1k-metal.parquet",
            "mat-vis-ambientcg-1k-wood.parquet",
        ],
        rowmap_files=[
            "ambientcg-1k-stone-rowmap.json",
            "ambientcg-1k-metal-rowmap.json",
            "ambientcg-1k-wood-rowmap.json",
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
        client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
        cache_path = Path(tmp) / ".manifest.json"
        cache_path.write_text(json.dumps(rich_manifest))
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
        assert m["schema_version"] == 1
        assert "tiers" in m

    def test_tiers(self, mock_client):
        assert mock_client.tiers() == ["1k"]

    def test_sources(self, mock_client):
        sources = mock_client.sources("1k")
        assert "ambientcg" in sources
        assert "polyhaven" in sources


# ── #64 update-check DX: logging + TTY gating ──────────────────


def _fresh_client(cache_dir: Path) -> MatVisClient:
    """Client with MOCK_MANIFEST pre-cached but the update-check flag
    NOT suppressed — lets tests exercise the TTY / env-var gating path.
    """
    client = MatVisClient(tag="v2026.04.0", cache_dir=cache_dir)
    (cache_dir / ".manifest.json").write_text(json.dumps(MOCK_MANIFEST))
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

    def test_rejects_manifest_without_schema_version(self):
        """A manifest with only ``version: 1`` raises RuntimeError."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            legacy = {k: v for k, v in MOCK_MANIFEST.items() if k != "schema_version"}
            assert "schema_version" not in legacy
            (Path(tmp) / ".manifest.json").write_text(json.dumps(legacy))
            with pytest.raises(RuntimeError, match="schema_version"):
                _ = client.manifest

    def test_error_message_mentions_cache_clear(self):
        """Recovery path is surfaced in the error message."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            (Path(tmp) / ".manifest.json").write_text(json.dumps({"version": 1}))
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
            (Path(tmp) / ".manifest.json").write_text(json.dumps(future))
            with pytest.raises(RuntimeError, match="does not support"):
                _ = client.manifest


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
        assert entry["color"]["parquet_file"] == "ambientcg-1k.parquet"


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

    def test_search_invalid_category_returns_empty(self, mock_client, caplog):
        """Invalid category soft-warns and returns empty rather than raising.

        Raising would force consumers to validate against a moving-target
        category set that's discovered from the manifest. An empty list
        is the honest answer for "find materials in a category that has
        none" and is friendlier to tooling (no exception handling required).
        """
        import logging

        with caplog.at_level(logging.WARNING, logger="mat-vis-client"):
            results = mock_client.search("invalid_category")
        assert results == []
        assert any("invalid_category" in rec.message for rec in caplog.records)


class TestClientPrefetch:
    @patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
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
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
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
    @patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_materialize_writes_pngs(self, mock_http, mock_json, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            tex_dir = mock_client.materialize("ambientcg", "Rock064", "1k", tmp)
            assert tex_dir.is_dir()
            assert (tex_dir / "color.png").exists()
            assert (tex_dir / "normal.png").exists()
            assert (tex_dir / "roughness.png").exists()
            assert (tex_dir / "color.png").read_bytes()[:4] == b"\x89PNG"

    @patch("mat_vis_client.client._get_json", return_value=MOCK_ROWMAP)
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_materialize_skips_existing(self, mock_http, mock_json, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            # First call writes
            mock_client.materialize("ambientcg", "Rock064", "1k", tmp)
            call_count_1 = mock_http.call_count

            # Second call should skip (files exist)
            mock_client.materialize("ambientcg", "Rock064", "1k", tmp)
            assert mock_http.call_count == call_count_1  # no new HTTP calls

    @patch("mat_vis_client.client._get_json")
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_to_mtlx_generates_valid_document(self, mock_http, mock_json, mock_client):
        # Deprecated but still functional.
        mock_json.side_effect = [MOCK_ROWMAP, MOCK_INDEX_AMBIENTCG, MOCK_ROWMAP]
        import warnings

        with tempfile.TemporaryDirectory() as tmp:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                mtlx_path = mock_client.to_mtlx("ambientcg", "Rock064", "1k", tmp)
            assert mtlx_path.exists()
            assert mtlx_path.suffix == ".mtlx"

            content = mtlx_path.read_text()
            assert "UsdPreviewSurface" in content
            assert "<nodegraph " in content
            assert "color.png" in content
            assert 'version="1.38"' in content


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
        mock_json.side_effect = [MOCK_ROWMAP, MOCK_INDEX_AMBIENTCG]
        with patch("mat_vis_client.client._get") as mock_get:
            xml = mock_client.mtlx("ambientcg", "Rock064", "1k").xml
            # No _get (which fetches PNG bytes) should have been called.
            assert mock_get.call_count == 0

        assert xml.startswith("<?xml")
        assert 'version="1.38"' in xml
        assert "UsdPreviewSurface" in xml
        assert "color.png" in xml

    @patch("mat_vis_client.client._get_json")
    def test_synthesized_xml_is_cached(self, mock_json, mock_client):
        """Second .xml access returns the cached string."""
        mock_json.side_effect = [MOCK_ROWMAP, MOCK_INDEX_AMBIENTCG]
        source = mock_client.mtlx("ambientcg", "Rock064", "1k")
        xml1 = source.xml
        xml2 = source.xml
        assert xml1 is xml2

    @patch("mat_vis_client.client._get_json")
    @patch("mat_vis_client.client._get", side_effect=_mock_get)
    def test_synthesized_export_writes_files(self, mock_http, mock_json, mock_client):
        """.export(path) writes channel PNGs + a .mtlx file."""
        mock_json.side_effect = [MOCK_ROWMAP, MOCK_INDEX_AMBIENTCG, MOCK_ROWMAP]
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
        assert source.original is None

    @patch("mat_vis_client.client._get_json")
    def test_original_returns_none_for_unknown_material(self, mock_json, mock_client):
        """.original is None when the material isn't in the upstream map."""
        mock_json.return_value = {"other-uuid": "<materialx version='1.38'/>"}
        source = mock_client.mtlx("gpuopen", "nonexistent-uuid", "1k")
        assert source.original is None

    @patch("mat_vis_client.client._get_json")
    def test_original_returns_mtlxsource_for_gpuopen(self, mock_json, mock_client):
        """.original returns a new MtlxSource when upstream exists."""
        upstream_xml = '<?xml version="1.0"?><materialx version="1.38"><nodegraph/></materialx>'
        mock_json.return_value = {"test-uuid": upstream_xml}
        source = mock_client.mtlx("gpuopen", "test-uuid", "1k")
        orig = source.original
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
        xml = mock_client.mtlx("gpuopen", "test-uuid", "1k").original.xml
        assert xml == upstream_xml

    def test_original_on_original_returns_none(self, mock_client):
        """Calling .original on an already-original source returns None."""
        from mat_vis_client import MtlxSource

        fake_original = MtlxSource(mock_client, "gpuopen", "x", "1k", is_original=True)
        assert fake_original.original is None

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
        # Calls in order: mtlx-originals map, rowmap (first materialize/channels call
        # populates the in-process rowmap cache — subsequent calls are cached).
        mock_json.side_effect = [
            {"test-uuid": upstream_xml},
            MOCK_ROWMAP_GPUOPEN,
        ]
        with tempfile.TemporaryDirectory() as tmp:
            orig = mock_client.mtlx("gpuopen", "test-uuid", "1k").original
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
            assert s1.original is not None
            assert s2.original is None
            # Only one network call for the whole source's map, shared by both.
            assert mock_json.call_count == 1

    def test_deprecated_to_mtlx_warns(self, mock_client):
        """client.to_mtlx emits DeprecationWarning pointing at .mtlx(...).export."""
        import warnings

        with (
            patch("mat_vis_client.client._get_json") as mock_json,
            patch("mat_vis_client.client._get", side_effect=_mock_get),
        ):
            mock_json.side_effect = [MOCK_ROWMAP, MOCK_INDEX_AMBIENTCG, MOCK_ROWMAP]
            with tempfile.TemporaryDirectory() as tmp:
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    mock_client.to_mtlx("ambientcg", "Rock064", "1k", tmp)
                assert any(
                    issubclass(wi.category, DeprecationWarning) and "mtlx(" in str(wi.message)
                    for wi in w
                )

    def test_deprecated_fetch_mtlx_original_warns(self, mock_client):
        """client.fetch_mtlx_original emits DeprecationWarning."""
        import warnings

        with patch("mat_vis_client.client._get_json", return_value={}):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                result = mock_client.fetch_mtlx_original("ambientcg", "Rock064")
            assert result is None
            assert any(issubclass(wi.category, DeprecationWarning) for wi in w)

    def test_deprecated_materialize_mtlx_warns(self, mock_client):
        """client.materialize_mtlx emits DeprecationWarning."""
        import warnings

        with (
            patch("mat_vis_client.client._get_json") as mock_json,
            patch("mat_vis_client.client._get", side_effect=_mock_get),
        ):
            # no-originals path: fetch originals (empty), then materialize + synthesize
            mock_json.side_effect = [{}, MOCK_ROWMAP, MOCK_INDEX_AMBIENTCG, MOCK_ROWMAP]
            with tempfile.TemporaryDirectory() as tmp:
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    mock_client.materialize_mtlx("ambientcg", "Rock064", "1k", tmp)
                assert any(issubclass(wi.category, DeprecationWarning) for wi in w)


class TestFetchMtlxOriginal:
    """Retained for backward compat — method still works, just warns."""

    @patch("mat_vis_client.client._get_json")
    def test_returns_xml_for_known_material(self, mock_json, mock_client):
        import warnings

        mock_json.return_value = {
            "test-uuid": '<?xml version="1.0"?><materialx version="1.38"><nodegraph/></materialx>'
        }
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            xml = mock_client.fetch_mtlx_original("gpuopen", "test-uuid")
        assert xml is not None
        assert "<materialx" in xml

    @patch("mat_vis_client.client._get_json")
    def test_returns_none_for_unknown(self, mock_json, mock_client):
        import warnings

        mock_json.return_value = {"other-uuid": "<materialx/>"}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            xml = mock_client.fetch_mtlx_original("gpuopen", "nonexistent")
        assert xml is None

    @patch("mat_vis_client.client._get_json", side_effect=Exception("404"))
    def test_returns_none_when_no_mtlx_asset(self, mock_json, mock_client):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            xml = mock_client.fetch_mtlx_original("ambientcg", "Rock064")
        assert xml is None


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
        assert m["schema_version"] == 1
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
