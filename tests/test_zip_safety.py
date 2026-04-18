"""Regression tests for zip-slip + decompression-bomb defense (#59)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from mat_vis_baker.common import UnsafeZipError, check_zip_safety, safe_zip_extract


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_benign_zip_passes():
    data = _make_zip({"a.png": b"\x89PNG\r\n\x1a\n" + b"x" * 100, "b.png": b"data"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        check_zip_safety(zf, output_dir=Path("/tmp/test-zip"))


def test_zip_slip_absolute_path_rejected():
    data = _make_zip({"../../etc/passwd": b"pwned"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(UnsafeZipError, match="zip-slip"):
            check_zip_safety(zf, output_dir=Path("/tmp/test-zip-slip"))


def test_zip_slip_ignored_when_no_output_dir():
    # If caller derives paths independently (our fetchers do), zip-slip
    # check is optional. Must not error.
    data = _make_zip({"../../etc/passwd": b"pwned"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        check_zip_safety(zf)  # no output_dir, no slip check


def test_decompression_bomb_per_file_rejected():
    # Create a 10 MB file, limit per-file to 5 MB
    big = b"A" * (10 * 1024 * 1024)
    data = _make_zip({"huge.bin": big})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(UnsafeZipError, match="decompression bomb"):
            check_zip_safety(zf, max_per_file_mb=5)


def test_decompression_bomb_total_rejected():
    # Many small files exceeding total limit
    files = {f"f{i}.bin": b"A" * (1024 * 1024) for i in range(10)}  # 10 MB total
    data = _make_zip(files)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(UnsafeZipError, match="total uncompressed"):
            check_zip_safety(zf, max_total_mb=5)


def test_compression_ratio_bomb_rejected():
    # 1 MB of zeros compresses very well — high ratio
    data = _make_zip({"bomb.bin": b"\x00" * (1024 * 1024)})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Default max is 100x; a MB of zeros is well past that
        with pytest.raises(UnsafeZipError, match="compression ratio"):
            check_zip_safety(zf, max_compression_ratio=10.0)


def test_safe_extract_full_flow(tmp_path: Path):
    data = _make_zip({"a.txt": b"hello", "sub/b.txt": b"world"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        safe_zip_extract(zf, tmp_path)
    assert (tmp_path / "a.txt").read_bytes() == b"hello"
    assert (tmp_path / "sub" / "b.txt").read_bytes() == b"world"


def test_safe_extract_blocks_slip(tmp_path: Path):
    data = _make_zip({"../../escape.txt": b"bad"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(UnsafeZipError, match="zip-slip"):
            safe_zip_extract(zf, tmp_path)
    # Confirm nothing escaped
    assert not (tmp_path.parent.parent / "escape.txt").exists()
