"""Regression tests for mat_vis_baker.common.normalize_category.

Covers the bug in mat-vis#150 where ~40% of real upstream categories
collapsed to "other" because _CATEGORY_MAP only contained singular
forms. Inputs below are sampled verbatim from the four upstream
sources' live responses (audit in mat-vis#152).
"""

from __future__ import annotations

import pytest

from mat_vis_baker.common import normalize_category


# ── the exact reproducer from the issue ─────────────────────────


class TestIssue150Reproducer:
    def test_bricks_plural(self):
        assert normalize_category("Bricks") == "ceramic"

    def test_rocks_plural(self):
        assert normalize_category("Rocks") == "stone"


# ── plural stripping (core fix) ─────────────────────────────────


class TestPluralStripping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Bricks", "ceramic"),
            ("Tiles", "ceramic"),
            ("Rocks", "stone"),
            ("Fabrics", "fabric"),
            ("Metals", "metal"),
            ("Woods", "wood"),
            ("Stones", "stone"),
            ("Plastics", "plastic"),
            ("Leaves", "organic"),  # already in map as plural, must still work
        ],
    )
    def test_plural_forms_map_to_canonical(self, raw: str, expected: str) -> None:
        assert normalize_category(raw) == expected

    def test_short_words_not_over_stripped(self):
        # 2-char words ending in "s" must not have "s" stripped (would
        # produce empty / 1-char garbage lookups). "as" -> would strip
        # to "a" which isn't in the map; must fall through to "other".
        assert normalize_category("as") == "other"
        assert normalize_category("is") == "other"


# ── CamelCase + multi-word display strings ──────────────────────


class TestCamelCaseAndMultiWord:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # ambientcg real display categories
            ("WoodFloor", "wood"),
            ("PaintedPlaster", "concrete"),  # plaster is already canonical concrete
            ("Terrazzo", "concrete"),
            ("Paving", "concrete"),
            # gpuopen real display categories
            ("Brick Wall", "ceramic"),
            ("Interior Wood", "wood"),  # synthetic but plausible — word-split still works
            ("Stone Wall", "stone"),
            ("Metal Plates", "metal"),
        ],
    )
    def test_multi_word_and_camel(self, raw: str, expected: str) -> None:
        assert normalize_category(raw) == expected


# ── audit sweep: real strings from the four upstream sources ────


class TestAuditSweep:
    """Real inputs sampled from mat-vis#152's live audit.

    Each case is tagged with its source in the parametrize id so a
    failure message points straight at the offender.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # ── ambientcg browse taxonomy (23 entries; 10 were broken) ──
            ("Atlas", "other"),  # meta/format tag — intentional other
            ("Bricks", "ceramic"),  # was "other"
            ("Concrete", "concrete"),
            ("Decal", "other"),  # meta/format tag — intentional other
            ("Fabric", "fabric"),
            ("Facade", "other"),  # multi-material context — intentional other
            ("Ground", "organic"),
            ("Marble", "stone"),
            ("Metal", "metal"),
            ("OnlyPBR", "other"),  # meta/format tag — intentional other
            ("PaintedPlaster", "concrete"),  # was "other"
            ("Paving", "concrete"),  # was "other"
            ("Plaster", "concrete"),
            ("Rocks", "stone"),  # was "other"
            ("Sign", "other"),  # meta/format tag — intentional other
            ("Terrazzo", "concrete"),  # was "other"
            ("Tiles", "ceramic"),  # was "other"
            ("Wood", "wood"),
            ("WoodFloor", "wood"),  # was "other"
            # ── physicallybased (8 top-level cats; 3 intentionally other) ──
            ("Liquid", "other"),  # not a PBR class — intentional other
            ("Manmade", "other"),  # too broad — intentional other
            ("Human", "other"),  # skin/hair isn't organic vegetation
            ("Wood", "wood"),
            ("Metal", "metal"),
            # ── gpuopen real display categories (5 were broken) ──
            ("Interior Flooring", "other"),  # multi-material — intentional other
            ("SciFi", "other"),  # stylistic — intentional other
            ("Wallpaper", "other"),  # multi-material — intentional other
            ("Roofing", "other"),  # multi-material — intentional other
            ("Base Materials", "other"),  # meta-collection — intentional other
        ],
    )
    def test_audit_inputs(self, raw: str, expected: str) -> None:
        assert normalize_category(raw) == expected, (
            f"{raw!r} -> {normalize_category(raw)!r}, expected {expected!r}"
        )


# ── no regressions against the pre-existing test_common.py cases ─


class TestNoRegressions:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Metal/Steel", "metal"),  # hierarchical first segment
            ("Wood", "wood"),
            ("Stone/Marble/White", "stone"),
            ("FooBarBaz", "other"),
            ("", "other"),
            ("CONCRETE", "concrete"),  # case insensitive
            ("Soil", "organic"),
            # lower-case plain
            ("metal", "metal"),
            ("ceramic", "ceramic"),
            ("glass", "glass"),
            ("fabric", "fabric"),
            ("plastic", "plastic"),
        ],
    )
    def test_no_regression(self, raw: str, expected: str) -> None:
        assert normalize_category(raw) == expected


# ── delimiters: dashes, underscores ─────────────────────────────


class TestDelimiters:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("wood-floor", "wood"),
            ("wood_floor", "wood"),
            ("brick_wall", "ceramic"),
            ("painted-plaster", "concrete"),
        ],
    )
    def test_delimiters(self, raw: str, expected: str) -> None:
        assert normalize_category(raw) == expected
