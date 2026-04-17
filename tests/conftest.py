"""Shared fixtures. See docs/specs/ for schema definitions."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from mat_vis_baker.common import MaterialRecord


@pytest.fixture
def tiny_png_bytes() -> bytes:
    """A minimal valid 4x4 red PNG."""
    img = Image.new("RGB", (4, 4), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def sample_record(tmp_path: Path, tiny_png_bytes: bytes) -> MaterialRecord:
    """A single MaterialRecord with tiny ONGs on disk."""
    mat_dir = tmp_path / "TestMat001"
    mat_dir.mkdir()
    paths = {}
    for ch in ["color", "normal", "roughness"]:
        p = mat_dir / f"{ch}.png"
        p.write_bytes(tiny_png_bytes)
        paths[ch] = p

    return MaterialRecord(
        id="TestMat001",
        source="ambientcg",
        name="Test Material",
        category="metal",
        tags=["test"],
        source_url="https://example.com/a/TestMat001",
        source_license="CC0-1.0",
        last_updated="2026-04-16",
        available_tiers=["1k"],
        maps=sorted(paths.keys()),
        texture_paths=paths,
    )


@pytest.fixture
def sample_records(tmp_path: Path, tiny_png_bytes: bytes) -> list[MaterialRecord]:
    """Three MaterialRecords with tiny ONGs."""
    records = []
    for i in range(3):
        mid = f"TestMat{i:03d}"
        mat_dir = tmp_path / mid
        mat_dir.mkdir()
        paths = {}
        for ch in ["color", "normal", "roughness"]:
            p = mat_dir / f"{ch}.png"
            p.write_bytes(tiny_png_bytes)
            paths[ch] = p

        records.append(
            MaterialRecord(
                id=mid,
                source="ambientcg",
                name=f"Test Material {i}",
                category="metal",
                tags=["test"],
                source_url=f"https://example.com/a/{mid}",
                source_license="CC0-1.0",
                last_updated="2026-04-16",
                available_tiers=["1k"],
                maps=sorted(paths.keys()),
                texture_paths=paths,
            )
        )
    return records
