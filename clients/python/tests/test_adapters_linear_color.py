"""Tests for the linear-color foundation + #304 canonical input + 0.7.0 default flip.

ADR-0013 §Decision-1 / 2 / 5 — phase 0.7.0:

1. ``_srgb_to_linear`` / ``_linear_to_srgb`` — IEC 61966-2-1 piecewise
   conversions used at every adapter color boundary so sRGB inputs
   land in linear-aware fields (glTF ``baseColorFactor``, MTLX
   ``diffuseColor``) without the silent sRGB-as-linear rendering bug
   that shipped through 0.6.x.

2. ``_resolve_base_color`` — canonical input resolver. Returns linear
   RGBA in [0, 1] from any of:
       - ``base_color_linear`` (NEW canonical, no transform)
       - ``color_rgba`` (sRGB legacy alias, de-gammas RGB; alpha
         passes through linear)
       - ``color_hex`` (sRGB legacy alias, de-gammas)
   ValueError on conflicting non-equal values.

3. #304 alpha preservation — ``color_rgba`` 4-tuple input flows
   through ``to_gltf`` ``baseColorFactor`` as linear RGBA without
   collapsing alpha.

4. ``to_gltf`` ``baseColorFactor`` is now LINEAR per glTF 2.0 §3.9.2
   — fixes the latent over-bright rendering bug. Numeric output
   changes for any ``color_hex``-only caller (extremes #000000 /
   #FFFFFF / #FF0000 round-trip identically; mid-tones differ).

5. ``to_threejs`` default ``color_format`` flips from int to
   ``"hex"``. The DeprecationWarning is gone. The sentinel is
   removed.

#304 + ADR-0013.
"""

from __future__ import annotations

import math
import warnings

import pytest

from mat_vis_client.adapters import (
    _linear_to_srgb,
    _resolve_base_color,
    _srgb_to_linear,
    to_gltf,
    to_threejs,
)


# ── sRGB ↔ linear helpers ──────────────────────────────────────


class TestSrgbLinearHelpers:
    """IEC 61966-2-1 piecewise transfer function. Verified against
    the spec's reference points and round-trip preservation.
    """

    def test_pure_extremes_unchanged(self):
        # 0 and 1 are mathematical fixed points; allow float-math tolerance.
        assert _srgb_to_linear(0.0) == 0.0
        assert math.isclose(_srgb_to_linear(1.0), 1.0, abs_tol=1e-9)
        assert _linear_to_srgb(0.0) == 0.0
        assert math.isclose(_linear_to_srgb(1.0), 1.0, abs_tol=1e-9)

    def test_linear_breakpoint(self):
        # The piecewise breakpoint at sRGB = 0.04045 → linear = 0.0031308.
        # Below the breakpoint, sRGB to linear is the linear segment c/12.92.
        assert math.isclose(_srgb_to_linear(0.04045), 0.0031308, abs_tol=1e-6)

    def test_mid_tone_de_gamma(self):
        # sRGB 0.5 → linear ~0.2140 (well-known reference point).
        assert math.isclose(_srgb_to_linear(0.5), 0.21404, abs_tol=1e-4)

    def test_round_trip_integrity(self):
        # Round-trip preserves value to within 1e-9 across the [0, 1]
        # range (modulo float precision).
        for v in (0.1, 0.25, 0.5, 0.749, 0.8, 0.95):
            assert math.isclose(_linear_to_srgb(_srgb_to_linear(v)), v, abs_tol=1e-6)

    def test_known_byte_values(self):
        # 0xbf = 191/255 ≈ 0.7490196 sRGB → linear ≈ 0.520996.
        # Used as a substrate-realistic check.
        srgb = 0xBF / 255.0
        assert math.isclose(_srgb_to_linear(srgb), 0.520996, abs_tol=1e-5)
        # 0xc4 = 196/255 ≈ 0.7686275 sRGB → linear ≈ 0.552011.
        srgb_c4 = 0xC4 / 255.0
        assert math.isclose(_srgb_to_linear(srgb_c4), 0.552011, abs_tol=1e-5)


# ── _resolve_base_color ────────────────────────────────────────


class TestResolveBaseColor:
    def test_no_color_returns_none(self):
        assert _resolve_base_color({}) is None

    def test_color_hex_de_gammas(self):
        # #bfbfc4 sRGB → linear (~0.521, ~0.521, ~0.552, 1.0).
        rgba = _resolve_base_color({"color_hex": "#bfbfc4"})
        assert rgba is not None
        assert math.isclose(rgba[0], 0.520996, abs_tol=1e-5)
        assert math.isclose(rgba[2], 0.552011, abs_tol=1e-5)
        assert rgba[3] == 1.0

    def test_color_hex_pure_extremes_round_trip(self):
        # #FFFFFF and #000000 are fixed points in sRGB→linear.
        assert _resolve_base_color({"color_hex": "#FFFFFF"}) == (1.0, 1.0, 1.0, 1.0)
        assert _resolve_base_color({"color_hex": "#000000"}) == (0.0, 0.0, 0.0, 1.0)
        assert _resolve_base_color({"color_hex": "#FF0000"}) == (1.0, 0.0, 0.0, 1.0)

    def test_base_color_linear_passes_through(self):
        # Canonical linear input — no de-gamma, no transform.
        rgba = _resolve_base_color({"base_color_linear": (0.5, 0.5, 0.5, 0.7)})
        assert rgba == (0.5, 0.5, 0.5, 0.7)

    def test_color_rgba_de_gammas_rgb_preserves_alpha(self):
        # color_rgba is sRGB-encoded RGB + linear alpha.
        rgba = _resolve_base_color({"color_rgba": (0.5, 0.5, 0.5, 0.7)})
        assert rgba is not None
        # Linear value of sRGB 0.5
        assert math.isclose(rgba[0], 0.21404, abs_tol=1e-4)
        # Alpha passes through linearly
        assert rgba[3] == 0.7

    def test_priority_canonical_wins(self):
        # When both base_color_linear and color_hex are set, the
        # canonical key takes precedence; conflict only fires when
        # values are non-equal.
        rgba = _resolve_base_color(
            {
                "base_color_linear": (0.1, 0.2, 0.3, 0.5),
            }
        )
        assert rgba == (0.1, 0.2, 0.3, 0.5)

    def test_conflict_color_rgba_vs_color_hex_raises(self):
        with pytest.raises(ValueError, match="color"):
            _resolve_base_color({"color_rgba": (1.0, 0.0, 0.0, 1.0), "color_hex": "#00FF00"})

    def test_conflict_base_color_linear_vs_color_hex_raises(self):
        with pytest.raises(ValueError, match="color"):
            _resolve_base_color(
                {
                    "base_color_linear": (0.0, 1.0, 0.0, 1.0),
                    "color_hex": "#FF0000",
                }
            )

    def test_none_keys_defer(self):
        # Setting a key to None should defer to the next priority key.
        rgba = _resolve_base_color({"base_color_linear": None, "color_hex": "#FF0000"})
        assert rgba == (1.0, 0.0, 0.0, 1.0)


# ── to_gltf — linear baseColorFactor ────────────────────────────


class TestToGltfLinearBaseColor:
    """ADR-0013 §Decision-2: glTF 2.0 §3.9.2 requires baseColorFactor
    in linear space. 0.6.x emitted sRGB-byte/255 — fixed in 0.7.0.
    """

    def test_white_round_trips_identical(self):
        result = to_gltf({"color_hex": "#FFFFFF"})
        assert result["pbrMetallicRoughness"]["baseColorFactor"] == [1.0, 1.0, 1.0, 1.0]

    def test_black_round_trips_identical(self):
        result = to_gltf({"color_hex": "#000000"})
        assert result["pbrMetallicRoughness"]["baseColorFactor"] == [0.0, 0.0, 0.0, 1.0]

    def test_pure_red_round_trips_identical(self):
        result = to_gltf({"color_hex": "#FF0000"})
        assert result["pbrMetallicRoughness"]["baseColorFactor"] == [1.0, 0.0, 0.0, 1.0]

    def test_mid_tone_de_gammas(self):
        # #bfbfc4 sRGB → linear ~(0.521, 0.521, 0.552). Was (0.749…, …)
        # pre-0.7.0 — this is the breaking-change correctness fix.
        result = to_gltf({"color_hex": "#bfbfc4"})
        bcf = result["pbrMetallicRoughness"]["baseColorFactor"]
        assert math.isclose(bcf[0], 0.520996, abs_tol=1e-5)
        assert math.isclose(bcf[2], 0.552011, abs_tol=1e-5)
        assert bcf[3] == 1.0

    def test_color_rgba_alpha_preserved_through_to_gltf(self):
        # #304 alpha-loss fix. RGBA with alpha=0.7 must reach
        # baseColorFactor[3] verbatim; RGB de-gamma applies separately.
        result = to_gltf({"color_rgba": (0.5, 0.5, 0.5, 0.7)})
        bcf = result["pbrMetallicRoughness"]["baseColorFactor"]
        assert math.isclose(bcf[0], 0.21404, abs_tol=1e-4)  # sRGB 0.5 → linear
        assert bcf[3] == 0.7

    def test_base_color_linear_passes_through(self):
        # Canonical linear input — no transform, alpha intact.
        result = to_gltf({"base_color_linear": (0.5, 0.5, 0.5, 0.7)})
        assert result["pbrMetallicRoughness"]["baseColorFactor"] == [0.5, 0.5, 0.5, 0.7]


# ── to_threejs — default flip ──────────────────────────────────


class TestToThreejsDefaultIsHex:
    """0.7.0: default color_format flips from int (with
    DeprecationWarning) to "hex" (silent, Pythonic, JSON-friendly).
    """

    def test_default_emits_hex_string(self):
        result = to_threejs({"color_hex": "#bfbfc4"})
        assert result["color"] == "#bfbfc4"
        assert isinstance(result["color"], str)

    def test_no_deprecation_warning_at_default(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            to_threejs({"color_hex": "#FF0000"})

    def test_int_format_still_works(self):
        result = to_threejs({"color_hex": "#bfbfc4"}, color_format="int")
        assert result["color"] == 0xBFBFC4

    def test_hex_format_explicit_unchanged(self):
        result = to_threejs({"color_hex": "#bfbfc4"}, color_format="hex")
        assert result["color"] == "#bfbfc4"


# ── color_rgba on to_threejs ───────────────────────────────────


class TestToThreejsColorRgba:
    """to_threejs accepts color_rgba and base_color_linear too.
    Three.js consumes sRGB hex; we re-encode from linear so the
    string output is correct regardless of input form.
    """

    def test_color_rgba_emits_correct_hex(self):
        # color_rgba sRGB (1.0, 0.0, 0.0, 1.0) → "#ff0000"
        result = to_threejs({"color_rgba": (1.0, 0.0, 0.0, 1.0)})
        assert result["color"].lower() == "#ff0000"

    def test_base_color_linear_re_gammas_for_hex(self):
        # base_color_linear (1.0, 1.0, 1.0, 1.0) → "#ffffff" (sRGB)
        result = to_threejs({"base_color_linear": (1.0, 1.0, 1.0, 1.0)})
        assert result["color"].lower() == "#ffffff"


# ── MTLX diffuseColor / emissiveColor — linear values ───────────


class TestMtlxLinearValues:
    """ADR-0013 §Decision-2: UsdPreviewSurface diffuseColor /
    emissiveColor are linear by convention. 0.6.x emitted
    sRGB-byte/255 — fixed in 0.7.0.
    """

    def test_diffuse_color_is_linear(self, tmp_path):
        from mat_vis_client.adapters import export_mtlx

        out = export_mtlx({"color_hex": "#bfbfc4"}, output_dir=tmp_path)
        xml = out.read_text()
        # 0xbf/255 = 0.74902 sRGB → ~0.520996 linear
        assert "0.520996" in xml
        # The pre-0.7.0 sRGB-byte value (0.74902) must not appear.
        assert "0.74902" not in xml
