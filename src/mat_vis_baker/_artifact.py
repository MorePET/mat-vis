"""Shared ``ArtifactID`` — stable identity for a baked / derived / ktx2'd
substrate artifact.

Per mat-vis#349: each release-matrix cell ``produces`` an
``ArtifactID``, and derive / ktx2 cells declare ``inputs`` (a tuple of
ArtifactIDs they read from). Today the per-phase modules
(``release_matrix.py`` for bake, ``derive_matrix.py``, ``ktx2_matrix.py``)
each declare cells using this type; ``release_registry.release_dag()``
composes them into a single DAG view that validates topology
(no duplicate produces, every input is produced by some cell, no
cycles).

Why a shared identity type instead of per-phase ``(source, tier)``
tuples: it lets the DAG migration (#349 v2 / Option D) be mechanical —
``ArtifactID`` becomes the DAG node ID with zero schema change.

Example::

    aluminum_1k = ArtifactID(source="gpuopen", tier="1k")
    aluminum_512 = ArtifactID(source="gpuopen", tier="512")
    # derive cell: produces 512 from 1k
    DeriveCell(produces=aluminum_512, inputs=(aluminum_1k,))
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class ArtifactID:
    """Stable identity for a baked / derived substrate artifact.

    ``source`` is one of ``mat_vis_baker.sources.KNOWN_SOURCES``.
    ``tier`` is a member of ``mat_vis_baker.common.VALID_TIERS`` plus
    the ``"scalar"`` sentinel for scalar-only sources, plus the
    ``"ktx2-<tier>"`` shape for ktx2 artifacts (e.g. ``"ktx2-1k"``).

    The class is frozen + slotted so cells can be set / dict-keyed and
    the dataclass overhead is minimal. ``order=True`` so DAG views can
    deterministically sort cells by `(source, tier)` for stable JSON
    output.
    """

    source: str
    tier: str

    def __str__(self) -> str:
        # Compact form for log + error messages: "gpuopen:1k"
        return f"{self.source}:{self.tier}"
