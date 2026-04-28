"""Tests for mat_vis_baker.common."""

import logging

from mat_vis_baker.common import (
    UpstreamBlock,
    _filter_upstream,
    normalize_category,
    normalize_channel,
    normalize_spdx,
)


class TestNormalizeCategory:
    def test_known_metal(self):
        assert normalize_category("Metal/Steel") == "metal"

    def test_known_wood(self):
        assert normalize_category("Wood") == "wood"

    def test_hierarchical_first_segment(self):
        assert normalize_category("Stone/Marble/White") == "stone"

    def test_unknown_falls_to_other(self):
        assert normalize_category("FooBarBaz") == "other"

    def test_empty_string(self):
        assert normalize_category("") == "other"

    def test_case_insensitive(self):
        assert normalize_category("CONCRETE") == "concrete"

    def test_organic_soil(self):
        assert normalize_category("Soil") == "organic"

    def test_planks_now_maps_to_wood(self):
        # mat-vis#178: ambientcg 'Planks' category, 59 records
        assert normalize_category("Planks") == "wood"

    # ── tag fallback (#178) ───────────────────────────────────────

    def test_tag_fallback_rescues_ambientcg_diamond_plate(self):
        # ambientcg 'Diamond Plate' category has no direct keyword hit,
        # but its tags carry material tokens (metal, steel, plate).
        assert (
            normalize_category(
                "Diamond Plate",
                tags=["9", "diamond", "floor", "metal", "plate", "steel"],
            )
            == "metal"
        )

    def test_tag_fallback_rescues_polyhaven_context_only_categories(self):
        # polyhaven 'Anti Skid Tiles' style: categories=[floor, man made]
        # (all context), tags contain "tiles" -> ceramic.
        assert (
            normalize_category(
                "floor",
                tags=["antislip", "tiles", "patio", "nonslip"],
            )
            == "ceramic"
        )

    def test_tag_fallback_is_first_hit_wins(self):
        # Documented behaviour: when multiple tags match different
        # categories, tag-list order decides (upstream-authored order).
        # Acceptable trade-off vs voting/weighting because real catalogs
        # tend to lead with the primary material in the tag list.
        # "Aerial Ground Rock" style — mud comes before rocks so organic
        # wins over stone. Both are legitimate readings of the texture.
        assert (
            normalize_category(
                "aerial",
                tags=["mud", "rocks", "stones", "dirt"],
            )
            == "organic"
        )

    def test_tag_fallback_skips_ambiguous_color_metals(self):
        # "gold", "silver", "copper", "brass", "bronze", "chrome" double
        # as color words on stylistic items (gold-coloured wallpaper,
        # bronze fabric). Skipped in the tag path so a wallpaper tagged
        # [gold, floral] doesn't get misclassified as metal.
        assert (
            normalize_category(
                "Wallpaper",
                tags=["art-deco", "gold", "floral", "bronze"],
            )
            == "other"
        )
        # Primary path is unaffected — "Gold" as a category still maps.
        assert normalize_category("Gold") == "metal"

    def test_tag_fallback_does_not_override_primary_hit(self):
        # When the primary category already resolves, tags are not
        # consulted — caller's category choice wins.
        assert normalize_category("Metal", tags=["wood"]) == "metal"

    def test_tag_fallback_wallpaper_stays_other(self):
        # gpuopen 'Wallpaper' — tags are stylistic, no material token.
        # Should legitimately stay 'other'.
        assert (
            normalize_category(
                "Wallpaper",
                tags=["art-deco", "blue", "floral", "gold", "pattern"],
            )
            == "other"
        )

    def test_tag_fallback_empty_category_uses_tags(self):
        # Sources whose category field is empty fall straight through
        # to the tag scan.
        assert normalize_category("", tags=["carpet", "red"]) == "fabric"

    def test_tag_fallback_plural_handled(self):
        # 'Planks' category is now mapped directly (via 'plank' kw),
        # but tags like 'Tiles' should also work via plural fallback.
        assert normalize_category("Something", tags=["Tiles"]) == "ceramic"

    def test_tag_fallback_skips_non_string_tags(self):
        # Upstream payloads occasionally carry ints or dicts in tag
        # arrays; those must not crash the normalizer.
        assert (
            normalize_category(
                "Unknown",
                tags=[42, None, {"x": 1}, "wood"],
            )
            == "wood"
        )


class TestNormalizeChannel:
    def test_ambientcg_color(self):
        assert normalize_channel("ambientcg", "Color") == "color"

    def test_ambientcg_normalgl(self):
        assert normalize_channel("ambientcg", "NormalGL") == "normal"

    def test_ambientcg_ao(self):
        assert normalize_channel("ambientcg", "AmbientOcclusion") == "ao"

    def test_polyhaven_diffuse(self):
        assert normalize_channel("polyhaven", "diffuse") == "color"

    def test_polyhaven_nor_gl(self):
        assert normalize_channel("polyhaven", "nor_gl") == "normal"

    def test_unknown_returns_none(self):
        assert normalize_channel("ambientcg", "SomeWeirdMap") is None

    def test_unknown_source(self):
        assert normalize_channel("unknown_source", "color") is None


class TestFilterUpstream:
    def test_keeps_allowlisted_keys(self):
        raw = {"a": 1, "b": 2, "c": 3}
        assert _filter_upstream(raw, frozenset({"a", "c"})) == {"a": 1, "c": 3}

    def test_drops_unlisted_keys(self):
        raw = {"keep": 1, "drop": 2}
        assert _filter_upstream(raw, frozenset({"keep"})) == {"keep": 1}

    def test_empty_allowlist_returns_empty_dict(self):
        assert _filter_upstream({"a": 1}, frozenset()) == {}

    def test_non_dict_input_returns_empty(self):
        assert _filter_upstream(None, frozenset({"a"})) == {}
        assert _filter_upstream("oops", frozenset({"a"})) == {}
        assert _filter_upstream([1, 2, 3], frozenset({"a"})) == {}

    def test_shallow_nested_dict_passes_through_verbatim(self):
        raw = {"meta": {"nested": "kept-whole"}, "drop": "bye"}
        out = _filter_upstream(raw, frozenset({"meta"}))
        assert out == {"meta": {"nested": "kept-whole"}}
        # shallow copy at top-level; nested dicts are identity-shared and
        # that's fine — we never mutate filtered output in the pipeline.
        assert out["meta"] is raw["meta"]

    def test_missing_keys_are_not_inserted(self):
        """Allowlist lists what we WANT; absent keys stay absent."""
        assert _filter_upstream({}, frozenset({"a", "b"})) == {}


class TestNormalizeSpdx:
    def test_normalize_spdx_known_gpuopen(self):
        assert normalize_spdx("MIT Public Domain") == "MIT"

    def test_normalize_spdx_unknown_fallback(self, caplog):
        with caplog.at_level(logging.WARNING, logger="mat-vis-baker"):
            assert normalize_spdx("Weird New License v7") == "NOASSERTION"
        assert "unknown upstream license" in caplog.text

    def test_normalize_spdx_none_and_empty(self):
        assert normalize_spdx(None) == "NOASSERTION"
        assert normalize_spdx("") == "NOASSERTION"
        assert normalize_spdx("   ") == "NOASSERTION"

    def test_normalize_spdx_whitespace_stripped(self):
        assert normalize_spdx("  MIT Public Domain  ") == "MIT"


class TestUpstreamBlock:
    def test_defaults(self):
        u = UpstreamBlock()
        assert u.source == ""
        assert u.schema_version == 1
        assert u.fetched_at is None
        assert u.raw is None

    def test_explicit_fields(self):
        u = UpstreamBlock(
            source="ambientcg",
            schema_version=1,
            fetched_at="2026-04-20T16:00:00Z",
            raw={"assetId": "Bricks097"},
        )
        assert u.source == "ambientcg"
        assert u.raw == {"assetId": "Bricks097"}
