"""Tests for the two-layer failure gates in `_stream_transform_into_tar`
(ADR-0009, hardened after /falsify review of the one-shot fail-fast).

Covers:

1. **Continuous sliding-window fail-fast** — aborts during the run if
   the last WINDOW completions exceed the failure ratio. Not a one-
   shot at the 50th channel.
2. **Terminal success-rate gate** — a run whose overall success ratio
   is below TERMINAL_MIN_OK_RATIO refuses to finalize, even if fail-
   fast didn't trip.
3. **Happy path** — 100% success still passes; a small tolerable
   fraction of failures (< 10%) also passes.
"""

from __future__ import annotations

import io
import itertools
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from mat_vis_baker.hf_derive import _stream_transform_into_tar


def _png_bytes(size: int = 16, color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    img = Image.new("RGB", (size, size), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _materials(n: int) -> dict:
    """N materials with one channel each, offsets just monotonic ints.
    The range-read path is mocked, so offsets don't have to be real."""
    return {f"m{i:05d}": {"color": {"offset": i, "length": 1}} for i in range(n)}


def _pool_with(transform, materials_count, tmp_path, label="test", workers=4):
    return _stream_transform_into_tar(
        materials=_materials(materials_count),
        tar_url="http://fake/tar",
        token=None,
        transform=transform,
        max_workers=workers,
        out_tar_path=tmp_path / "out.tar",
        label=label,
    )


@pytest.fixture(autouse=True)
def _mock_range_read():
    """Every range-read returns a dummy PNG. Transform decides success/fail."""
    with patch(
        "mat_vis_baker.hf_derive._range_read",
        side_effect=lambda **kw: _png_bytes(),
    ):
        yield


# ── happy paths ──────────────────────────────────────────


def test_all_success_passes(tmp_path: Path):
    materials, n_ok, n_failed = _pool_with(
        transform=lambda raw: raw,
        materials_count=200,
        tmp_path=tmp_path,
    )
    assert n_ok == 200 and n_failed == 0
    assert len(materials) == 200


def test_small_failure_fraction_below_terminal_gate_passes(tmp_path: Path):
    """5% failures — below the 10% terminal gate — is tolerated."""
    counter = itertools.count()

    def flaky(raw: bytes) -> bytes:
        i = next(counter)
        if i % 20 == 0:  # 5% fail rate
            raise RuntimeError("transient")
        return raw

    materials, n_ok, n_failed = _pool_with(transform=flaky, materials_count=200, tmp_path=tmp_path)
    assert n_failed == 10
    assert n_ok == 190


# ── continuous sliding-window fail-fast ─────────────────


def test_mid_run_regression_trips_sliding_window(tmp_path: Path):
    """First 200 channels succeed, then HF rate-limits for the rest —
    the sliding-window gate must abort instead of grinding on."""
    counter = itertools.count()

    def regression(raw: bytes) -> bytes:
        i = next(counter)
        if i < 200:
            return raw
        raise RuntimeError(f"rate-limited at channel {i}")

    with pytest.raises(RuntimeError, match="fail-fast"):
        _pool_with(transform=regression, materials_count=2000, tmp_path=tmp_path)


def test_systemic_failure_trips_early(tmp_path: Path):
    """Every channel fails — aborted once enough samples accumulate."""

    def always_fail(raw: bytes) -> bytes:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="fail-fast"):
        _pool_with(transform=always_fail, materials_count=500, tmp_path=tmp_path)


# ── terminal success-rate gate ──────────────────────────


def test_terminal_gate_rejects_sub_90_percent(tmp_path: Path):
    """A failure distribution that slips *under* the sliding-window
    threshold (so fail-fast never trips) but ends with <90% success
    must still be rejected by the terminal gate."""
    counter = itertools.count()

    # 15% uniform failures spread across a small run. 15% > 10% terminal
    # cap → must reject. 15% < 20% rolling window threshold → rolling
    # window alone wouldn't catch it. Exactly the "silent 1-of-many
    # success" counterexample from /falsify review.
    def uniform_15(raw: bytes) -> bytes:
        i = next(counter)
        if i % 7 == 0:  # ~14% fail
            raise RuntimeError("bad")
        return raw

    with pytest.raises(RuntimeError, match="terminal check|fail-fast"):
        _pool_with(transform=uniform_15, materials_count=200, tmp_path=tmp_path)
