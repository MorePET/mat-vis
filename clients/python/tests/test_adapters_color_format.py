"""Tests for to_threejs ``color_format`` kwarg (ADR-0013 §Decision-2 / #298).

py-mat #99 (Bernhard, build123d): emitting ``result["color"]`` as a
hex int (``12566468``) is opaque in the REPL, doesn't round-trip
through JSON for non-Three.js consumers, and is un-Pythonic. The
substrate gains a ``color_format: Literal["hex", "int"]`` kwarg; the
default flips from ``"int"`` (current) to ``"hex"`` in 0.7.0.

0.6.5 phasing:
    color_format=None (unset) → DeprecationWarning + "int" (preserves
                                current behavior; gives downstream
                                callers a minor cycle to migrate).
    color_format="int"        → silent; result["color"] is hex int.
    color_format="hex"        → silent; result["color"] is "#RRGGBB"
                                string (Three.js MeshPhysicalMaterial
                                accepts both lossless).

0.7.0 will drop the sentinel: default flips to ``"hex"``, no warning.

#298.
"""

from __future__ import annotations

import warnings

import pytest

from mat_vis_client.adapters import to_threejs


class TestColorFormatDefault:
    """Default (unset) behavior — emits DeprecationWarning, returns int."""

    def test_default_emits_deprecation_warning(self):
        with pytest.warns(DeprecationWarning, match="color_format"):
            to_threejs({"color_hex": "#bfbfc4"})

    def test_default_preserves_int_behavior(self):
        with pytest.warns(DeprecationWarning):
            result = to_threejs({"color_hex": "#bfbfc4"})
        assert result["color"] == 0xBFBFC4
        assert isinstance(result["color"], int)

    def test_no_warning_when_color_hex_absent(self):
        # color_format only governs the color emit; no color_hex → no warning.
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            to_threejs({"metalness": 1.0, "roughness": 0.3})

    def test_no_warning_when_color_hex_is_none(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
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

    def test_invalid_format_only_raises_when_color_hex_present(self):
        # Without color_hex, color_format is unused — no validation needed.
        result = to_threejs({}, color_format="rgb")  # type: ignore[arg-type]
        assert "color" not in result


class TestColorFormatKwargOnly:
    """color_format is keyword-only — positional pass should fail."""

    def test_positional_third_arg_rejected(self):
        with pytest.raises(TypeError):
            # Third positional arg should TypeError because color_format
            # is behind a `*` separator.
            to_threejs({}, None, "hex")  # type: ignore[misc]
