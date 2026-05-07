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


def _apply_filter_ids(
    items: list,
    filter_ids: list[str] | None,
    *,
    key: str | None,
    source: str,
) -> list:
    """Restrict ``items`` to those whose id matches ``filter_ids`` (#342).

    Per-source spot-test affordance. Applied BEFORE ``offset`` / ``limit``
    so the workflow input is a semantic filter, not a positional one.

    ``key`` selects the dict field on each item that holds the id (e.g.
    ``"id"`` for gpuopen UUIDs, ``"assetId"`` for ambientcg slugs).
    Pass ``key=None`` when ``items`` is already a list of id strings
    (polyhaven slugs).

    Empty list / None → no filtering. Non-empty list matching zero
    items raises ``ValueError`` with the unmatched ids so the caller
    fails loud rather than producing an empty bake by mistake.
    """
    if not filter_ids:
        return items
    allowed = {fid.strip() for fid in filter_ids if fid and fid.strip()}
    if not allowed:
        return items

    def _id_of(item) -> str:
        return item if key is None else item.get(key, "")

    matched = [item for item in items if _id_of(item) in allowed]
    found = {_id_of(item) for item in matched}
    missing = sorted(allowed - found)
    if missing:
        raise ValueError(
            f"{source}: filter_ids matched no materials for ids "
            f"{missing!r} (filter requested {sorted(allowed)!r})"
        )
    return matched
