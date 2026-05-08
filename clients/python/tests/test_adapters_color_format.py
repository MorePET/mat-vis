"""Tests for to_threejs ``color_format`` kwarg (ADR-0013 §Decision-2 / #298).

py-mat#99 (build123d): emitting ``result["color"]`` as a
hex int is opaque in the REPL, doesn't round-trip through JSON for
non-Three.js consumers, and is un-Pythonic. ``color_format: Literal[
"hex", "int"]`` exposes the choice; default is ``"hex"`` since 0.7.0.

  color_format="hex" (default) → result["color"] is "#RRGGBB" string.
  color_format="int"           → result["color"] is hex int (legacy).
  color_format=anything else   → ValueError (eager validation).

#298.
"""

from __future__ import annotations

import warnings

import pytest

from mat_vis_client.adapters import to_threejs


class TestColorFormatDefault:
    """Default behavior (0.7.0): emits ``"#RRGGBB"`` string, no warning."""

    def test_default_emits_hex_string(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = to_threejs({"color_hex": "#bfbfc4"})
        assert result["color"] == "#bfbfc4"
        assert isinstance(result["color"], str)

    def test_no_warning_for_any_call(self):
        # The 0.6.x DeprecationWarning is gone in 0.7.0.
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            to_threejs({"color_hex": "#FF0000"})
            to_threejs({"metalness": 1.0, "roughness": 0.3})
            to_threejs({"color_hex": None})


class TestColorFormatExplicit:
    """Explicit kwarg — no warning, behavior pinned."""

    def test_int_format_explicit(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = to_threejs({"color_hex": "#bfbfc4"}, color_format="int")
        assert result["color"] == 0xBFBFC4
        assert isinstance(result["color"], int)

    def test_hex_format_explicit(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = to_threejs({"color_hex": "#bfbfc4"}, color_format="hex")
        assert result["color"] == "#bfbfc4"
        assert isinstance(result["color"], str)

    def test_hex_format_uppercase_preserved(self):
        # The adapter passes the hex string through verbatim; no normalization.
        result = to_threejs({"color_hex": "#FF0000"}, color_format="hex")
        assert result["color"] == "#FF0000"

    def test_int_format_pure_red(self):
        result = to_threejs({"color_hex": "#FF0000"}, color_format="int")
        assert result["color"] == 0xFF0000

    def test_int_format_pure_white(self):
        result = to_threejs({"color_hex": "#FFFFFF"}, color_format="int")
        assert result["color"] == 0xFFFFFF


class TestColorFormatInvalid:
    """Invalid values raise ValueError on first use."""

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="color_format"):
            to_threejs({"color_hex": "#bfbfc4"}, color_format="rgb")  # type: ignore[arg-type]

    def test_invalid_format_raises_eagerly_even_without_color(self):
        # 0.7.0: validation fires up front regardless of whether scalars
        # actually contain a color key. Fail fast for typos.
        with pytest.raises(ValueError, match="color_format"):
            to_threejs({}, color_format="rgb")  # type: ignore[arg-type]


class TestColorFormatKwargOnly:
    """color_format is keyword-only — positional pass should fail."""

    def test_positional_third_arg_rejected(self):
        with pytest.raises(TypeError):
            # Third positional arg should TypeError because color_format
            # is behind a `*` separator.
            to_threejs({}, None, "hex")  # type: ignore[misc]
