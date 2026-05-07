"""Red test for mat-vis#331: physicallybased catalog ships available_tiers=[].

bernhard-42's mat-vis#311 + #313: ``client.materials("physicallybased", "scalar")``
returns empty even though 86 materials exist in the catalog. Root cause is in
the fetcher (``sources/physicallybased.py:197``) which hardcodes
``available_tiers=[]`` instead of ``available_tiers=["scalar"]``.

This test fails red on the current fetcher; the fix is a one-line change
that flips the literal. xfail-strict forces removing the marker as a
mechanical step in the fix PR — guards against the symptom-vs-spec failure
mode that bit #287/#288 (closing on a different layer than the bug).

See https://github.com/MorePET/mat-vis/issues/331 for the proper-fix
acceptance, and #313 for bernhard's user-visible repro that this unblocks
in combination with mat#222.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mat_vis_baker.sources.physicallybased import fetch


@pytest.mark.xfail(
    strict=True,
    reason="mat-vis#331: physicallybased fetcher hardcodes available_tiers=[]",
)
def test_fetch_populates_available_tiers_with_scalar_sentinel() -> None:
    """Every record from the physicallybased fetcher must declare
    ``available_tiers=["scalar"]`` so ``client.materials()`` filters
    correctly.

    The catalog uses the ``scalar`` sentinel for textureless sources
    (manifest already declares ``tiers.scalar.complete=True`` for
    physicallybased — the fetcher is the only place still emitting
    ``[]``).
    """
    fake_api = [
        {
            "name": "Aluminum",
            "category": "metal",
            "color": [0.9, 0.9, 0.9],
            "ior": 1.39,
            "metalness": 1.0,
            "roughness": 0.0,
            "tags": ["mirror"],
        },
        {
            "name": "Banana",
            "category": "organic",
            "color": [1.0, 0.8, 0.2],
            "ior": 1.45,
            "tags": [],
        },
    ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = fake_api

    with patch("mat_vis_baker.sources.physicallybased.retry_request", return_value=mock_resp):
        records = fetch()

    assert records, "physicallybased fetcher returned no records"
    for rec in records:
        assert rec.available_tiers == ["scalar"], (
            f"physicallybased record {rec.mat_vis.name!r} has "
            f"available_tiers={rec.available_tiers}; expected ['scalar'] "
            '(mat-vis#331 — required so client.materials("physicallybased", '
            '"scalar") returns the entries; cascades to mat-vis#313)'
        )
