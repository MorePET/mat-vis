"""Canonical declaration of (source × tier) cells per release line.

Per mat-vis#306: the (source × tier) cells that constitute a release
line live HERE, in a single Python module — not scattered across
``bake.yml`` runtime expansions, dispatcher heads, or shell scripts.

Why a Python module (vs TOML / pyproject stanza) — picked from the
spike's three options:

- **Drift safety:** at import time we cross-check every declared
  source against ``mat_vis_baker.sources.KNOWN_SOURCES`` and fail
  fast if the matrix references a source that doesn't exist as an
  importable module. TOML can't do this without a separate validator
  step.
- **Type-checked, testable, refactor-friendly.** ``mypy`` /
  ``ruff`` see the data; tests just import + assert.
- **Consumers stay honest.** The Dagger ``release_matrix(line)``
  function reads from this module; clients that want the canonical
  cells for a release line ``import mat_vis_baker.release_matrix``
  rather than parsing a workflow YAML.

Schema:

    Cell(source, tier)   — one (source × tier) bake target.
    Release(line, cells) — the full set of cells for a release line.

The release line is a CalVer prefix (e.g. ``"v2026.04"``) and
groups patch-level cuts (``v2026.04.0``, ``.1``, ``.2``, ``.3``,
``v2026.04.X``) — the cell shape is stable across patches by
design. A new line (``v2026.05``) gets its own ``Release`` entry
when its cell shape diverges (e.g. adding ``polyhaven 2k``).

Filtering (for spot-tests):

    filter_cells(release.cells, source="gpuopen")
    filter_cells(release.cells, tier="1k")
    filter_cells(release.cells, source="polyhaven", tier="1k")
"""

from __future__ import annotations

from dataclasses import dataclass

from mat_vis_baker._artifact import ArtifactID
from mat_vis_baker.common import VALID_TIERS
from mat_vis_baker.sources import KNOWN_SOURCES, SCALAR_SOURCES, TEXTURED_SOURCES

# Tier accepted as the "no-texture" sentinel for scalar-only sources.
SCALAR_TIER: str = "scalar"

# All tier strings the matrix may reference. Pinned to the existing
# baker tier vocabulary plus the scalar sentinel — a typo here will
# trip the import-time validator below.
_VALID_CELL_TIERS: frozenset[str] = frozenset(VALID_TIERS) | {SCALAR_TIER}


@dataclass(frozen=True, slots=True)
class Cell:
    """One bake-phase cell — produces an artifact by fetching from upstream.

    Equality + hashing are structural — two cells with the same
    ``(source, tier)`` are the same cell. That makes deduping trivial
    and lets cells live in sets / be dict keys.

    mat-vis#349: bake cells have **no** ``inputs`` (they fetch from
    upstream APIs, not from other cells in this release). The
    ``produces`` property exposes the artifact identity for the DAG
    composer (``release_registry.release_dag()``) — same shape derive
    and ktx2 cells use, so the v2 DAG migration is mechanical.
    """

    source: str
    tier: str

    @property
    def produces(self) -> ArtifactID:
        """The artifact this cell produces. mat-vis#349 DAG-shape."""
        return ArtifactID(source=self.source, tier=self.tier)

    @property
    def inputs(self) -> tuple[ArtifactID, ...]:
        """Bake cells take no in-release inputs — empty tuple. Same
        shape derive/ktx2 cells use so DAG composition is uniform."""
        return ()


@dataclass(frozen=True, slots=True)
class Release:
    """The full set of cells that constitute a release line.

    ``cells`` is a tuple (not list) so the declaration is immutable
    after the dataclass is constructed — preventing accidental mutation
    by a consumer.
    """

    line: str
    cells: tuple[Cell, ...]


# Canonical declarations. ADD a new line as a new key; do NOT mutate
# an existing line's cell shape after the first cut on it has shipped
# (the cell shape is the contract that #295 content-drift gate
# verifies against).
_RELEASES: dict[str, Release] = {
    "v2026.04": Release(
        line="v2026.04",
        cells=(
            Cell("ambientcg", "1k"),
            Cell("polyhaven", "1k"),
            Cell("gpuopen", "1k"),
            Cell("physicallybased", SCALAR_TIER),
        ),
    ),
}


def _validate_release(release: Release) -> None:
    """Fail fast if a declared cell references an unknown source/tier
    or a source/tier mismatch (textured-source asking for ``scalar``,
    or scalar-source asking for ``1k``)."""
    seen: set[Cell] = set()
    for cell in release.cells:
        if cell in seen:
            raise ValueError(f"release {release.line!r}: duplicate cell {cell.source}×{cell.tier}")
        seen.add(cell)

        if cell.source not in KNOWN_SOURCES:
            raise ValueError(
                f"release {release.line!r}: cell references unknown source "
                f"{cell.source!r} (known: {sorted(KNOWN_SOURCES)})"
            )
        if cell.tier not in _VALID_CELL_TIERS:
            raise ValueError(
                f"release {release.line!r}: cell {cell.source!r} references "
                f"unknown tier {cell.tier!r} (valid: {sorted(_VALID_CELL_TIERS)})"
            )

        # Source/tier compatibility — the structural drift safety the
        # spike pitfalls call out.
        if cell.source in TEXTURED_SOURCES and cell.tier == SCALAR_TIER:
            raise ValueError(
                f"release {release.line!r}: textured source {cell.source!r} cannot "
                f"target tier {SCALAR_TIER!r}"
            )
        if cell.source in SCALAR_SOURCES and cell.tier != SCALAR_TIER:
            raise ValueError(
                f"release {release.line!r}: scalar source {cell.source!r} must "
                f"target tier {SCALAR_TIER!r}, not {cell.tier!r}"
            )


# Validate at module import — a typo in _RELEASES surfaces immediately
# rather than at next bake dispatch.
for _r in _RELEASES.values():
    _validate_release(_r)


def known_lines() -> tuple[str, ...]:
    """Names of all declared release lines, in declaration order."""
    return tuple(_RELEASES.keys())


def get_release(line: str) -> Release:
    """Return the canonical ``Release`` for a release line.

    Raises ``KeyError`` with a helpful message if the line is unknown.
    """
    if line not in _RELEASES:
        raise KeyError(f"unknown release line {line!r}; known lines: {known_lines()}")
    return _RELEASES[line]


def filter_cells(
    cells: tuple[Cell, ...],
    *,
    source: str = "",
    tier: str = "",
) -> tuple[Cell, ...]:
    """Filter cells by source and/or tier (empty string = no filter).

    Returns a tuple in the same order as the input. An empty result is
    not an error — the caller decides what "no cells matched my filter"
    means (spot-test that should target everything? typo?).
    """
    out = cells
    if source:
        out = tuple(c for c in out if c.source == source)
    if tier:
        out = tuple(c for c in out if c.tier == tier)
    return out
