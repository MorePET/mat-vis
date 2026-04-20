"""Shard-hash invariants: determinism, coverage, bounds (#134)."""

from __future__ import annotations

import pytest

from mat_vis_baker.shard_utils import (
    channel_in_shard,
    channel_shard,
    shard_suffix,
    validate_shard_args,
)


def _sample_channels() -> list[tuple[str, str]]:
    # Representative cross-source sample: long ids, short ids, unicode-free
    # channel names matching what lives in actual rowmaps.
    return [
        (f"{src}_{i:04d}", ch)
        for src in ("ambientcg", "polyhaven", "gpuopen")
        for i in range(200)
        for ch in ("basecolor", "normal", "roughness", "metallic", "ao", "height")
    ]


def test_shard_index_in_range() -> None:
    for mid, ch in _sample_channels()[:50]:
        for k in (1, 2, 4, 8, 16):
            idx = channel_shard(mid, ch, k)
            assert 0 <= idx < k


def test_determinism_across_calls() -> None:
    samples = _sample_channels()
    first = [channel_shard(m, c, 8) for m, c in samples]
    second = [channel_shard(m, c, 8) for m, c in samples]
    assert first == second


def test_coverage_union_equals_full_set() -> None:
    samples = _sample_channels()
    for k in (1, 2, 4, 8):
        owned: list[set[tuple[str, str]]] = [set() for _ in range(k)]
        for mid, ch in samples:
            for i in range(k):
                if channel_in_shard(mid, ch, i, k):
                    owned[i].add((mid, ch))
        union = set().union(*owned)
        assert union == set(samples)
        # Disjoint: each channel owned by exactly one shard.
        for i in range(k):
            for j in range(i + 1, k):
                assert not owned[i] & owned[j]


def test_distribution_roughly_uniform() -> None:
    samples = _sample_channels()
    k = 8
    counts = [0] * k
    for mid, ch in samples:
        counts[channel_shard(mid, ch, k)] += 1
    mean = len(samples) / k
    # md5 on 3600 inputs splits into 8 buckets within ±10% of mean in practice.
    for c in counts:
        assert abs(c - mean) / mean < 0.15, f"counts too skewed: {counts}"


def test_validate_shard_args_unsharded() -> None:
    assert validate_shard_args(None, None) is None


def test_validate_shard_args_valid() -> None:
    assert validate_shard_args(0, 1) == (0, 1)
    assert validate_shard_args(3, 8) == (3, 8)


def test_validate_shard_args_partial_rejected() -> None:
    with pytest.raises(ValueError, match="both"):
        validate_shard_args(0, None)
    with pytest.raises(ValueError, match="both"):
        validate_shard_args(None, 4)


def test_validate_shard_args_bad_total() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        validate_shard_args(0, 0)


def test_validate_shard_args_index_out_of_range() -> None:
    with pytest.raises(ValueError, match="out of range"):
        validate_shard_args(4, 4)
    with pytest.raises(ValueError, match="out of range"):
        validate_shard_args(-1, 4)


def test_shard_suffix_formatting() -> None:
    assert shard_suffix(0, 4) == ".shard-0-of-4"
    assert shard_suffix(7, 8) == ".shard-7-of-8"


def test_channel_shard_rejects_bad_total() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        channel_shard("m", "c", 0)
