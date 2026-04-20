"""Stable channel-level sharding for bake/derive workers (#134).

GitHub-hosted runners cap at 6 h per job; ambientcg-ktx2-2k needs
~22 h of single-runner work. Splitting the channel list across N
shards fans out the work without raising per-job cost. This module
is the one place the partitioning function lives so every consumer
agrees on which shard owns which channel.

Determinism matters: a failed shard must be re-runnable and produce
bit-identical output, and two different shards must cover disjoint
sets. Python's builtin ``hash()`` is randomised per-process, so we
use an explicit ``md5`` prefix instead.

Usage:

    if channel_in_shard(mid, ch, shard_index, shard_total):
        process(mid, ch)
"""

from __future__ import annotations

import hashlib


def channel_shard(material_id: str, channel: str, shard_total: int) -> int:
    """Return the shard index (0-based) that owns ``(material_id, channel)``."""
    if shard_total < 1:
        raise ValueError(f"shard_total must be >= 1, got {shard_total}")
    key = f"{material_id}/{channel}".encode()
    digest = hashlib.md5(key, usedforsecurity=False).digest()
    bucket = int.from_bytes(digest[:4], "big")
    return bucket % shard_total


def channel_in_shard(material_id: str, channel: str, shard_index: int, shard_total: int) -> bool:
    """True iff ``(material_id, channel)`` belongs to shard ``shard_index``."""
    if not 0 <= shard_index < shard_total:
        raise ValueError(f"shard_index {shard_index} out of range for shard_total {shard_total}")
    return channel_shard(material_id, channel, shard_total) == shard_index


def validate_shard_args(shard_index: int | None, shard_total: int | None) -> tuple[int, int] | None:
    """Normalise CLI shard args. Returns ``None`` when unsharded, else
    ``(index, total)``. Rejects partial specs (only one flag provided)."""
    if shard_index is None and shard_total is None:
        return None
    if shard_index is None or shard_total is None:
        raise ValueError("--shard-index and --shard-total must both be provided or both omitted")
    if shard_total < 1:
        raise ValueError(f"--shard-total must be >= 1, got {shard_total}")
    if not 0 <= shard_index < shard_total:
        raise ValueError(f"--shard-index {shard_index} out of range [0, {shard_total})")
    return shard_index, shard_total


def shard_suffix(shard_index: int, shard_total: int) -> str:
    """Filename suffix for shard artifacts: ``.shard-N-of-K``."""
    return f".shard-{shard_index}-of-{shard_total}"
