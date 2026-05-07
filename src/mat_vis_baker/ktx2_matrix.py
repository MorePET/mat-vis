"""Canonical declaration of ktx2 (transcode) cells per release line.

Per mat-vis#349 (B with DAG-cheap-later structure): the ktx2 phase
declares cells that produce a ``ktx2-<tier>`` artifact by toktx-
transcoding an existing PNG tier on HF — locally, no upstream fetch.
The peer to ``release_matrix.py`` (bake) and ``derive_matrix.py``
(resize); ``release_registry.release_dag()`` composes all three.

Each ``Ktx2Cell`` carries:

- ``produces``: the ``ArtifactID`` (with tier prefix ``ktx2-``)
- ``inputs``: a tuple with one source-tier PNG ``ArtifactID``

Production today only ships ``ktx2-1k`` and ``ktx2-512`` per textured
source. Adding ``ktx2-256`` or ``ktx2-128`` is a one-liner here when
mobile clients ask for it.

Validation: cross-phase invariants (the input PNG must be produced by
SOME cell in the same release — bake or derive) are enforced by
``release_registry.release_dag()``. Per-cell sanity is enforced at
import time below.
"""

from __future__ import annotations

from dataclasses import dataclass

from mat_vis_baker._artifact import ArtifactID
from mat_vis_baker.common import VALID_TIERS
from mat_vis_baker.sources import KNOWN_SOURCES, SCALAR_SOURCES, TEXTURED_SOURCES

# ktx2 tier shape: "ktx2-<png_tier>". The validator below enforces
# that the suffix matches a real PNG tier in VALID_TIERS.
KTX2_TIER_PREFIX = "ktx2-"


def _ktx2_tier_for(png_tier: str) -> str:
    """Map a PNG tier to its ktx2 tier name (e.g. ``"1k"`` → ``"ktx2-1k"``)."""
    return f"{KTX2_TIER_PREFIX}{png_tier}"


@dataclass(frozen=True, slots=True)
class Ktx2Cell:
    """One ktx2-transcode cell.

    Equality + hashing structural by ``produces`` (one cell per ktx2
    artifact in a release; cross-phase validator enforces uniqueness)."""

    produces: ArtifactID
    inputs: tuple[ArtifactID, ...]


@dataclass(frozen=True, slots=True)
class Release:
    """The full set of ktx2 cells that constitute a release line."""

    line: str
    cells: tuple[Ktx2Cell, ...]


def _ktx2_for_source(source: str, png_source_tiers: tuple[str, ...]) -> tuple[Ktx2Cell, ...]:
    """Helper: emit one Ktx2Cell per declared source PNG tier."""
    return tuple(
        Ktx2Cell(
            produces=ArtifactID(source=source, tier=_ktx2_tier_for(t)),
            inputs=(ArtifactID(source=source, tier=t),),
        )
        for t in png_source_tiers
    )


# Canonical declarations.
_RELEASES: dict[str, Release] = {
    "v2026.04": Release(
        line="v2026.04",
        cells=(
            *_ktx2_for_source("ambientcg", png_source_tiers=("1k", "512")),
            *_ktx2_for_source("polyhaven", png_source_tiers=("1k", "512")),
            *_ktx2_for_source("gpuopen", png_source_tiers=("1k", "512")),
            # physicallybased — scalar-only, no PNGs to transcode.
        ),
    ),
}


def _validate_release(release: Release) -> None:
    """Per-cell sanity. Cross-phase invariants live in
    ``release_registry.release_dag()``."""
    seen: set[ArtifactID] = set()
    for cell in release.cells:
        if cell.produces in seen:
            raise ValueError(f"release {release.line!r}: duplicate ktx2 cell {cell.produces}")
        seen.add(cell.produces)

        a = cell.produces
        if a.source not in KNOWN_SOURCES:
            raise ValueError(
                f"release {release.line!r}: ktx2 cell {a} references unknown "
                f"source (known: {sorted(KNOWN_SOURCES)})"
            )
        if a.source in SCALAR_SOURCES:
            raise ValueError(
                f"release {release.line!r}: ktx2 cell {a} — scalar-only "
                f"source {a.source!r} has no PNGs to transcode"
            )
        if a.source not in TEXTURED_SOURCES:
            raise ValueError(
                f"release {release.line!r}: ktx2 cell {a} — source not in TEXTURED_SOURCES"
            )

        # ktx2 tier shape
        if not a.tier.startswith(KTX2_TIER_PREFIX):
            raise ValueError(
                f"release {release.line!r}: ktx2 cell {a} — tier must start "
                f"with {KTX2_TIER_PREFIX!r}"
            )
        png_tier = a.tier[len(KTX2_TIER_PREFIX) :]  # noqa: E203
        if png_tier not in VALID_TIERS:
            raise ValueError(
                f"release {release.line!r}: ktx2 cell {a} — png suffix "
                f"{png_tier!r} not in VALID_TIERS ({sorted(VALID_TIERS)})"
            )

        # inputs sanity
        if len(cell.inputs) != 1:
            raise ValueError(
                f"release {release.line!r}: ktx2 cell {a} must have exactly "
                f"one input (the source PNG tier); got {len(cell.inputs)}"
            )
        inp = cell.inputs[0]
        if inp.source != a.source:
            raise ValueError(
                f"release {release.line!r}: ktx2 cell {a} reads from {inp} — "
                "must transcode within the same source"
            )
        if inp.tier != png_tier:
            raise ValueError(
                f"release {release.line!r}: ktx2 cell {a} input tier "
                f"{inp.tier!r} doesn't match the ktx2 suffix {png_tier!r}"
            )


# Validate at module import.
for _r in _RELEASES.values():
    _validate_release(_r)


def known_lines() -> tuple[str, ...]:
    return tuple(_RELEASES.keys())


def get_release(line: str) -> Release:
    """Return the canonical ktx2 ``Release`` for a release line."""
    if line not in _RELEASES:
        raise KeyError(f"unknown release line {line!r}; known lines: {known_lines()}")
    return _RELEASES[line]


def filter_cells(
    cells: tuple[Ktx2Cell, ...],
    *,
    source: str = "",
    tier: str = "",
) -> tuple[Ktx2Cell, ...]:
    """Filter ktx2 cells by produces.source / produces.tier."""
    out = cells
    if source:
        out = tuple(c for c in out if c.produces.source == source)
    if tier:
        out = tuple(c for c in out if c.produces.tier == tier)
    return out
