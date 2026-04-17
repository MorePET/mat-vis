"""Tests for mat_vis_baker.common."""

from mat_vis_baker.common import normalize_category, normalize_channel


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
