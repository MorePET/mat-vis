"""Red test for mat-vis#330: materials_named() output ergonomics.

mat-vis#311 sub-bullet "Consistently support material names":
``client.materials("ambientcg", "1k")`` returns IDs (UUIDs for gpuopen,
slugs for ambientcg/polyhaven). The catalog already carries human-readable
names in ``entry.mat_vis.name``; ``materials()`` just doesn't surface them.

This test fails red because the proposed ``materials_named()`` accessor
doesn't exist on ``MatVisClient``. xfail-strict forces removing the
marker as a mechanical step in the fix PR — guards against the
symptom-vs-spec failure mode that bit #287/#288.

See https://github.com/MorePET/mat-vis/issues/330 for the proposed shape:
``MatVisClient.materials_named(source, tier) -> dict[str, str]`` mapping
id → display name pulled from ``entry["mat_vis"]["name"]``.
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(
    strict=True,
    reason="mat-vis#330: materials_named() accessor not implemented",
)
def test_materials_named_method_exists() -> None:
    """``MatVisClient`` must expose a ``materials_named()`` method that
    returns ``{id: display_name}`` for the given (source, tier).

    The full behavioural test lives in the fix PR (it needs a stubbed
    HF tree to assert returned values). This test is the forcing
    function: the API surface must exist before the fix can claim closed.

    Repro at https://github.com/MorePET/mat-vis/issues/311 —
    "Consistently support material names" section.
    """
    from mat_vis_client import MatVisClient

    assert hasattr(MatVisClient, "materials_named"), (
        "MatVisClient.materials_named is missing — see mat-vis#330. "
        "mat-vis#330 wants {id: display_name} dict alongside the existing "
        "materials() method so consumers can render material names in UI "
        "without a second round-trip through the catalog."
    )
