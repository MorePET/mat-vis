"""Unit tests for hf_derive (ADR-0007, issue #112).

Covers the pure-Python core paths without touching HF:

- `_patch_catalog_tiers`: tier-union semantics on `available_tiers`.
- `_slice_channel`: offset/length → byte slice.
- End-to-end resize path: TarWriter → slice → PIL resize → TarWriter.
  Builds a tiny fake PNG tar in a tmp dir, runs `derive_smaller_tier`
  with `dry_run=True` against a local-file-backed HF cache mock, and
  verifies the output tar contains the expected material×channel
  count and that a sliced PNG decodes at the target size.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from mat_vis_baker.hf_derive import (
    _patch_catalog_tiers,
    _slice_channel,
    derive_smaller_tier,
)
from mat_vis_baker.tar_writer import TarWriter


def _png_bytes(size: int, color: tuple[int, int, int] = (200, 100, 50)) -> bytes:
    img = Image.new("RGB", (size, size), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_patch_catalog_tiers_adds_and_sorts():
    cat = [
        {"id": "A", "available_tiers": ["1k"]},
        {"id": "B", "available_tiers": ["1k", "2k"]},
    ]
    out = _patch_catalog_tiers(cat, {"A", "B"}, "512")
    assert out[0]["available_tiers"] == ["1k", "512"]
    assert out[1]["available_tiers"] == ["1k", "2k", "512"]


def test_patch_catalog_tiers_skips_materials_not_in_set():
    cat = [
        {"id": "A", "available_tiers": ["1k"]},
        {"id": "B", "available_tiers": ["1k"]},
    ]
    out = _patch_catalog_tiers(cat, {"A"}, "512")
    assert out[0]["available_tiers"] == ["1k", "512"]
    assert out[1]["available_tiers"] == ["1k"]


def test_patch_catalog_tiers_does_not_mutate_input():
    cat = [{"id": "A", "available_tiers": ["1k"]}]
    _patch_catalog_tiers(cat, {"A"}, "512")
    assert cat[0]["available_tiers"] == ["1k"]


def test_slice_channel_roundtrips_exact_bytes(tmp_path: Path):
    # Write a tar with one PNG, then slice by rowmap offset/length and
    # assert we get the original bytes back.
    tar_path = tmp_path / "t.tar"
    payload = _png_bytes(64)
    with TarWriter(tar_path) as tw:
        tw.add_channel("M", "color", payload)
        materials = tw.finalize()
    spec = materials["M"]["color"]
    tar_bytes = tar_path.read_bytes()
    sliced = _slice_channel(tar_bytes, spec)
    assert sliced == payload
    assert Image.open(io.BytesIO(sliced)).size == (64, 64)


def test_derive_smaller_tier_end_to_end_dry_run(tmp_path: Path):
    """Fake an existing HF revision on local disk, run derive, verify output tar."""
    # 1. Build a fake "1k" tar + rowmap on disk that the mock will hand to derive.
    src_tar = tmp_path / "polyhaven-1k.tar"
    src_rowmap_path = tmp_path / "polyhaven-1k-rowmap.json"
    with TarWriter(src_tar) as tw:
        for mid in ("mat_a", "mat_b"):
            tw.add_channel(mid, "color", _png_bytes(1024, (255, 0, 0)))
            tw.add_channel(mid, "normal", _png_bytes(1024, (0, 255, 0)))
        new_materials = tw.finalize()
    src_rowmap = {
        "version": 1,
        "release_tag": "v0.0.0-test",
        "source": "polyhaven",
        "tier": "1k",
        "tar_file": "polyhaven-1k.tar",
        "materials": new_materials,
    }
    src_rowmap_path.write_text(json.dumps(src_rowmap))

    fake_catalog = [
        {
            "id": "mat_a",
            "source": "polyhaven",
            "available_tiers": ["1k"],
            "maps": ["color", "normal"],
        },
        {
            "id": "mat_b",
            "source": "polyhaven",
            "available_tiers": ["1k"],
            "maps": ["color", "normal"],
        },
    ]

    work_dir = tmp_path / "work"

    def fake_hf_hub_download(repo_id, repo_type, revision, filename, token):
        if filename.endswith(".tar"):
            return str(src_tar)
        if filename.endswith("rowmap.json"):
            return str(src_rowmap_path)
        raise FileNotFoundError(filename)

    def fake_download_json(*, repo_id, revision, path, hf_token):
        if path == "polyhaven.json":
            return fake_catalog
        return None

    with (
        patch("mat_vis_baker.hf_derive.hf_hub_download", side_effect=fake_hf_hub_download),
        patch("mat_vis_baker.manifest._download_json", side_effect=fake_download_json),
    ):
        result = derive_smaller_tier(
            source="polyhaven",
            target_tier="512",
            source_tier="1k",
            release_tag="v0.0.0-test",
            work_dir=work_dir,
            hf_token="test",
            dry_run=True,
        )

    assert result["dry_run"] is True
    assert result["ok"] == 4  # 2 mats × 2 channels

    # Check the produced tar has the right materials, and one sliced channel
    # decodes as a 512×512 PNG.
    out_tar = work_dir / "polyhaven-512.tar"
    out_rowmap = json.loads((work_dir / "polyhaven-512-rowmap.json").read_text())
    assert set(out_rowmap["materials"].keys()) == {"mat_a", "mat_b"}
    for mid in ("mat_a", "mat_b"):
        for ch in ("color", "normal"):
            spec = out_rowmap["materials"][mid][ch]
            png = out_tar.read_bytes()[spec["offset"] : spec["offset"] + spec["length"]]
            assert png[:4] == b"\x89PNG"
            assert Image.open(io.BytesIO(png)).size == (512, 512)

    # Catalog + manifest got the new tier stamped.
    cat = json.loads((work_dir / "polyhaven.json").read_text())
    for entry in cat:
        assert "512" in entry["available_tiers"]
    manifest = json.loads((work_dir / "release-manifest.json").read_text())
    assert "512" in manifest["sources"]["polyhaven"]["tiers"]


def test_derive_refuses_upscale(tmp_path: Path):
    """Deriving 2k from 1k would require upscaling — must raise."""
    with pytest.raises(ValueError, match="upscaling not supported"):
        derive_smaller_tier(
            source="polyhaven",
            target_tier="2k",
            source_tier="1k",
            release_tag="v0.0.0-test",
            work_dir=tmp_path,
            hf_token="test",
            dry_run=True,
        )
