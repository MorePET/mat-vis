"""Unit tests for ``apply_pbr_neutral_multiplier_conventions``.

Exercises the helper directly with every (field × texture × authored
scalar) combination so a future change to the convention surface is
caught immediately, independent of any individual fetcher.

Convention contract (mat-vis#290 follow-up):
- Iff ``pbr.<field>`` is None AND the matching channel is in the
  texture set, write the glTF-MR neutral multiplier.
- Authored (non-None) values are NEVER overridden.
- Texture set with NO matching channel is a no-op.
- Channels covered: ``color`` → ``color_rgb=[1,1,1]``,
  ``metalness`` → ``metalness=1.0``, ``roughness`` → ``roughness=1.0``.
"""

from __future__ import annotations

from pathlib import Path

from mat_vis_baker.common import PBRBlock, apply_pbr_neutral_multiplier_conventions


def _textures(*channels: str) -> dict[str, Path]:
    """Build a fake texture-paths dict — values irrelevant, keys are."""
    return {ch: Path(f"/tmp/{ch}.png") for ch in channels}


# ── per-channel coverage ───────────────────────────────────────


def test_color_neutralized_when_color_texture_present() -> None:
    pbr = PBRBlock()
    apply_pbr_neutral_multiplier_conventions(pbr, _textures("color"))
    assert pbr.color_rgb == [1.0, 1.0, 1.0]


def test_metalness_neutralized_when_metalness_texture_present() -> None:
    pbr = PBRBlock()
    apply_pbr_neutral_multiplier_conventions(pbr, _textures("metalness"))
    assert pbr.metalness == 1.0


def test_roughness_neutralized_when_roughness_texture_present() -> None:
    """Symmetric third PBR slot — query correctness for the substrate."""
    pbr = PBRBlock()
    apply_pbr_neutral_multiplier_conventions(pbr, _textures("roughness"))
    assert pbr.roughness == 1.0


def test_all_three_channels_neutralized_simultaneously() -> None:
    pbr = PBRBlock()
    apply_pbr_neutral_multiplier_conventions(
        pbr,
        _textures("color", "metalness", "roughness"),
    )
    assert pbr.color_rgb == [1.0, 1.0, 1.0]
    assert pbr.metalness == 1.0
    assert pbr.roughness == 1.0


# ── no-op cases ────────────────────────────────────────────────


def test_empty_textures_is_noop() -> None:
    pbr = PBRBlock()
    apply_pbr_neutral_multiplier_conventions(pbr, {})
    assert pbr.color_rgb is None
    assert pbr.metalness is None
    assert pbr.roughness is None


def test_unrelated_textures_is_noop() -> None:
    """Normal/AO/displacement/emission don't gate any PBR scalar."""
    pbr = PBRBlock()
    apply_pbr_neutral_multiplier_conventions(
        pbr,
        _textures("normal", "ao", "displacement", "emission"),
    )
    assert pbr.color_rgb is None
    assert pbr.metalness is None
    assert pbr.roughness is None


# ── authored values are preserved ──────────────────────────────


def test_authored_color_preserved() -> None:
    pbr = PBRBlock(color_rgb=[0.5, 0.5, 0.5])
    apply_pbr_neutral_multiplier_conventions(pbr, _textures("color"))
    assert pbr.color_rgb == [0.5, 0.5, 0.5]


def test_authored_metalness_preserved() -> None:
    pbr = PBRBlock(metalness=0.3)
    apply_pbr_neutral_multiplier_conventions(pbr, _textures("metalness"))
    assert pbr.metalness == 0.3


def test_authored_roughness_preserved() -> None:
    pbr = PBRBlock(roughness=0.4)
    apply_pbr_neutral_multiplier_conventions(pbr, _textures("roughness"))
    assert pbr.roughness == 0.4


def test_zero_authored_metalness_preserved() -> None:
    """Authored 0.0 (dielectric) is NOT confused with None — must stay 0.0
    even when a metalness texture is in the set. Truthiness traps would
    overwrite this; the convention must use ``is None`` strictly.
    """
    pbr = PBRBlock(metalness=0.0)
    apply_pbr_neutral_multiplier_conventions(pbr, _textures("metalness"))
    assert pbr.metalness == 0.0


def test_partial_authored_partial_neutralized() -> None:
    """Authored color + texture-bound metalness → color preserved,
    metalness filled."""
    pbr = PBRBlock(color_rgb=[0.7, 0.2, 0.1])
    apply_pbr_neutral_multiplier_conventions(pbr, _textures("color", "metalness"))
    assert pbr.color_rgb == [0.7, 0.2, 0.1]
    assert pbr.metalness == 1.0


# ── return value ───────────────────────────────────────────────


def test_returns_same_pbr_for_chaining() -> None:
    """Helper returns the input PBRBlock to enable chained construction."""
    pbr = PBRBlock()
    result = apply_pbr_neutral_multiplier_conventions(pbr, _textures("color"))
    assert result is pbr


# ── adapter sync regression (audit-1 finding) ──────────────────


def test_synthesizer_skips_scalar_input_when_metalness_texture_bound() -> None:
    """Regression (audit-1 finding): when the baker injects
    ``pbr.metalness=1.0`` AND a metalness texture exists, the .mtlx
    synthesizer must NOT emit a *scalar* ``<input ... value="..."/>``
    for that channel — it should emit only the nodegraph reference so
    the texture is the sole driver.

    Pins ``adapters._build_mtlx_tree`` behavior so a future change can't
    silently regress the baker→adapter handshake.
    """
    from mat_vis_client.adapters import _build_mtlx_tree, _mtlx_tree_to_string

    scalars = {"metalness": 1.0, "roughness": 1.0}
    tex_filenames = {"metalness": "mat_metalness.png", "roughness": "mat_roughness.png"}

    root = _build_mtlx_tree(scalars, tex_filenames, "mat")
    xml = _mtlx_tree_to_string(root)

    # No scalar value attribute on the metallic / roughness inputs —
    # the nodegraph reference is the only contributor.
    assert 'name="metallic" type="float" value="' not in xml
    assert 'name="roughness" type="float" value="' not in xml
    # Sanity: the nodegraph references DO exist (texture-driven).
    assert 'name="metallic" type="float" nodegraph="mat_textures"' in xml
    assert 'name="roughness" type="float" nodegraph="mat_textures"' in xml


def test_synthesizer_emits_scalar_when_no_metalness_texture() -> None:
    """Companion: with metalness scalar set but NO metalness texture,
    the synthesizer DOES emit the scalar shader input. This is the
    other half of the handshake — clears the regression test's claim."""
    from mat_vis_client.adapters import _build_mtlx_tree, _mtlx_tree_to_string

    scalars = {"metalness": 1.0, "roughness": 0.5}
    tex_filenames: dict[str, str] = {}  # no textures at all

    root = _build_mtlx_tree(scalars, tex_filenames, "mat")
    xml = _mtlx_tree_to_string(root)

    assert 'name="metallic" type="float" value="1.0"' in xml
    assert 'name="roughness" type="float" value="0.5"' in xml
