"""Tests for the polyhaven MTLX download path (#96).

Polyhaven publishes per-tier MaterialX documents alongside textures, but
the fetcher historically skipped them — so v2026.04.0 shipped without a
``polyhaven-mtlx.json`` and every ``client.mtlx("polyhaven", ...).original``
call returned ``None``. The new ``_download_mtlx`` helper threads through
``fetch(..., mtlx_dir=...)`` and writes ``mtlx_dir/polyhaven/{slug}.mtlx``,
which ``pack-mtlx`` then bundles into the JSON map.

These tests cover:
- happy path: tier-matching MTLX URL → file written to expected location
- absent MTLX section → returns None, no file written, no exception
- bad tier_key → returns None
- network failure → returns None, no exception (best-effort, callers want
  the texture maps to land regardless)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from mat_vis_baker.sources.polyhaven import _download_mtlx


def _file_info_with_mtlx(tier_key: str, url: str = "https://example.com/foo.mtlx") -> dict:
    return {
        "Diffuse": {tier_key: {"png": {"url": "ignored"}}},
        "mtlx": {
            tier_key: {
                "mtlx": {
                    "url": url,
                    "md5": "deadbeef",
                    "size": 1234,
                    "include": {},
                }
            }
        },
    }


def test_download_mtlx_happy_path(tmp_path: Path) -> None:
    file_info = _file_info_with_mtlx("1k")
    mock_resp = MagicMock(content=b"<materialx>...</materialx>")

    with patch("mat_vis_baker.sources.polyhaven.retry_request", return_value=mock_resp):
        out = _download_mtlx(file_info, "1k", tmp_path, "wood_floor")

    assert out is not None
    assert out == tmp_path / "polyhaven" / "wood_floor.mtlx"
    assert out.read_bytes() == b"<materialx>...</materialx>"


def test_download_mtlx_no_mtlx_section_returns_none(tmp_path: Path) -> None:
    """Most polyhaven materials have MTLX, but a few don't. The helper
    must return None silently — synthesized MTLX still works as fallback."""
    file_info = {"Diffuse": {"1k": {"png": {"url": "x"}}}}
    out = _download_mtlx(file_info, "1k", tmp_path, "no_mtlx_mat")
    assert out is None
    assert not (tmp_path / "polyhaven" / "no_mtlx_mat.mtlx").exists()


def test_download_mtlx_unknown_tier_returns_none(tmp_path: Path) -> None:
    """Tiers not in _TIER_KEYS (e.g. '128') don't map to a polyhaven tier
    string. Helper bails cleanly."""
    file_info = _file_info_with_mtlx("1k")
    out = _download_mtlx(file_info, "128", tmp_path, "wood_floor")
    assert out is None


def test_download_mtlx_missing_url_returns_none(tmp_path: Path) -> None:
    """Defensive: handle malformed responses where the mtlx block exists
    but has no url key (shouldn't happen in practice)."""
    file_info = {
        "mtlx": {
            "1k": {"mtlx": {"md5": "abc", "size": 0}}  # no url
        },
    }
    out = _download_mtlx(file_info, "1k", tmp_path, "wood_floor")
    assert out is None


def test_download_mtlx_network_failure_returns_none(tmp_path: Path) -> None:
    """Best-effort: if upstream MTLX fetch fails, log + return None.
    Caller still gets MaterialRecord with textures; .original falls back
    to None at client-side."""
    file_info = _file_info_with_mtlx("1k")
    with patch(
        "mat_vis_baker.sources.polyhaven.retry_request",
        side_effect=Exception("network down"),
    ):
        out = _download_mtlx(file_info, "1k", tmp_path, "wood_floor")
    assert out is None
    assert not (tmp_path / "polyhaven" / "wood_floor.mtlx").exists()


def test_download_mtlx_writes_to_pack_compatible_layout(tmp_path: Path) -> None:
    """``pack-mtlx`` (mtlx_tier.pack_original_mtlx_json) reads
    ``mtlx_dir/{source}/**/*.mtlx`` with two filename conventions:

      MaterialName/material.mtlx  → material_id = parent dir
      MaterialName.mtlx           → material_id = stem

    Polyhaven uses the second form (``polyhaven/{slug}.mtlx``), so the
    bundled JSON keys end up as the slug — matching what
    ``client.mtlx("polyhaven", slug, tier).original`` will look up.
    """
    file_info = _file_info_with_mtlx("2k")
    with patch(
        "mat_vis_baker.sources.polyhaven.retry_request",
        return_value=MagicMock(content=b"<mtlx/>"),
    ):
        out = _download_mtlx(file_info, "2k", tmp_path, "rocky_terrain_02")

    assert out is not None
    # Roundtrip: pack-mtlx should derive the slug from the stem.
    from mat_vis_baker.mtlx_tier import pack_original_mtlx_json

    json_path = pack_original_mtlx_json(mtlx_dir=tmp_path, source="polyhaven", output_dir=tmp_path)
    import json

    bundled = json.loads(json_path.read_text())
    assert "rocky_terrain_02" in bundled
    assert bundled["rocky_terrain_02"] == "<mtlx/>"
