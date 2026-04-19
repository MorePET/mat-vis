"""Tests for the tar_writer primitive (ADR-0007 Phase 1)."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from mat_vis_baker.tar_writer import TarWriter

_PNG_HEAD = b"\x89PNG\r\n\x1a\n"


def _fake_png(seed: int, size: int = 64) -> bytes:
    body = bytes((seed + i) % 256 for i in range(size - len(_PNG_HEAD)))
    return _PNG_HEAD + body


def test_roundtrip_three_materials_two_channels(tmp_path: Path) -> None:
    """Offset/length from the sidecar must slice back the exact bytes."""
    out = tmp_path / "ambientcg-1k.tar"
    payload: dict[str, dict[str, bytes]] = {}
    with TarWriter(out) as w:
        for i, mid in enumerate(["Bricks080", "Wood033", "Metal001"]):
            payload[mid] = {
                "color": _fake_png(seed=i, size=128),
                "normal": _fake_png(seed=i + 100, size=96),
            }
            for ch, data in payload[mid].items():
                w.add_channel(mid, ch, data)
        rowmap = w.finalize()

    raw = out.read_bytes()
    for mid, channels in payload.items():
        for ch, expected in channels.items():
            off = rowmap[mid][ch]["offset"]
            length = rowmap[mid][ch]["length"]
            assert length == len(expected)
            assert raw[off : off + length] == expected


def test_empty_tar_is_valid(tmp_path: Path) -> None:
    out = tmp_path / "empty.tar"
    with TarWriter(out) as w:
        rowmap = w.finalize()

    assert rowmap == {}
    with tarfile.open(out, "r") as tf:
        assert tf.getmembers() == []


def test_filename_layout_inside_tar(tmp_path: Path) -> None:
    out = tmp_path / "layout.tar"
    with TarWriter(out) as w:
        w.add_channel("Bricks080", "color", _fake_png(1))
        w.add_channel("Bricks080", "normal", _fake_png(2))
        w.finalize()

    with tarfile.open(out, "r") as tf:
        names = sorted(m.name for m in tf.getmembers())
    assert names == ["Bricks080/color.png", "Bricks080/normal.png"]


def test_multi_channel_material(tmp_path: Path) -> None:
    """All 5 channels of one material must each slice back correctly."""
    out = tmp_path / "multi.tar"
    blobs = {
        ch: _fake_png(i, size=128 + i)
        for i, ch in enumerate(["color", "normal", "roughness", "metalness", "ao"])
    }
    with TarWriter(out) as w:
        for ch, data in blobs.items():
            w.add_channel("MatX", ch, data)
        rowmap = w.finalize()

    raw = out.read_bytes()
    for ch, expected in blobs.items():
        off = rowmap["MatX"][ch]["offset"]
        length = rowmap["MatX"][ch]["length"]
        assert raw[off : off + length] == expected


def test_large_blob_roundtrips(tmp_path: Path) -> None:
    """5 MB blob — offset arithmetic must not be capped at small sizes."""
    out = tmp_path / "large.tar"
    big = _PNG_HEAD + b"\x00" * (5 * 1024 * 1024 - len(_PNG_HEAD))
    with TarWriter(out) as w:
        w.add_channel("Huge", "color", big)
        rowmap = w.finalize()

    raw = out.read_bytes()
    off = rowmap["Huge"]["color"]["offset"]
    length = rowmap["Huge"]["color"]["length"]
    assert length == len(big)
    assert raw[off : off + length] == big


def test_duplicate_add_raises(tmp_path: Path) -> None:
    out = tmp_path / "dup.tar"
    with TarWriter(out) as w:
        w.add_channel("M1", "color", _fake_png(1))
        with pytest.raises(ValueError, match="duplicate"):
            w.add_channel("M1", "color", _fake_png(2))
        w.finalize()


def test_ktx2_extension_detected(tmp_path: Path) -> None:
    """KTX2 payloads get .ktx2 extension, PNG gets .png."""
    out = tmp_path / "mixed.tar"
    ktx2 = b"\xabKTX 20\xbb\r\n\x1a\n" + b"\x00" * 32
    with TarWriter(out) as w:
        w.add_channel("M1", "color", _fake_png(1))
        w.add_channel("M2", "color", ktx2)
        w.finalize()

    with tarfile.open(out, "r") as tf:
        names = sorted(m.name for m in tf.getmembers())
    assert names == ["M1/color.png", "M2/color.ktx2"]


def test_add_after_finalize_raises(tmp_path: Path) -> None:
    out = tmp_path / "closed.tar"
    w = TarWriter(out)
    w.add_channel("M1", "color", _fake_png(1))
    w.finalize()
    with pytest.raises(RuntimeError, match="finalized"):
        w.add_channel("M2", "color", _fake_png(2))
