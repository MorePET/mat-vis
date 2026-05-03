"""Per-source supported-tier manifest (#179).

Upstream sources don't all serve the same resolution tiers. ambientcg
and polyhaven publish every PNG tier from 128 up to 8k; gpuopen only
exposes 1k and larger; physicallybased has no textures at all (scalar
PBR only). The baker used to happily call ``bake_one(source, tier)``
for any combo, then discover the mismatch 454-materials later when
every fetch returned no matching package — slow, noisy, and leaves
an ugly ``{'error': 'no materials'}`` artifact in the log.

This module is the single source of truth for which (source, tier)
combos have native upstream data. ``bake_one`` consults it upfront
(see ``hf_bake._guard_supported_tier``) and refuses unsupported
combos with a clear message naming the supported set. The legacy
``hf-derive`` resize path was retired with the tar substrate in
#189; per-file derive is future work, so for now operators must
bake from a tier the upstream natively serves.

Captured on 2026-04-21 by enumerating live gpuopen package labels:

    454 × "1k 8b", 435 × "2k 8b", 428 × "4k 8b", 169 × "8k 8b"

Re-check by running ``scripts/probe-metadata-vocab.py`` (which uses
the same ``discover()`` fetchers). If upstreams add new tiers, the
probe picks them up and we update this file in a single commit so
the drift stays visible in git history.
"""

from __future__ import annotations

# Native bake-time tiers per upstream source. Keys are canonical
# source names (mat_vis_baker.common.CANONICAL_SOURCES). Values are
# the set of tier labels the baker can pass straight to the
# fetcher without pre-processing.
#
# Derive-time tiers (resize / KTX2 transcode of an already-baked tier)
# are NOT listed here — they come into existence on HF without
# involving this guard. The tar-era derive pipeline was retired in
# #189; per-file derive is future work.
SUPPORTED_TIERS: dict[str, frozenset[str]] = {
    "ambientcg": frozenset({"128", "256", "512", "1k", "2k", "4k", "8k"}),
    "polyhaven": frozenset({"128", "256", "512", "1k", "2k", "4k", "8k"}),
    "gpuopen": frozenset({"1k", "2k", "4k", "8k"}),
    "physicallybased": frozenset({"scalar"}),
}


def is_supported(source: str, tier: str) -> bool:
    """Return True iff the baker can call ``bake_one(source, tier)``
    without producing a 454-failures artifact."""
    return tier in SUPPORTED_TIERS.get(source, frozenset())


def unsupported_tier_message(source: str, tier: str) -> str:
    """Build the error message for an unsupported (source, tier) combo.

    Named so the test asserting the message content can assert on the
    exact string rather than re-duplicating the format. Mentions the
    source, the tier, and the supported set."""
    supported = SUPPORTED_TIERS.get(source, frozenset())
    supported_str = ", ".join(sorted(supported)) if supported else "(none)"
    guidance = (
        "bake one of the natively-supported tiers above"
        if source != "physicallybased"
        else "physicallybased has no textures; bake tier='scalar' instead"
    )
    return (
        f"Source {source!r} does not natively serve tier {tier!r}. "
        f"Supported tiers for {source}: {{{supported_str}}}. "
        f"To produce tier {tier!r} for {source}, {guidance}."
    )
