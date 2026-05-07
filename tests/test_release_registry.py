"""Tests for mat_vis_baker.release_registry (mat-vis#349).

Cross-phase composer + DAG validator. Covers:

- ``release_dag`` happy path: bake + derive + ktx2 cells compose
  cleanly for v2026.04 (the canonical declaration shipped today).
- Cell counts per phase for v2026.04 (acceptance criterion in #349).
- ``Derivation`` stable sort order.
- Cross-phase invariants — synthesized minimal release lines exercise
  each failure mode the validator should catch.
"""

from __future__ import annotations

import pytest

from mat_vis_baker._artifact import ArtifactID
from mat_vis_baker.release_registry import (
    Derivation,
    _to_derivations,
    _validate_dag,
    known_lines,
    release_dag,
)


# ── happy path: v2026.04 canonical declaration ───────────────────


def test_v2026_04_dag_composes():
    dag = release_dag("v2026.04")
    assert dag.line == "v2026.04"
    # 4 + 9 + 6 = 19 (mat-vis#349 acceptance)
    assert len(dag.derivations) == 19


def test_v2026_04_phase_counts():
    """The exact acceptance numbers from mat-vis#349."""
    dag = release_dag("v2026.04")
    assert len(dag.cells_for_phase("bake")) == 4
    assert len(dag.cells_for_phase("derive")) == 9
    assert len(dag.cells_for_phase("ktx2")) == 6


def test_v2026_04_every_textured_source_has_full_derive_chain():
    """gpuopen / ambientcg / polyhaven each declare 1k → {512, 256, 128}."""
    dag = release_dag("v2026.04")
    for source in ("gpuopen", "ambientcg", "polyhaven"):
        derive_tiers = {
            d.produces.tier for d in dag.cells_for_phase("derive") if d.produces.source == source
        }
        assert derive_tiers == {"128", "256", "512"}, (
            f"{source} derive tiers expected {{128,256,512}}, got {derive_tiers}"
        )


def test_v2026_04_every_textured_source_has_ktx2_set():
    dag = release_dag("v2026.04")
    for source in ("gpuopen", "ambientcg", "polyhaven"):
        ktx2_tiers = {
            d.produces.tier for d in dag.cells_for_phase("ktx2") if d.produces.source == source
        }
        assert ktx2_tiers == {"ktx2-1k", "ktx2-512"}


def test_v2026_04_physicallybased_only_in_bake():
    """Scalar-only source has bake cell, NO derive or ktx2."""
    dag = release_dag("v2026.04")
    pb_derivations = [d for d in dag.derivations if d.produces.source == "physicallybased"]
    assert len(pb_derivations) == 1
    assert pb_derivations[0].phase == "bake"
    assert pb_derivations[0].produces.tier == "scalar"


def test_v2026_04_dag_by_artifact_index():
    """O(1) lookup from ArtifactID → Derivation."""
    dag = release_dag("v2026.04")
    a = ArtifactID(source="gpuopen", tier="512")
    d = dag.by_artifact[a]
    assert d.phase == "derive"
    assert d.inputs == (ArtifactID(source="gpuopen", tier="1k"),)


def test_v2026_04_dag_all_artifacts_unique_and_sorted():
    dag = release_dag("v2026.04")
    arts = dag.all_artifacts()
    assert len(arts) == len(set(arts))  # unique
    assert list(arts) == sorted(arts)  # ordered


# ── known_lines / get_release plumbing ────────────────────────────


def test_known_lines_includes_v2026_04():
    assert "v2026.04" in known_lines()


def test_release_dag_unknown_line_raises_keyerror():
    with pytest.raises(KeyError, match="unknown release line"):
        release_dag("v9999.99")


# ── _validate_dag (synthetic minimal cases) ───────────────────────


def _bake_derivation(s: str, t: str) -> Derivation:
    return Derivation(phase="bake", produces=ArtifactID(s, t), inputs=())


def _derive_derivation(s: str, target_t: str, source_t: str) -> Derivation:
    return Derivation(
        phase="derive",
        produces=ArtifactID(s, target_t),
        inputs=(ArtifactID(s, source_t),),
    )


def _ktx2_derivation(s: str, png_t: str) -> Derivation:
    return Derivation(
        phase="ktx2",
        produces=ArtifactID(s, f"ktx2-{png_t}"),
        inputs=(ArtifactID(s, png_t),),
    )


def test_validate_dag_clean_case():
    derivations = (
        _bake_derivation("foo", "1k"),
        _derive_derivation("foo", "512", "1k"),
        _ktx2_derivation("foo", "1k"),
    )
    by_artifact = _validate_dag("test", derivations)
    assert len(by_artifact) == 3


def test_validate_dag_rejects_duplicate_produces():
    """Two cells producing the same artifact (e.g. one in bake + one
    in derive) is the cross-phase invariant violation the cell-local
    validator can't see."""
    derivations = (
        _bake_derivation("foo", "1k"),
        _derive_derivation("foo", "1k", "2k"),  # also produces foo:1k
    )
    with pytest.raises(ValueError, match="produced by both"):
        _validate_dag("test", derivations)


def test_validate_dag_rejects_dangling_input():
    """ktx2 cell reads from foo:1k but no cell produces foo:1k."""
    derivations = (
        _ktx2_derivation("foo", "1k"),  # reads foo:1k → not produced
    )
    with pytest.raises(ValueError, match="reads input"):
        _validate_dag("test", derivations)


def test_validate_dag_rejects_cycle():
    """Synthetic cycle: A reads B, B reads A. Real matrices today are
    2-deep so this is artificial — but the v2 expansion will have
    chained derives, locking the invariant matters."""
    derivations = (
        Derivation(
            phase="derive",
            produces=ArtifactID("foo", "256"),
            inputs=(ArtifactID("foo", "512"),),
        ),
        Derivation(
            phase="derive",
            produces=ArtifactID("foo", "512"),
            inputs=(ArtifactID("foo", "256"),),
        ),
    )
    with pytest.raises(ValueError, match="cycle"):
        _validate_dag("test", derivations)


def test_validate_dag_rejects_self_loop():
    derivations = (
        Derivation(
            phase="derive",
            produces=ArtifactID("foo", "512"),
            inputs=(ArtifactID("foo", "512"),),
        ),
    )
    with pytest.raises(ValueError, match="cycle"):
        _validate_dag("test", derivations)


# ── _to_derivations stable ordering ──────────────────────────────


def test_to_derivations_sorted_by_phase_then_artifact():
    """Stable JSON output requires deterministic order: phase
    alphabetical (bake < derive < ktx2), then by produces."""
    from mat_vis_baker.derive_matrix import DeriveCell
    from mat_vis_baker.ktx2_matrix import Ktx2Cell
    from mat_vis_baker.release_matrix import Cell

    bake = (Cell("zeta", "1k"), Cell("alpha", "1k"))
    derive = (
        DeriveCell(
            produces=ArtifactID("alpha", "512"),
            inputs=(ArtifactID("alpha", "1k"),),
        ),
    )
    ktx2 = (
        Ktx2Cell(
            produces=ArtifactID("alpha", "ktx2-1k"),
            inputs=(ArtifactID("alpha", "1k"),),
        ),
    )
    out = _to_derivations(bake, derive, ktx2)

    # Phase order: bake < derive < ktx2 alphabetically
    phases = [d.phase for d in out]
    assert phases == sorted(phases)

    # Within each phase, sorted by produces
    bakes = [d for d in out if d.phase == "bake"]
    assert [d.produces.source for d in bakes] == ["alpha", "zeta"]


# ── ReleaseDAG type integrity ────────────────────────────────────


def test_release_dag_carries_per_phase_tuples_for_callers():
    """Workflow plan jobs filter to one phase; they want the original
    per-phase dataclass not the union Derivation type."""
    dag = release_dag("v2026.04")
    # Each is a tuple of the right per-phase dataclass.
    assert isinstance(dag.bake_cells, tuple)
    assert isinstance(dag.derive_cells, tuple)
    assert isinstance(dag.ktx2_cells, tuple)
    # And the count matches the unified view.
    assert len(dag.bake_cells) + len(dag.derive_cells) + len(dag.ktx2_cells) == len(dag.derivations)
