"""Tests for VisAsset.thumb / thumb_for / safe_thumb / _repr_html_
and MatVisClient.prefetch_thumbs (mat-vis preview surface).

The thumb resolver covers two intersecting concerns:

  - **Tier resolution**: ``"thumb"`` is a named alias resolving to a
    dedicated baked tier (mat-vis#361, future) or fallback through
    the preview ladder (128 → 256 → 512 → 1k). Explicit tier names
    bypass the alias.
  - **Channel resolution**: default ``channel=None`` walks the channel
    fallback ladder; explicit channel names try only that channel.

Tests are organised by failure-mode dimension:

  - Happy paths (5 cases: dedicated thumb, fallback ladder, channel
    fallback, explicit overrides, propagation through .thumb)
  - Scalar-only sources → NoPreviewError
  - No preview-sized tier staged → PreviewUnavailableError
  - Network failures (404 vs 5xx vs network) → typed propagation
  - Resolver fallback (tier-level + channel-level)
  - safe_thumb non-raising contract
  - _repr_png_ + _repr_html_ rich-repr behaviour
  - prefetch_thumbs concurrency + counters
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mat_vis_client import (
    HTTPFetchError,
    MatVisClient,
    MatVisError,
    NetworkError,
    NoPreviewError,
    PreviewUnavailableError,
    ThumbResult,
    VisAsset,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _entry(material_id: str, *, tiers: list[str] | None) -> dict:
    """Build a minimal index entry with controllable available_tiers."""
    return {
        "id": material_id,
        "source": "gpuopen",
        "mat_vis": {"name": material_id, "pbr": {}},
        "available_tiers": tiers if tiers is not None else [],
        "maps": ["color", "normal"],
    }


# ── Happy paths ─────────────────────────────────────────────────


def test_thumb_uses_dedicated_thumb_tier_when_staged():
    """When the substrate ships a dedicated 'thumb' tier (mat-vis#361),
    .thumb resolves to it directly — no fallback ladder traversal."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Aluminum Brushed", "1k")
    entry = _entry("Aluminum Brushed", tiers=["thumb", "1k", "2k"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", return_value=PNG_MAGIC + b"thumb") as fetch,
    ):
        png = a.thumb
    assert png == PNG_MAGIC + b"thumb"
    fetch.assert_called_once_with("gpuopen", "Aluminum Brushed", "color", tier="thumb")


def test_thumb_falls_back_to_smallest_preview_tier():
    """No dedicated 'thumb' tier — resolves to smallest staged tier ≤ 1k."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["256", "512", "1k", "2k"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", return_value=PNG_MAGIC + b"256") as fetch,
    ):
        png = a.thumb
    assert png == PNG_MAGIC + b"256"
    fetch.assert_called_once_with("gpuopen", "Steel", "color", tier="256")


def test_thumb_for_explicit_tier_bypasses_alias():
    """Explicit tier= kwarg disables the named-alias resolver."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["thumb", "128", "512"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", return_value=PNG_MAGIC + b"512") as fetch,
    ):
        png = a.thumb_for(tier="512")
    assert png == PNG_MAGIC + b"512"
    fetch.assert_called_once_with("gpuopen", "Steel", "color", tier="512")


def test_thumb_for_explicit_channel_bypasses_ladder():
    """Explicit channel= kwarg disables channel-fallback walk."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["256", "1k"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", return_value=PNG_MAGIC + b"normal") as fetch,
    ):
        png = a.thumb_for(channel="normal")
    fetch.assert_called_once_with("gpuopen", "Steel", "normal", tier="256")
    assert png == PNG_MAGIC + b"normal"


def test_thumb_property_is_thumb_for_with_defaults():
    """`.thumb` is exactly `.thumb_for()` — same call, no divergence."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["128"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", return_value=PNG_MAGIC) as fetch,
    ):
        prop_result = a.thumb
        method_result = a.thumb_for()
    assert prop_result == method_result == PNG_MAGIC
    assert fetch.call_count == 2


# ── Scalar-only sources ─────────────────────────────────────────


def test_thumb_raises_no_preview_for_scalar_only_source():
    """physicallybased entries (available_tiers=[]) raise NoPreviewError —
    distinct from PreviewUnavailableError. Closes by mat-vis#361."""
    c = MatVisClient()
    a = VisAsset(c, "physicallybased", "Aluminum", "1k")
    entry = _entry("Aluminum", tiers=[])  # scalar-only
    with patch.object(c, "index", return_value=[entry]):
        with pytest.raises(NoPreviewError) as exc_info:
            _ = a.thumb
    assert exc_info.value.source == "physicallybased"
    assert exc_info.value.material_id == "Aluminum"
    # Error message references the substrate-side tracking issue.
    assert "361" in str(exc_info.value)


def test_thumb_for_also_raises_no_preview_for_scalar_only():
    """Even with explicit kwargs, scalar-only short-circuits to NoPreviewError —
    user can't override the absence of textures."""
    c = MatVisClient()
    a = VisAsset(c, "physicallybased", "Aluminum", "1k")
    entry = _entry("Aluminum", tiers=[])
    with patch.object(c, "index", return_value=[entry]):
        with pytest.raises(NoPreviewError):
            a.thumb_for(channel="color", tier="1k")


# ── No preview-sized tier ───────────────────────────────────────


def test_thumb_raises_preview_unavailable_when_no_small_tier_staged():
    """Material has 2k+ tiers only — no preview-ladder match. Raises
    PreviewUnavailableError carrying staged tiers as a hint."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Chrome", "1k")
    entry = _entry("Chrome", tiers=["2k", "4k", "8k"])
    with patch.object(c, "index", return_value=[entry]):
        with pytest.raises(PreviewUnavailableError) as exc_info:
            _ = a.thumb
    assert exc_info.value.available == ["2k", "4k", "8k"]
    assert "2k" in str(exc_info.value)  # message hints at .thumb_for(tier='2k')


def test_preview_unavailable_when_only_ktx2_tiers_staged():
    """ktx2-* tiers don't qualify as preview tiers (clients want PNG)."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Chrome", "1k")
    entry = _entry("Chrome", tiers=["ktx2-512", "ktx2-1k"])
    with patch.object(c, "index", return_value=[entry]):
        with pytest.raises(PreviewUnavailableError):
            _ = a.thumb


# ── Network failures ────────────────────────────────────────────


def test_thumb_propagates_5xx_immediately_no_fallback():
    """5xx is a substrate problem, not a 'this combo doesn't exist'
    signal. Resolver must NOT swallow it across tier/channel attempts."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["128", "256", "512"])
    err = HTTPFetchError("https://hf/x", 503, "Service Unavailable")
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", side_effect=err) as fetch,
    ):
        with pytest.raises(HTTPFetchError) as exc_info:
            _ = a.thumb
    assert exc_info.value.code == 503
    # Exactly one call — fallback must not retry on 5xx.
    assert fetch.call_count == 1


def test_thumb_swallows_404_and_falls_through_tiers():
    """404 means 'this tier isn't actually staged' (catalog over-claimed
    or substrate gap). Default-args resolver tries the next candidate."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["128", "256", "512"])
    not_found = HTTPFetchError("https://hf/x", 404, "Not Found")
    side_effects = [not_found, not_found, PNG_MAGIC + b"512"]
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", side_effect=side_effects) as fetch,
    ):
        png = a.thumb
    # Exhausted color-channel for 128, 256 → succeeded at 512.
    assert png == PNG_MAGIC + b"512"
    assert fetch.call_count == 3


def test_thumb_for_explicit_tier_propagates_404_directly():
    """When user named both tier and channel, 404 propagates verbatim —
    they asked for a specific thing, give them the specific failure."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["128", "256", "512"])
    err = HTTPFetchError("https://hf/x", 404, "Not Found")
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", side_effect=err),
    ):
        with pytest.raises(HTTPFetchError) as exc_info:
            a.thumb_for(channel="color", tier="256")
    assert exc_info.value.code == 404


def test_thumb_propagates_network_error():
    """NetworkError (DNS / connection) is not a 'doesn't exist' signal
    either — it goes through to the caller."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["128"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", side_effect=NetworkError("https://hf/x", "DNS")),
    ):
        with pytest.raises(NetworkError):
            _ = a.thumb


# ── Channel fallback ────────────────────────────────────────────


def test_thumb_channel_ladder_falls_back_to_normal_when_color_missing():
    """Metallic-only or normal-only materials still produce *some*
    preview — the channel ladder walks color → basecolor → albedo →
    normal → roughness."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "MetallicOnly", "1k")
    entry = _entry("MetallicOnly", tiers=["128"])
    # Channel-not-found surfaces as MatVisError from fetch_texture's
    # pre-flight check (client.py L1822).
    color_err = MatVisError("channel 'color' not found ...")
    basecolor_err = MatVisError("channel 'basecolor' not found ...")
    albedo_err = MatVisError("channel 'albedo' not found ...")
    side_effects = [color_err, basecolor_err, albedo_err, PNG_MAGIC + b"normal"]
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", side_effect=side_effects) as fetch,
    ):
        png = a.thumb
    assert png == PNG_MAGIC + b"normal"
    # Walked the ladder up to and including 'normal'.
    assert fetch.call_count == 4


# ── safe_thumb non-raising ──────────────────────────────────────


def test_safe_thumb_returns_thumb_result_on_success():
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["128"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", return_value=PNG_MAGIC),
    ):
        result = a.safe_thumb()
    assert isinstance(result, ThumbResult)
    assert result.png == PNG_MAGIC
    assert result.error is None
    assert result.reason is None


def test_safe_thumb_traps_no_preview_error():
    """Scalar-only source → ThumbResult with error tag, never raises."""
    c = MatVisClient()
    a = VisAsset(c, "physicallybased", "Aluminum", "1k")
    entry = _entry("Aluminum", tiers=[])
    with patch.object(c, "index", return_value=[entry]):
        result = a.safe_thumb()
    assert result.png is None
    assert result.error == "NoPreviewError"
    assert "scalar-only" in result.reason


def test_safe_thumb_traps_preview_unavailable_error():
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Chrome", "1k")
    entry = _entry("Chrome", tiers=["2k", "4k"])
    with patch.object(c, "index", return_value=[entry]):
        result = a.safe_thumb()
    assert result.png is None
    assert result.error == "PreviewUnavailableError"


def test_safe_thumb_traps_network_error():
    """Even loud failures (5xx, network) become ThumbResult — that's
    the contract for iteration / MCP composition."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["128"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(
            c,
            "fetch_texture",
            side_effect=HTTPFetchError("https://hf/x", 503, "down"),
        ),
    ):
        result = a.safe_thumb()
    assert result.png is None
    assert result.error == "HTTPFetchError"
    assert "503" in result.reason


def test_safe_thumb_iteration_does_not_break_on_one_bad_material():
    """Real consumer pain: ``[m.safe_thumb() for m in mats]`` must
    produce N results when one of them has no preview."""
    c = MatVisClient()
    good = VisAsset(c, "gpuopen", "Good", "1k")
    bad = VisAsset(c, "physicallybased", "Bad", "1k")

    def _index(source):
        if source == "gpuopen":
            return [_entry("Good", tiers=["128"])]
        return [_entry("Bad", tiers=[])]

    with (
        patch.object(c, "index", side_effect=_index),
        patch.object(c, "fetch_texture", return_value=PNG_MAGIC),
    ):
        results = [m.safe_thumb() for m in (good, bad)]
    assert len(results) == 2
    assert results[0].png == PNG_MAGIC
    assert results[1].png is None
    assert results[1].error == "NoPreviewError"


# ── Rich-repr (Jupyter / IPython) ───────────────────────────────


def test_repr_png_returns_bytes_on_success():
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["128"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", return_value=PNG_MAGIC),
    ):
        assert a._repr_png_() == PNG_MAGIC


def test_repr_png_returns_none_on_failure_so_html_takes_over():
    """Bernhard's #312 pain: cell shows nothing with no explanation.
    _repr_png_ returns None and _repr_html_ then renders the diagnostic."""
    c = MatVisClient()
    a = VisAsset(c, "physicallybased", "Aluminum", "1k")
    entry = _entry("Aluminum", tiers=[])
    with patch.object(c, "index", return_value=[entry]):
        assert a._repr_png_() is None


def test_repr_html_returns_none_on_success_so_png_wins():
    """When PNG is available, _repr_html_ must return None — IPython
    picks the higher-priority repr that returns non-None, so two
    non-None values would double-display."""
    c = MatVisClient()
    a = VisAsset(c, "gpuopen", "Steel", "1k")
    entry = _entry("Steel", tiers=["128"])
    with (
        patch.object(c, "index", return_value=[entry]),
        patch.object(c, "fetch_texture", return_value=PNG_MAGIC),
    ):
        assert a._repr_html_() is None


def test_repr_html_renders_diagnostic_on_failure():
    c = MatVisClient()
    a = VisAsset(c, "physicallybased", "Aluminum", "1k")
    entry = _entry("Aluminum", tiers=[])
    with patch.object(c, "index", return_value=[entry]):
        html = a._repr_html_()
    assert html is not None
    assert "preview unavailable" in html
    assert "NoPreviewError" in html
    assert "physicallybased" in html
    assert "Aluminum" in html


# ── prefetch_thumbs ─────────────────────────────────────────────


def test_prefetch_thumbs_counts_outcomes_correctly():
    """Walks index, fetches in parallel, returns ok/no_preview/
    unavailable/errors counts. Failures don't poison the run."""
    c = MatVisClient()
    entries = [
        _entry("Good1", tiers=["128"]),
        _entry("Good2", tiers=["256"]),
        _entry("Unavailable", tiers=["4k"]),
        _entry("ScalarOnly", tiers=[]),
    ]

    def _fetch(source, mid, channel, *, tier):
        return PNG_MAGIC

    with (
        patch.object(c, "index", return_value=entries),
        patch.object(c, "fetch_texture", side_effect=_fetch),
    ):
        counters = c.prefetch_thumbs("gpuopen", max_workers=2)

    assert counters["ok"] == 2
    assert counters["unavailable"] == 1
    assert counters["no_preview"] == 1
    assert counters["errors"] == 0


def test_prefetch_thumbs_counts_network_errors():
    """5xx during prefetch counted as 'errors' bucket — distinct from
    structural absences (no_preview / unavailable)."""
    c = MatVisClient()
    entries = [_entry("M1", tiers=["128"]), _entry("M2", tiers=["128"])]
    err = HTTPFetchError("https://hf/x", 503, "down")
    with (
        patch.object(c, "index", return_value=entries),
        patch.object(c, "fetch_texture", side_effect=err),
    ):
        counters = c.prefetch_thumbs("gpuopen", max_workers=2)
    assert counters["ok"] == 0
    assert counters["errors"] == 2
