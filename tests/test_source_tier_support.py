"""Source-level tier-support contract (#179).

Each upstream source publishes different native resolution tiers.
The baker must declare those upfront so ``bake_one`` can:

- Refuse an unsupported (source, tier) combo with a clear message
  pointing operators at ``hf-derive`` for the smaller tiers.
- Let ``scripts/full-bake.sh`` enumerate (source, tier) pairs that
  will actually succeed, rather than issuing 454 per-material
  failures when no upstream package matches.

Observed on 2026-04-21 against the live APIs:

- ambientcg: 128/256/512/1k/2k/4k/8k (all PNG tiers)
- polyhaven: 128/256/512/1k/2k/4k/8k (all PNG tiers)
- gpuopen:   1k/2k/4k/8k only — no packages below 1k
- physicallybased: "scalar" only (no textures at all)

Regressions on this set would surface as sudden rc=1 floods during a
staging bake. The full-bake loop relies on this to stay green.
"""

from __future__ import annotations

import pytest

# Will exist after the fix. Marked as RED by default so the pytest
# failure on first run pins the gap in the implementation.
supported_tiers_module_available = False
try:
    from mat_vis_baker.source_tiers import SUPPORTED_TIERS  # type: ignore[attr-defined]

    supported_tiers_module_available = True
except ImportError:  # pragma: no cover — RED-phase guard
    SUPPORTED_TIERS = None


@pytest.mark.skipif(
    not supported_tiers_module_available,
    reason="RED phase — mat_vis_baker.source_tiers not yet implemented",
)
class TestSupportedTiers:
    def test_ambientcg_serves_full_png_range(self):
        assert SUPPORTED_TIERS["ambientcg"] == {
            "128",
            "256",
            "512",
            "1k",
            "2k",
            "4k",
            "8k",
        }

    def test_polyhaven_serves_full_png_range(self):
        assert SUPPORTED_TIERS["polyhaven"] == {
            "128",
            "256",
            "512",
            "1k",
            "2k",
            "4k",
            "8k",
        }

    def test_gpuopen_serves_only_1k_and_up(self):
        """gpuopen's /packages/ endpoint only exposes 1k+ labels. Bakes
        at 128/256/512 would produce 0 matching packages per material
        (every material fails) — pointless compute."""
        assert SUPPORTED_TIERS["gpuopen"] == {"1k", "2k", "4k", "8k"}

    def test_physicallybased_is_scalar_only(self):
        assert SUPPORTED_TIERS["physicallybased"] == {"scalar"}

    def test_every_canonical_source_has_an_entry(self):
        """No source should be missing from the map — keeps the
        ``full-bake.sh`` loop tight."""
        from mat_vis_baker.common import CANONICAL_SOURCES

        missing = set(CANONICAL_SOURCES) - set(SUPPORTED_TIERS.keys())
        assert not missing, f"sources missing from SUPPORTED_TIERS: {missing}"


class TestBakeRefusesUnsupportedTier:
    """``bake_one(source, tier, ...)`` must refuse early when the
    (source, tier) combo is known-unsupported by the upstream. Raises
    with a message that names the supported set + points at hf-derive
    for the smaller tiers."""

    def test_gpuopen_512_raises_with_helpful_message(self, tmp_path):
        """The staging-bake failure mode on 2026-04-21 was 454
        per-material failures and a cryptic {'error': 'no materials'}.
        Should be a single upfront ValueError that names the fix.
        """
        from mat_vis_baker.hf_bake import bake_one

        with pytest.raises(ValueError) as exc:
            bake_one(
                source="gpuopen",
                tier="512",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="unused",
                dry_run=True,
            )
        msg = str(exc.value)
        assert "gpuopen" in msg, f"error doesn't name source: {msg!r}"
        assert "512" in msg, f"error doesn't name tier: {msg!r}"
        assert "hf-derive" in msg, f"error should point at hf-derive as the fix: {msg!r}"

    def test_gpuopen_1k_still_works(self, tmp_path, monkeypatch):
        """Supported combos proceed past the guard — we only want the
        RED case to fail fast, not the whole fetcher path."""
        from mat_vis_baker.hf_bake import bake_one

        # Short-circuit the actual fetch so we don't hit upstream.
        # Per-file (default since #184) reads the fetcher from
        # hf_bake_per_file._get_fetcher, not hf_bake — patch both so
        # this test is substrate-agnostic.
        monkeypatch.setattr(
            "mat_vis_baker.hf_bake._get_fetcher",
            lambda _source: lambda *a, **kw: [],
        )
        monkeypatch.setattr(
            "mat_vis_baker.hf_bake_per_file._get_fetcher",
            lambda _source: lambda *a, **kw: [],
        )
        # Dry-run + empty fetcher result is fine — we're only checking
        # the supported-tier guard lets the call through. Use the
        # scratch repo so the per-file prod-target guard doesn't fire.
        result = bake_one(
            source="gpuopen",
            tier="1k",
            release_tag="v0.0.0-test",
            work_dir=tmp_path,
            repo_id="gerchowl/mat-vis-tst",
            hf_token="unused",
            dry_run=True,
        )
        # "no materials" is the error from an empty fetcher — NOT the
        # unsupported-tier guard. This confirms we passed the guard.
        assert result.get("error") != "unsupported_tier"

    def test_physicallybased_non_scalar_raises(self, tmp_path):
        """Scalar-only source with a PNG tier requested — same guard
        applies, with the analogous message."""
        from mat_vis_baker.hf_bake import bake_one

        with pytest.raises(ValueError) as exc:
            bake_one(
                source="physicallybased",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="unused",
                dry_run=True,
            )
        msg = str(exc.value)
        assert "physicallybased" in msg
        assert "scalar" in msg
