"""mat-vis#374 — ``tier="auto"`` + ``tier="best"`` resolution.

Covers:

* ``MatVisClient._resolve_tier`` collapse — table-driven over the full
  forward-compat surface (auto/best, all ladder positions, scalar-only
  short-circuit, unknown-tier filtering, no-textures-staged raise).
* ``MatVisClient.fetch_all_textures`` default flip + auto/best routing.
* Cache-key fidelity: ``tier="auto"`` then ``tier="1k"`` must hit the
  cache (no double download). The exact bug class the spec calls out.
* ``VisAsset.resolved_tier`` lazy accessor — populated when textures
  resolves; usable as the source-of-truth for bake-pipeline manifests.

Test fixtures use plain mocks against ``client.index`` and friends so
the suite stays offline + deterministic. No network IO, no HF substrate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mat_vis_client import (
    MaterialNotStagedError,
    MatVisClient,
    NoPreviewError,
    VisAsset,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _entry(material_id: str, *, tiers: list[str]) -> dict:
    """Build a minimal index entry with controllable available_tiers."""
    return {
        "id": material_id,
        "source": "gpuopen",
        "mat_vis": {"name": material_id, "pbr": {}},
        "available_tiers": tiers,
        "maps": ["color", "normal"],
    }


# ── _resolve_tier table-driven ─────────────────────────────────


@pytest.mark.parametrize(
    "staged_tiers,expected",
    [
        # Auto picks the highest preview-ladder rank that's staged.
        (["1k"], "1k"),
        (["1k", "512", "256"], "1k"),
        (["512", "256", "128"], "512"),
        (["256", "128"], "256"),
        (["128"], "128"),
        # Auto with higher-than-1k tiers: still prefers 1k for REPL
        # (the ladder caps at 1k by design — see _AUTO_TIER_LADDER).
        (["8k", "4k", "2k", "1k", "512"], "1k"),
        # Future tiers (e.g. "16k") sort to nowhere and are skipped.
        (["16k", "1k"], "1k"),
        # Scalar-only: short-circuits to "scalar" (REPL-friendly).
        (["scalar"], "scalar"),
        # Mixed (scalar + textures): textures win — scalar is a fallback
        # not a preference.
        (["scalar", "1k"], "1k"),
        # All-future-tiers (unknown to this client): no usable tier,
        # raises MaterialNotStagedError with the staged hint intact.
    ],
)
def test_resolve_tier_auto(staged_tiers, expected):
    c = MatVisClient()
    entry = _entry("M1", tiers=staged_tiers)
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        assert c._resolve_tier("gpuopen", "M1", "auto") == expected


def test_resolve_tier_auto_no_known_tiers_raises():
    """All staged tiers unknown to this client — auto can't pick anything."""
    c = MatVisClient()
    entry = _entry("Future", tiers=["16k", "32k"])
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        with pytest.raises(MaterialNotStagedError) as exc:
            c._resolve_tier("gpuopen", "Future", "auto")
    # The error surfaces the raw staged tiers so the user sees what's
    # actually there (forward-compat hint for clients on a newer
    # substrate they don't recognize yet).
    assert exc.value.available == ["16k", "32k"]
    assert exc.value.tier == "auto"


def test_resolve_tier_auto_empty_index_raises():
    """No entry in the index — auto can't decide, raises with empty hint."""
    c = MatVisClient()
    with patch.object(c, "_load_index_raw", return_value=[]):
        with pytest.raises(MaterialNotStagedError) as exc:
            c._resolve_tier("gpuopen", "GhostMaterial", "auto")
    assert exc.value.available == []


@pytest.mark.parametrize(
    "staged_tiers,expected",
    [
        (["8k", "4k", "2k", "1k"], "8k"),
        (["4k", "2k", "1k"], "4k"),
        (["2k", "1k"], "2k"),
        (["1k"], "1k"),
        (["512", "256", "128"], "512"),
        (["256", "128"], "256"),
        (["128"], "128"),
    ],
)
def test_resolve_tier_best(staged_tiers, expected):
    c = MatVisClient()
    entry = _entry("M1", tiers=staged_tiers)
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        assert c._resolve_tier("gpuopen", "M1", "best") == expected


def test_resolve_tier_best_scalar_only_raises():
    """``best`` has NO scalar fallback (archival contract — explicit failure)."""
    c = MatVisClient()
    entry = _entry("Aluminum", tiers=["scalar"])
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        with pytest.raises(MaterialNotStagedError) as exc:
            c._resolve_tier("physicallybased", "Aluminum", "best")
    # The hint omits "scalar" — it's the sentinel, not a quality tier
    # the user could pin.
    assert "scalar" not in exc.value.available
    assert exc.value.tier == "best"


def test_resolve_tier_best_no_textures_raises_with_empty_hint():
    """No staged tiers at all — best raises with available=[]."""
    c = MatVisClient()
    entry = _entry("Empty", tiers=[])
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        with pytest.raises(MaterialNotStagedError) as exc:
            c._resolve_tier("gpuopen", "Empty", "best")
    assert exc.value.available == []


def test_resolve_tier_literal_passthrough():
    """Literal tier names round-trip unchanged — no index lookup."""
    c = MatVisClient()
    # Must NOT call into index (we'd raise if it did). Literal tier is
    # the trivial path: caller asked for "1k", return "1k".
    with patch.object(c, "_load_index_raw", side_effect=AssertionError("no lookup")):
        assert c._resolve_tier("gpuopen", "anything", "1k") == "1k"
        assert c._resolve_tier("gpuopen", "anything", "ktx2-1k") == "ktx2-1k"
        assert c._resolve_tier("gpuopen", "anything", "scalar") == "scalar"


# ── fetch_all_textures default + routing ──────────────────────


def test_fetch_all_textures_default_auto_returns_1k_textures():
    """Acceptance #1: `client.fetch_all_textures(source, mid)` with no
    tier kwarg returns 1k textures when 1k is staged."""
    c = MatVisClient()
    entry = _entry("Bark001", tiers=["1k", "512"])
    captured: dict = {}

    def fake_fetch(source, mid, ch, tier):
        captured["tier"] = tier
        return PNG_MAGIC + ch.encode()

    with (
        patch.object(c, "_load_index_raw", return_value=[entry]),
        patch.object(c, "_resolve_material_id", return_value="Bark001"),
        patch.object(c, "channels", return_value=["color", "normal"]),
        patch.object(c, "fetch_texture", side_effect=fake_fetch),
    ):
        out = c.fetch_all_textures("ambientcg", "Bark001")  # NO tier kwarg
    assert captured["tier"] == "1k"  # default flipped to auto → 1k
    assert set(out) == {"color", "normal"}


def test_fetch_all_textures_auto_scalar_only_returns_empty_dict():
    """Acceptance #2: scalar-only with default auto returns ``{}``
    (REPL-friendly — `_scalars_for` still produces the PBR dict)."""
    c = MatVisClient()
    entry = _entry("Aluminum", tiers=["scalar"])
    with (
        patch.object(c, "_load_index_raw", return_value=[entry]),
        patch.object(c, "fetch_texture") as fetch_mock,
    ):
        out = c.fetch_all_textures("physicallybased", "Aluminum")
    assert out == {}
    fetch_mock.assert_not_called()  # short-circuit — no network call


def test_fetch_all_textures_best_scalar_only_raises():
    """Acceptance #3: scalar-only with ``tier="best"`` raises loudly.
    Archival contract: ``best`` never falls back to scalar."""
    c = MatVisClient()
    entry = _entry("Aluminum", tiers=["scalar"])
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        with pytest.raises(MaterialNotStagedError):
            c.fetch_all_textures("physicallybased", "Aluminum", tier="best")


# ── Cache-key fidelity ────────────────────────────────────────


def test_auto_then_explicit_1k_is_cache_hit(tmp_path):
    """Acceptance #4: ``tier="auto"`` then ``tier="1k"`` must hit the
    cache (no double download).

    The exact bug class the spec calls out: if ``auto`` were used as a
    literal cache-key segment, the second call (with explicit ``"1k"``)
    would miss and re-download. We collapse before cache, so the on-disk
    layout is keyed by the resolved tier.

    We observe via the ``on_event`` hook: ``download_start`` fires once
    per real network fetch (cache hits stay silent).
    """
    events: list[str] = []

    def on_event(ev):
        events.append(ev.kind)

    c = MatVisClient(cache_dir=tmp_path, on_event=on_event)
    entry = _entry("Rock064", tiers=["1k"])
    # Stub the network: return PNG bytes for any per-file URL.
    png_body = PNG_MAGIC + b"rock"

    # Patch all the bits fetch_texture leans on so we don't touch HF.
    # manifest must report the source + tier as valid; index must
    # advertise the material; tier-complete sentinel must pass.
    fake_manifest = {"sources": {"ambientcg": {"tiers": {"1k": {}}, "catalog": "ambientcg.json"}}}

    with (
        patch.object(
            MatVisClient, "manifest", new_callable=lambda: property(lambda self: fake_manifest)
        ),
        patch.object(c, "_load_index_raw", return_value=[entry]),
        patch.object(c, "_assert_tier_complete"),
        patch("mat_vis_client.client._get", return_value=png_body),
    ):
        # First call: auto resolves to 1k, real fetch happens.
        out1 = c.fetch_all_textures("ambientcg", "Rock064", "auto")
        # Second call: explicit 1k. Cache key is …/ambientcg/1k/Rock064/color.png
        # — the same path the first call wrote. Must be a cache hit.
        out2 = c.fetch_all_textures("ambientcg", "Rock064", "1k")

    assert out1 == out2
    # One download_start per channel on the first call; zero on the
    # second (cache hits stay silent per the on_event contract).
    starts = [e for e in events if e == "download_start"]
    ends = [e for e in events if e == "download_end"]
    # We expect exactly len(channels) network round-trips. Channels
    # comes from the entry's "maps": color + normal = 2 channels.
    assert len(starts) == 2
    assert len(ends) == 2


# ── VisAsset.resolved_tier ────────────────────────────────────


def test_visasset_resolved_tier_auto_collapses_lazily():
    """`.resolved_tier` is lazy: first access triggers resolution."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "M1", "auto")
    entry = _entry("M1", tiers=["1k", "512"])
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        assert a.resolved_tier == "1k"


def test_visasset_resolved_tier_best_picks_largest():
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "M1", "best")
    entry = _entry("M1", tiers=["2k", "1k", "512"])
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        assert a.resolved_tier == "2k"


def test_visasset_resolved_tier_scalar_only_short_circuit():
    """Scalar-only assets resolve to "scalar" without hitting the
    auto/best ladder — important for bake-pipeline manifests that
    record what was actually used."""
    c = MatVisClient()
    a = VisAsset(c, "physicallybased", "Aluminum", "auto")
    entry = _entry("Aluminum", tiers=["scalar"])
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        assert a.resolved_tier == "scalar"


def test_visasset_resolved_tier_literal_is_passthrough():
    """For a literal tier the resolved value equals the pinned tier."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "M1", "1k")
    entry = _entry("M1", tiers=["1k", "512"])
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        assert a.resolved_tier == "1k"


def test_visasset_textures_caches_resolved_tier():
    """After .textures resolves auto, .resolved_tier reflects the choice."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "M1", "auto")
    entry = _entry("M1", tiers=["512"])
    with (
        patch.object(c, "_load_index_raw", return_value=[entry]),
        patch.object(c, "fetch_all_textures", return_value={"color": PNG_MAGIC}) as fetch_mock,
    ):
        _ = a.textures
    # fetch_all_textures was called with the *resolved* tier, not "auto".
    fetch_mock.assert_called_once_with("gpuopen", "M1", "512")
    assert a.resolved_tier == "512"


# ── fetch_texture single-channel auto/best ────────────────────


def test_fetch_texture_auto_scalar_only_raises_no_preview():
    """Single-channel fetch with auto on a scalar-only material has no
    bytes to return — raises NoPreviewError (parity with thumb)."""
    c = MatVisClient()
    entry = _entry("Aluminum", tiers=["scalar"])
    with patch.object(c, "_load_index_raw", return_value=[entry]):
        with pytest.raises(NoPreviewError):
            c.fetch_texture("physicallybased", "Aluminum", "color", tier="auto")
