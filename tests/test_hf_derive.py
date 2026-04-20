"""Unit tests for hf_derive (ADR-0007, issue #112).

Covers the pure-Python core paths without touching HF.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from mat_vis_baker.hf_derive import _slice_channel, derive_smaller_tier
from mat_vis_baker.tar_writer import TarWriter


def _png_bytes(size: int, color: tuple[int, int, int] = (200, 100, 50)) -> bytes:
    img = Image.new("RGB", (size, size), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
    """HTTP-streaming path: mock _pin_commit + _fetch_rowmap + _range_read
    so the pipeline runs without network. Verifies output tar's sliced
    channels decode at the target size."""
    src_tar = tmp_path / "polyhaven-1k.tar"
    with TarWriter(src_tar) as tw:
        for mid in ("mat_a", "mat_b"):
            tw.add_channel(mid, "color", _png_bytes(1024, (255, 0, 0)))
            tw.add_channel(mid, "normal", _png_bytes(1024, (0, 255, 0)))
        src_materials = tw.finalize()
    src_tar_bytes = src_tar.read_bytes()
    src_rowmap = {
        "version": 1,
        "release_tag": "v0.0.0-test",
        "source": "polyhaven",
        "tier": "1k",
        "tar_file": "polyhaven-1k.tar",
        "materials": src_materials,
    }

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
    _ = fake_catalog  # retained just to document the pre-existing shape

    def fake_range_read(*, session, tar_url, spec, token):
        lo, length = int(spec["offset"]), int(spec["length"])
        return src_tar_bytes[lo : lo + length]

    with (
        patch("mat_vis_baker.hf_derive._pin_commit", return_value="deadbeefcafe"),
        patch("mat_vis_baker.hf_derive._fetch_rowmap", return_value=src_rowmap),
        patch("mat_vis_baker.hf_derive._range_read", side_effect=fake_range_read),
    ):
        result = derive_smaller_tier(
            source="polyhaven",
            target_tier="512",
            source_tier="1k",
            release_tag="v0.0.0-test",
            work_dir=work_dir,
            hf_token="test",
            dry_run=True,
            workers=2,
        )

    assert result["dry_run"] is True
    assert result["ok"] == 4  # 2 mats × 2 channels

    # Output tar's sliced channels decode as 512×512 PNGs.
    out_tar = work_dir / "polyhaven-512.tar"
    out_rowmap = json.loads((work_dir / "polyhaven-512-rowmap.json").read_text())
    assert set(out_rowmap["materials"].keys()) == {"mat_a", "mat_b"}
    for mid in ("mat_a", "mat_b"):
        for ch in ("color", "normal"):
            spec = out_rowmap["materials"][mid][ch]
            png = out_tar.read_bytes()[spec["offset"] : spec["offset"] + spec["length"]]
            assert png[:4] == b"\x89PNG"
            assert Image.open(io.BytesIO(png)).size == (512, 512)

    # Critical: no catalog and no manifest are written — derives no
    # longer touch shared state (ADR-0007 race-free design).
    assert not (work_dir / "polyhaven.json").exists()
    assert not (work_dir / "release-manifest.json").exists()


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
