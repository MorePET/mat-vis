"""Tests for mat_vis_baker.common."""

from mat_vis_baker.common import (
    UpstreamBlock,
    _filter_upstream,
    normalize_category,
    normalize_channel,
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
