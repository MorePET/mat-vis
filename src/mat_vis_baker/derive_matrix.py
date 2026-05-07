"""Canonical declaration of derive (PNG resize) cells per release line.

Per mat-vis#349 (B with DAG-cheap-later structure): the derive phase
declares cells that produce a smaller PNG tier from an existing PNG
tier — locally via PIL/imageio resize, no upstream fetch. The peer
to ``release_matrix.py`` (bake) and ``ktx2_matrix.py`` (ktx2 transcode);
``release_registry.release_dag()`` composes all three into a single
DAG view.

Each ``DeriveCell`` carries:

- ``produces``: the ``ArtifactID`` the cell writes to HF
- ``inputs``: a tuple of source ``ArtifactID``s the resize reads from

Validation: cross-phase invariants (every ``input`` is ``produces`` by
some cell in the same release line, no cycles) are enforced by
``release_registry.release_dag()`` — not duplicated here. Per-cell
sanity (known source, known tier, no scalar sources) is enforced at
import time below; mirrors ``release_matrix.py``'s validator.

Why ``inputs`` is a tuple and not a single ``source_tier``: the v2 DAG
migration (#349 P2 / Option D) uses multi-input derivations
(e.g. content-addressed substrate where a derived artifact reads
multiple parents). Today every derive cell has exactly one input;
the tuple shape costs nothing and makes the DAG migration mechanical.

Schema:

    DeriveCell(produces, inputs)  — one PNG-resize derivation
    Release(line, cells)          — the full set of derive cells

The release line shape mirrors the bake matrix; production v2026.04
declares the canonical ``{1k → 512, 1k → 256, 1k → 128}`` chain per
textured source.
"""

from __future__ import annotations

from dataclasses import dataclass

from mat_vis_baker._artifact import ArtifactID
from mat_vis_baker.common import VALID_TIERS
from mat_vis_baker.sources import KNOWN_SOURCES, SCALAR_SOURCES, TEXTURED_SOURCES

# Tier vocabulary that derive cells may reference. Excludes "scalar"
# (scalar-only sources have no PNGs to resize) but includes every PNG
# tier in VALID_TIERS.
_VALID_DERIVE_TIERS: frozenset[str] = frozenset(VALID_TIERS)


@dataclass(frozen=True, slots=True)
class DeriveCell:
    """One PNG-resize derivation cell.

    Equality + hashing are structural by ``produces`` (each artifact
    has exactly one producing cell — enforced by the cross-phase
    validator in ``release_registry``). ``inputs`` may differ across
    valid declarations of the same ``produces`` (e.g. derive 256 from
    1k vs. from 512); the validator picks one canonical declaration.
    """

    produces: ArtifactID
    inputs: tuple[ArtifactID, ...]


@dataclass(frozen=True, slots=True)
class Release:
    """The full set of derive cells that constitute a release line."""

    line: str
    cells: tuple[DeriveCell, ...]


def _derive_chain(
    source: str, source_tier: str, target_tiers: tuple[str, ...]
) -> tuple[DeriveCell, ...]:
    """Helper: generate ``len(target_tiers)`` cells all reading from
    the same ``source_tier``.

    Production today: every derive reads from 1k (the only baked tier
    on textured sources). Future multi-tier sources will use a chain
    pattern (1k → 512 → 256 → 128) — change this helper then.
    """
    src = ArtifactID(source=source, tier=source_tier)
    return tuple(
        DeriveCell(produces=ArtifactID(source=source, tier=tt), inputs=(src,))
        for tt in target_tiers
    )


# Canonical declarations. ADD a new line as a new key; do NOT mutate
# an existing line's cell shape after the first cut on it has shipped.
_RELEASES: dict[str, Release] = {
    "v2026.04": Release(
        line="v2026.04",
        cells=(
            *_derive_chain("ambientcg", source_tier="1k", target_tiers=("512", "256", "128")),
            *_derive_chain("polyhaven", source_tier="1k", target_tiers=("512", "256", "128")),
            *_derive_chain("gpuopen", source_tier="1k", target_tiers=("512", "256", "128")),
            # physicallybased is scalar-only — no derives.
        ),
    ),
}


def _validate_release(release: Release) -> None:
    """Per-cell sanity check. Cross-phase invariants (input must be
    produced by some cell in the line, no cycles) live in
    ``release_registry.release_dag()`` — not duplicated here."""
    seen: set[ArtifactID] = set()
    for cell in release.cells:
        if cell.produces in seen:
            raise ValueError(f"release {release.line!r}: duplicate derive cell {cell.produces}")
        seen.add(cell.produces)

        # produces sanity
        a = cell.produces
        if a.source not in KNOWN_SOURCES:
            raise ValueError(
                f"release {release.line!r}: derive cell {a} references unknown "
                f"source (known: {sorted(KNOWN_SOURCES)})"
            )
        if a.source in SCALAR_SOURCES:
            raise ValueError(
                f"release {release.line!r}: derive cell {a} — scalar-only "
                f"source {a.source!r} has no PNGs to derive"
            )
        if a.source not in TEXTURED_SOURCES:
            raise ValueError(
                f"release {release.line!r}: derive cell {a} — source not in "
                f"TEXTURED_SOURCES (known textured: {sorted(TEXTURED_SOURCES)})"
            )
        if a.tier not in _VALID_DERIVE_TIERS:
            raise ValueError(
                f"release {release.line!r}: derive cell {a} — tier not in "
                f"VALID_TIERS (valid: {sorted(_VALID_DERIVE_TIERS)})"
            )

        # inputs sanity (per-cell only; cross-cell graph-validation in registry)
        if not cell.inputs:
            raise ValueError(
                f"release {release.line!r}: derive cell {a} has empty inputs; "
                "derive cells must declare at least one source artifact"
            )
        for inp in cell.inputs:
            if inp.source != a.source:
                raise ValueError(
                    f"release {release.line!r}: derive cell {a} reads from "
                    f"{inp} — derive cells must read from the same source "
                    "(cross-source derivation is not supported)"
                )
            if inp == a:
                raise ValueError(
                    f"release {release.line!r}: derive cell {a} cannot read "
                    f"from itself (self-loop in the derivation DAG)"
                )


# Validate at module import.
for _r in _RELEASES.values():
    _validate_release(_r)


def known_lines() -> tuple[str, ...]:
    return tuple(_RELEASES.keys())


def get_release(line: str) -> Release:
    """Return the canonical derive ``Release`` for a release line."""
    if line not in _RELEASES:
        raise KeyError(f"unknown release line {line!r}; known lines: {known_lines()}")
    return _RELEASES[line]


def filter_cells(
    cells: tuple[DeriveCell, ...],
    *,
    source: str = "",
    tier: str = "",
) -> tuple[DeriveCell, ...]:
    """Filter derive cells by produces.source / produces.tier."""
    out = cells
    if source:
        out = tuple(c for c in out if c.produces.source == source)
    if tier:
        out = tuple(c for c in out if c.produces.tier == tier)
    return out
