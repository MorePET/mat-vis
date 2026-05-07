"""Registry of known material sources.

The single source of truth for "what sources does this baker know about."
Edit ``KNOWN_SOURCES`` when adding a new source module under
``mat_vis_baker.sources.``.

Why a registry rather than ``glob('*.py')``: tests + release-matrix
validation (mat-vis#306) want a stable, importable name they can pin
against. A glob would silently include test fixtures, half-finished
modules, etc.
"""

from __future__ import annotations

KNOWN_SOURCES: frozenset[str] = frozenset(
    {
        "ambientcg",
        "polyhaven",
        "gpuopen",
        "physicallybased",
    }
)

# Sources that produce textured per-tier output. The release matrix
# (mat-vis#306) validates per-cell tier choices against this set.
TEXTURED_SOURCES: frozenset[str] = frozenset({"ambientcg", "polyhaven", "gpuopen"})

# Sources that ship as scalar-only (no per-tier textures).
SCALAR_SOURCES: frozenset[str] = frozenset({"physicallybased"})

assert TEXTURED_SOURCES | SCALAR_SOURCES == KNOWN_SOURCES, (
    "every source must be either TEXTURED_SOURCES or SCALAR_SOURCES"
)
