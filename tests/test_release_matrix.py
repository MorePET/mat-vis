"""Tests for mat_vis_baker.release_matrix (mat-vis#306)."""

from __future__ import annotations

import pytest

from mat_vis_baker.release_matrix import (
    SCALAR_TIER,
    Cell,
    Release,
    _validate_release,
    filter_cells,
    get_release,
    known_lines,
)
from mat_vis_baker.sources import KNOWN_SOURCES, SCALAR_SOURCES, TEXTURED_SOURCES


# ── canonical declarations ────────────────────────────────────────


def test_v2026_04_line_declared():
    """The v2026.04 line must be declared (current production)."""
    assert "v2026.04" in known_lines()


def test_v2026_04_covers_all_known_sources():
    """Every KNOWN_SOURCES entry must appear in the v2026.04 line —
    drift safety: adding a source to the registry without adding it
    to the matrix would silently exclude it from cuts."""
    line = get_release("v2026.04")
    sources_in_line = {c.source for c in line.cells}
    assert sources_in_line == KNOWN_SOURCES, (
        f"v2026.04 cells reference {sources_in_line}, but KNOWN_SOURCES is {KNOWN_SOURCES}"
    )


def test_v2026_04_textured_sources_use_texture_tiers():
    line = get_release("v2026.04")
    for cell in line.cells:
        if cell.source in TEXTURED_SOURCES:
            assert cell.tier != SCALAR_TIER, (
                f"textured source {cell.source!r} must not target {SCALAR_TIER!r}"
            )


def test_v2026_04_scalar_sources_use_scalar_tier():
    line = get_release("v2026.04")
    for cell in line.cells:
        if cell.source in SCALAR_SOURCES:
            assert cell.tier == SCALAR_TIER, (
                f"scalar source {cell.source!r} must target {SCALAR_TIER!r}"
            )


# ── get_release / known_lines ─────────────────────────────────────


def test_get_release_returns_immutable_tuple():
    line = get_release("v2026.04")
    assert isinstance(line.cells, tuple)


def test_get_release_unknown_raises_helpful_keyerror():
    with pytest.raises(KeyError, match="unknown release line"):
        get_release("v9999.99")


def test_known_lines_returns_tuple():
    assert isinstance(known_lines(), tuple)


# ── filter_cells ──────────────────────────────────────────────────


def test_filter_cells_no_filter_returns_all():
    cells = get_release("v2026.04").cells
    assert filter_cells(cells) == cells


def test_filter_cells_by_source():
    cells = get_release("v2026.04").cells
    out = filter_cells(cells, source="gpuopen")
    assert len(out) == 1
    assert out[0].source == "gpuopen"


def test_filter_cells_by_tier():
    cells = get_release("v2026.04").cells
    out = filter_cells(cells, tier=SCALAR_TIER)
    assert len(out) == 1
    assert out[0].source in SCALAR_SOURCES


def test_filter_cells_by_both_source_and_tier():
    cells = get_release("v2026.04").cells
    out = filter_cells(cells, source="ambientcg", tier="1k")
    assert len(out) == 1
    assert out[0] == Cell("ambientcg", "1k")


def test_filter_cells_no_match_is_empty_not_error():
    cells = get_release("v2026.04").cells
    assert filter_cells(cells, source="nonexistent") == ()


def test_filter_cells_preserves_input_order():
    cells = (
        Cell("ambientcg", "1k"),
        Cell("polyhaven", "1k"),
        Cell("gpuopen", "1k"),
    )
    out = filter_cells(cells, tier="1k")
    assert out == cells


# ── _validate_release: drift-safety failure modes ────────────────


def test_validate_rejects_unknown_source():
    bad = Release(line="test", cells=(Cell("nonexistent_source", "1k"),))
    with pytest.raises(ValueError, match="unknown source"):
        _validate_release(bad)


def test_validate_rejects_unknown_tier():
    bad = Release(line="test", cells=(Cell("ambientcg", "9999k"),))
    with pytest.raises(ValueError, match="unknown tier"):
        _validate_release(bad)


def test_validate_rejects_textured_source_with_scalar_tier():
    bad = Release(line="test", cells=(Cell("gpuopen", SCALAR_TIER),))
    with pytest.raises(ValueError, match="textured source.*cannot target"):
        _validate_release(bad)


def test_validate_rejects_scalar_source_with_texture_tier():
    bad = Release(line="test", cells=(Cell("physicallybased", "1k"),))
    with pytest.raises(ValueError, match="scalar source.*must target"):
        _validate_release(bad)


def test_validate_rejects_duplicate_cell():
    bad = Release(
        line="test",
        cells=(Cell("gpuopen", "1k"), Cell("gpuopen", "1k")),
    )
    with pytest.raises(ValueError, match="duplicate cell"):
        _validate_release(bad)


# ── Cell / Release dataclass behavior ────────────────────────────


def test_cell_is_hashable():
    cells: set[Cell] = {Cell("a", "1k"), Cell("a", "1k"), Cell("b", "1k")}
    assert len(cells) == 2  # dedup by structural equality


def test_cell_is_immutable():
    c = Cell("ambientcg", "1k")
    with pytest.raises(Exception):
        c.source = "polyhaven"  # type: ignore[misc]


def test_release_cells_is_tuple_not_list():
    r = get_release("v2026.04")
    assert isinstance(r.cells, tuple)
