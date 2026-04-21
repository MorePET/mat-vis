"""Discover-per-fetch regression guard (#179 follow-up).

Every upstream source exposes two levels:

- ``discover(session=...)`` — pages the full upstream catalog. Cheap
  per-call in absolute seconds (~10-30 s) but expensive in API
  traffic (ambientcg: 20 × /full_json at 100/page to cover 1993
  materials; polyhaven: 1 × /assets). Idempotent — the catalog
  snapshot doesn't change within a bake session.

- ``fetch(tier, ..., offset=N, limit=M)`` — returns one paginated
  slice of MaterialRecord objects. Used by ``hf_bake.bake_one`` in
  a batching loop (``batch_size=50``).

The bug caught during the v0.0.4-staging bake on 2026-04-21:
``bake_one`` calls ``fetch()`` once per batch. Until this fix each
``fetch()`` call internally re-invoked ``discover()`` from scratch,
so a 1993-record ambientcg 2k bake paginated the full catalog 40
times — ~600 wasted API calls, ~10 min wall-clock, and upstream
rate-pressure for no semantic gain.

These tests pin the fixed behaviour: discover is called at most
once per process (module-level memoization).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# Fixture cleans module caches between tests so test-order doesn't
# leak a cache from one source's test into another's.
@pytest.fixture(autouse=True)
def _reset_discover_caches():
    from mat_vis_baker.sources import ambientcg, gpuopen, polyhaven

    for mod in (ambientcg, gpuopen, polyhaven):
        reset = getattr(mod, "_reset_discover_cache", None)
        if reset is not None:
            reset()
    yield
    for mod in (ambientcg, gpuopen, polyhaven):
        reset = getattr(mod, "_reset_discover_cache", None)
        if reset is not None:
            reset()


class TestAmbientcgMemoizesDiscover:
    def test_three_fetches_trigger_one_discover(self, tmp_path):
        from mat_vis_baker.sources import ambientcg

        # Return a tiny synthetic catalog — 2 materials, both with no
        # downloads so fetch returns an empty record list fast (we're
        # only checking the discover() call count, not fetch output).
        fake_entries = [
            {"assetId": "M1", "displayName": "M1", "downloadFolders": {}, "tags": []},
            {"assetId": "M2", "displayName": "M2", "downloadFolders": {}, "tags": []},
        ]

        with patch.object(ambientcg, "discover", return_value=fake_entries) as m:
            for offset in (0, 50, 100):
                ambientcg.fetch("1k", tmp_path, limit=50, offset=offset)
        assert m.call_count == 1, (
            f"discover called {m.call_count}× across 3 fetches — "
            "should be memoized so bake-loop batching doesn't "
            "re-paginate (see regression note in module docstring)"
        )


class TestPolyhavenMemoizesDiscover:
    def test_three_fetches_trigger_one_discover(self, tmp_path):
        from mat_vis_baker.sources import polyhaven

        fake_assets = {"a1": {"name": "a1", "type": 1}}
        with patch.object(polyhaven, "discover", return_value=fake_assets) as m:
            for offset in (0, 50, 100):
                polyhaven.fetch("1k", tmp_path, limit=50, offset=offset)
        assert m.call_count == 1, (
            f"polyhaven.discover called {m.call_count}× across 3 fetches — "
            "should be memoized"
        )


class TestGpuopenMemoizesDiscover:
    def test_three_fetches_trigger_one_discover(self, tmp_path):
        from mat_vis_baker.sources import gpuopen

        fake_mats: list[dict] = []
        with patch.object(gpuopen, "discover", return_value=fake_mats) as m:
            for offset in (0, 50, 100):
                gpuopen.fetch("1k", tmp_path, limit=50, offset=offset)
        assert m.call_count == 1, (
            f"gpuopen.discover called {m.call_count}× across 3 fetches — "
            "should be memoized"
        )


class TestResetHelper:
    def test_reset_allows_fresh_discover(self, tmp_path):
        """The ``_reset_discover_cache`` helper exists so tests (and
        hypothetical catalog-refresh scenarios) can force a re-pagination
        on the next ``fetch``."""
        from mat_vis_baker.sources import ambientcg

        fake = [{"assetId": "M1", "displayName": "M1", "downloadFolders": {}, "tags": []}]
        with patch.object(ambientcg, "discover", return_value=fake) as m:
            ambientcg.fetch("1k", tmp_path, limit=50, offset=0)
            ambientcg.fetch("1k", tmp_path, limit=50, offset=50)
            assert m.call_count == 1
            ambientcg._reset_discover_cache()
            ambientcg.fetch("1k", tmp_path, limit=50, offset=100)
            assert m.call_count == 2, (
                "discover must be re-called after _reset_discover_cache()"
            )
