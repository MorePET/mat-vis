"""Tests for the atomic-upload helpers (#60).

All network calls are mocked via fake ``_run`` callables that mimic
``subprocess.run``'s return value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mat_vis_baker import upload


class _Result:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(responses):
    """Build a fake ``subprocess.run`` that returns a sequence of ``_Result``s.

    Each call pops the next response; raises if exhausted.
    """
    seq = list(responses)
    calls = []

    def _run(cmd, **_kw):
        calls.append(cmd)
        if not seq:
            raise AssertionError(f"unexpected extra subprocess.run call: {cmd}")
        return seq.pop(0)

    _run.calls = calls
    return _run


def _silent_sleep(_x):
    pass


class TestGhUpload:
    def test_success_first_try(self, tmp_path: Path) -> None:
        f = tmp_path / "x.parquet"
        f.write_bytes(b"hello")
        run = _fake_runner([_Result(0)])
        upload.gh_upload(f, "v1.0.0", _run=run, _sleep=_silent_sleep)
        assert len(run.calls) == 1
        cmd = run.calls[0]
        assert cmd[0:3] == ["gh", "release", "upload"]
        assert cmd[3] == "v1.0.0"

    def test_retries_on_rate_limit_then_succeeds(self, tmp_path: Path) -> None:
        f = tmp_path / "x.parquet"
        f.write_bytes(b"x")
        run = _fake_runner(
            [
                _Result(1, stderr="HTTP 429: rate limit exceeded"),
                _Result(0),
            ]
        )
        upload.gh_upload(f, "v1.0.0", _run=run, _sleep=_silent_sleep)
        assert len(run.calls) == 2

    def test_retries_on_503(self, tmp_path: Path) -> None:
        f = tmp_path / "x.parquet"
        f.write_bytes(b"x")
        run = _fake_runner(
            [
                _Result(1, stderr="upstream error: 503 Service Unavailable"),
                _Result(1, stderr="timeout after 60s"),
                _Result(0),
            ]
        )
        upload.gh_upload(f, "v1.0.0", _run=run, _sleep=_silent_sleep)
        assert len(run.calls) == 3

    def test_non_transient_fails_fast(self, tmp_path: Path) -> None:
        f = tmp_path / "x.parquet"
        f.write_bytes(b"x")
        run = _fake_runner(
            [
                _Result(1, stderr="HTTP 401: Bad credentials"),
            ]
        )
        with pytest.raises(upload.UploadError, match="gh upload failed"):
            upload.gh_upload(f, "v1.0.0", _run=run, _sleep=_silent_sleep)
        # Should not have retried
        assert len(run.calls) == 1

    def test_exhausts_retries_on_persistent_transient(self, tmp_path: Path) -> None:
        f = tmp_path / "x.parquet"
        f.write_bytes(b"x")
        run = _fake_runner([_Result(1, stderr="rate limit")] * 5)
        with pytest.raises(upload.UploadError, match="exhausted"):
            upload.gh_upload(f, "v1.0.0", max_retries=5, _run=run, _sleep=_silent_sleep)
        assert len(run.calls) == 5

    def test_missing_source_raises(self, tmp_path: Path) -> None:
        with pytest.raises(upload.UploadError, match="does not exist"):
            upload.gh_upload(tmp_path / "nope.parquet", "v1.0.0")


class TestVerifyUploadSize:
    def test_matching_size_returns_true(self) -> None:
        run = _fake_runner([_Result(0, stdout="1234\n")])
        assert upload.verify_upload_size("v1.0.0", "x.parquet", 1234, _run=run)

    def test_mismatched_size_returns_false(self) -> None:
        run = _fake_runner([_Result(0, stdout="9999\n")])
        assert not upload.verify_upload_size("v1.0.0", "x.parquet", 1234, _run=run)

    def test_asset_not_found_returns_false(self) -> None:
        run = _fake_runner([_Result(0, stdout="\n")])
        assert not upload.verify_upload_size("v1.0.0", "missing.parquet", 10, _run=run)


class TestUploadWithVerify:
    def test_success_on_first_round(self, tmp_path: Path) -> None:
        f = tmp_path / "x.parquet"
        f.write_bytes(b"abcdef")
        run = _fake_runner(
            [
                _Result(0),  # gh_upload
                _Result(0, stdout="6\n"),  # verify_upload_size
            ]
        )
        upload.upload_with_verify(f, "v1.0.0", _run=run, _sleep=_silent_sleep)
        assert len(run.calls) == 2

    def test_retries_on_size_mismatch(self, tmp_path: Path) -> None:
        f = tmp_path / "x.parquet"
        f.write_bytes(b"abcdef")  # 6 bytes
        run = _fake_runner(
            [
                _Result(0),  # upload #1
                _Result(0, stdout="3\n"),  # verify says wrong size
                _Result(0),  # upload #2
                _Result(0, stdout="6\n"),  # verify OK
            ]
        )
        upload.upload_with_verify(f, "v1.0.0", _run=run, _sleep=_silent_sleep)
        assert len(run.calls) == 4


class TestAtomicWritePath:
    def test_happy_path_replaces_into_place(self, tmp_path: Path) -> None:
        target = tmp_path / "data.bin"
        with upload.atomic_write_path(target) as part:
            assert part.name == "data.bin.part"
            part.write_bytes(b"hello")
        assert target.exists()
        assert target.read_bytes() == b"hello"
        assert not part.exists()

    def test_exception_removes_partial_file(self, tmp_path: Path) -> None:
        target = tmp_path / "data.bin"
        with pytest.raises(RuntimeError, match="boom"):
            with upload.atomic_write_path(target) as part:
                part.write_bytes(b"partial")
                raise RuntimeError("boom")
        assert not target.exists()
        assert not (tmp_path / "data.bin.part").exists()


class TestProgressMarker:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        upload.save_progress(
            tmp_path,
            source="ambientcg",
            tier="2k",
            offset_done=250,
            chunk_nums={"metal": 2, "stone": 1},
            completed_categories=["wood"],
            release_tag="v2026.04.0",
        )
        marker = upload.progress_path(tmp_path)
        assert marker.exists()

        loaded = upload.load_progress(tmp_path)
        assert loaded is not None
        assert loaded["source"] == "ambientcg"
        assert loaded["tier"] == "2k"
        assert loaded["offset_done"] == 250
        assert loaded["chunk_nums"] == {"metal": 2, "stone": 1}
        assert loaded["completed_categories"] == ["wood"]

    def test_missing_marker_returns_none(self, tmp_path: Path) -> None:
        assert upload.load_progress(tmp_path) is None

    def test_corrupt_marker_returns_none(self, tmp_path: Path) -> None:
        upload.progress_path(tmp_path).write_text("not json{{{")
        assert upload.load_progress(tmp_path) is None

    def test_clear(self, tmp_path: Path) -> None:
        upload.progress_path(tmp_path).write_text(json.dumps({"a": 1}))
        upload.clear_progress(tmp_path)
        assert not upload.progress_path(tmp_path).exists()
        # Safe to call when missing
        upload.clear_progress(tmp_path)
