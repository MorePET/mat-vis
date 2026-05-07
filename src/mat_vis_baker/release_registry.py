"""Cross-phase release matrix composer + validator (mat-vis#349).

Composes the three peer per-phase modules — ``release_matrix`` (bake),
``derive_matrix``, ``ktx2_matrix`` — into a single DAG view per
release line. Validates topology that no individual peer can check
because it crosses module boundaries:

- No two cells (across any phase) ``produces`` the same ``ArtifactID``.
- Every ``inputs`` reference points at an artifact ``produces`` by some
  cell in the same release line — if ktx2 reads ``gpuopen:1k`` then
  ``gpuopen:1k`` must be either a bake cell or a derive cell in this
  release.
- No cycles in the derivation DAG (today's matrix is 2-deep so this
  is trivially satisfied; the test suite locks the invariant for the
  v2 expansion when chained derives become routine).

This module is the cheap-later property of #349's Option B: today
``release_dag()`` is a convenience that composes the three peer
modules; tomorrow (Option D — DAG migration) it becomes the **primary**
API and the per-phase modules collapse into a single declarations
file. The migration is mechanical because each cell already carries
``produces: ArtifactID`` and ``inputs: tuple[ArtifactID, ...]``; the
phase membership is just metadata.

Public surface:

- :class:`ReleaseDAG` — composed view; carries cell lists per phase
  plus a flat ``derivations`` tuple keyed by ``produces`` for
  graph traversal.
- :func:`release_dag` — fetch + validate; raises ``ValueError`` on
  any cross-phase invariant violation.
- :func:`known_lines` — union of declared lines across the three
  peer modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from mat_vis_baker import derive_matrix, ktx2_matrix, release_matrix
from mat_vis_baker._artifact import ArtifactID

Phase = Literal["bake", "derive", "ktx2"]


@dataclass(frozen=True, slots=True)
class Derivation:
    """A flat, phase-tagged cell view used by :class:`ReleaseDAG` for
    graph traversal. The per-phase dataclasses (``Cell``, ``DeriveCell``,
    ``Ktx2Cell``) keep their phase-specific shape; this is the union
    type that graph algorithms operate on.

    The v2 DAG migration (#349 P2 / Option D) collapses the three
    per-phase modules into a single declarations file using this type
    directly — adding a new phase is "add a Literal value" instead of
    "add a new module."
    """

    phase: Phase
    produces: ArtifactID
    inputs: tuple[ArtifactID, ...]


@dataclass(frozen=True, slots=True)
class ReleaseDAG:
    """Composed view of all phases for a release line.

    Carries:

    - :attr:`bake_cells` / :attr:`derive_cells` / :attr:`ktx2_cells` —
      the original per-phase tuples, for callers that want the per-phase
      schema (workflow plan jobs filter to one phase).
    - :attr:`derivations` — flat tuple of :class:`Derivation` for
      graph traversal. Sorted by ``(phase, produces)`` for stable
      JSON output.
    - :attr:`by_artifact` — dict ``ArtifactID → Derivation`` for O(1)
      input lookup during DAG traversal.

    Invariants (enforced by :func:`release_dag` before construction):

    - Every ``produces`` is unique across all phases.
    - Every ``inputs`` element appears as some other cell's ``produces``.
    - No cycles.
    """

    line: str
    bake_cells: tuple[release_matrix.Cell, ...]
    derive_cells: tuple[derive_matrix.DeriveCell, ...]
    ktx2_cells: tuple[ktx2_matrix.Ktx2Cell, ...]
    derivations: tuple[Derivation, ...]
    by_artifact: dict[ArtifactID, Derivation] = field(default_factory=dict)

    def all_artifacts(self) -> tuple[ArtifactID, ...]:
        """Every artifact this release line produces, sorted."""
        return tuple(sorted(self.by_artifact.keys()))

    def cells_for_phase(self, phase: Phase) -> tuple[Derivation, ...]:
        """Filter derivations by phase. Used by the workflow CLI's
        ``--phase=`` flag."""
        return tuple(d for d in self.derivations if d.phase == phase)


def known_lines() -> tuple[str, ...]:
    """Union of release lines declared across the three peer modules.

    Today every line should appear in all three (or in bake-only +
    scalar-source-only); the validator below catches asymmetric
    declarations as missing-cell errors at composition time.
    """
    seen: set[str] = set()
    for mod in (release_matrix, derive_matrix, ktx2_matrix):
        seen.update(mod.known_lines())
    return tuple(sorted(seen))


def _to_derivations(
    bake_cells: tuple[release_matrix.Cell, ...],
    derive_cells: tuple[derive_matrix.DeriveCell, ...],
    ktx2_cells: tuple[ktx2_matrix.Ktx2Cell, ...],
) -> tuple[Derivation, ...]:
    """Flatten the three per-phase tuples into a single phase-tagged
    sequence. Sort by ``(phase, produces)`` for stable serialization."""
    out: list[Derivation] = []
    for c in bake_cells:
        out.append(Derivation(phase="bake", produces=c.produces, inputs=c.inputs))
    for c in derive_cells:
        out.append(Derivation(phase="derive", produces=c.produces, inputs=c.inputs))
    for c in ktx2_cells:
        out.append(Derivation(phase="ktx2", produces=c.produces, inputs=c.inputs))
    out.sort(key=lambda d: (d.phase, d.produces))
    return tuple(out)


def _validate_dag(line: str, derivations: tuple[Derivation, ...]) -> dict[ArtifactID, Derivation]:
    """Cross-phase topology validator. Returns the by_artifact map
    on success; raises ``ValueError`` with a specific message on any
    violation."""

    by_artifact: dict[ArtifactID, Derivation] = {}
    for d in derivations:
        if d.produces in by_artifact:
            other = by_artifact[d.produces]
            raise ValueError(
                f"release {line!r}: artifact {d.produces} produced by both "
                f"{other.phase} and {d.phase} cells — every artifact must "
                "have exactly one producing cell across all phases."
            )
        by_artifact[d.produces] = d

    # Every input must be produced by some cell in the same line.
    for d in derivations:
        for inp in d.inputs:
            if inp not in by_artifact:
                raise ValueError(
                    f"release {line!r}: {d.phase} cell {d.produces} reads "
                    f"input {inp} that no cell in this release produces. "
                    "Either add a cell that produces it (likely a bake or "
                    "derive cell) or remove the dependency."
                )

    # Cycle check via DFS. Today's matrix is 2-deep (bake → derive,
    # bake → ktx2, derive → ktx2-via-derived) so cycles are trivial,
    # but the v2 expansion will have multi-input chains; lock the
    # invariant now.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[ArtifactID, int] = {a: WHITE for a in by_artifact}

    def dfs(node: ArtifactID, path: list[ArtifactID]) -> None:
        color[node] = GRAY
        path.append(node)
        for nxt in by_artifact[node].inputs:
            if color[nxt] == GRAY:
                cycle = " → ".join(str(x) for x in path[path.index(nxt) :]) + f" → {nxt}"
                raise ValueError(f"release {line!r}: cycle in derivation DAG: {cycle}")
            if color[nxt] == WHITE:
                dfs(nxt, path)
        path.pop()
        color[node] = BLACK

    for a in by_artifact:
        if color[a] == WHITE:
            dfs(a, [])

    return by_artifact


def release_dag(line: str) -> ReleaseDAG:
    """Compose the three peer modules into a validated DAG view.

    Lookup is by ``line`` (e.g. ``"v2026.04"``). If a peer module
    doesn't declare the line, an empty cell tuple is used for that
    phase. If NO peer declares the line, raises ``KeyError``.

    Validation runs before the DAG object is returned; on any
    cross-phase invariant violation a ``ValueError`` with a specific
    error message is raised.
    """
    if line not in known_lines():
        raise KeyError(f"unknown release line {line!r}; known: {known_lines()}")

    # Each peer's get_release raises if it doesn't have the line; we
    # tolerate that (some lines may legitimately have no derive cells,
    # e.g. scalar-only lines). Default to empty.
    try:
        bake_cells = release_matrix.get_release(line).cells
    except KeyError:
        bake_cells = ()
    try:
        derive_cells = derive_matrix.get_release(line).cells
    except KeyError:
        derive_cells = ()
    try:
        ktx2_cells = ktx2_matrix.get_release(line).cells
    except KeyError:
        ktx2_cells = ()

    derivations = _to_derivations(bake_cells, derive_cells, ktx2_cells)
    by_artifact = _validate_dag(line, derivations)

    return ReleaseDAG(
        line=line,
        bake_cells=bake_cells,
        derive_cells=derive_cells,
        ktx2_cells=ktx2_cells,
        derivations=derivations,
        by_artifact=by_artifact,
    )


# Validate every known line at module import — surfaces cross-phase
# misdeclarations the moment the module is imported, rather than at
# the next workflow dispatch.
for _line in known_lines():
    release_dag(_line)
