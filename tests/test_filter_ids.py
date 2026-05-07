"""Tests for the ``filter_ids`` per-source fetcher kwarg (#342).

Spot-test affordance: dispatching ``bake.yml`` with
``filter-ids=Bronze_Oxydized,Aluminum_Hexagon,...`` should restrict
each fetcher's material loop to exactly those ids before
``offset`` / ``limit`` slicing fires. None / empty list = no filter
(current behavior). Non-empty filter that matches zero materials
raises ``ValueError`` with the unmatched ids — the call site fails
loud rather than producing an empty bake.

#342 / surfaced from #316 procedural-PBR Phase 1 verification.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mat_vis_baker.sources import ambientcg, gpuopen, polyhaven


# Minimal upstream-payload shapes per source. Just enough structure
# to flow through the filter step + a no-op _fetch_one stub.


def _gpuopen_materials() -> list[dict]:
    return [
        {
            "id": "uuid-bronze",
            "title": "Bronze Oxydized",
            "_category_title": "Metal",
            "_tag_titles": ["metal"],
            "packages": [],
        },
        {
            "id": "uuid-aluminum",
            "title": "Aluminum Hexagon",
            "_category_title": "Metal",
            "_tag_titles": ["metal"],
            "packages": [],
        },
        {
            "id": "uuid-foam",
            "title": "Acoustic Foam 001",
            "_category_title": "Foam",
            "_tag_titles": ["foam"],
            "packages": [],
        },
        {
            "id": "uuid-wallpaper",
            "title": "Boutique Maroon Wallpaper",
            "_category_title": "Wallpaper",
            "_tag_titles": [],
            "packages": [],
        },
    ]


def _ambientcg_entries() -> list[dict]:
    return [
        {"assetId": "Metal001", "downloadFolders": {}},
        {"assetId": "Wood001", "downloadFolders": {}},
        {"assetId": "Bricks001", "downloadFolders": {}},
    ]


def _polyhaven_assets() -> dict:
    return {
        "rusty_metal_02": {"name": "Rusty Metal 02"},
        "wood_floor_01": {"name": "Wood Floor 01"},
        "concrete_05": {"name": "Concrete 05"},
    }


# ── gpuopen ────────────────────────────────────────────────────


class TestGpuopenFilterIds:
    def test_filter_ids_restricts_loop(self, tmp_path: Path) -> None:
        # Patch _cached_materials to return our small fixture set;
        # _fetch_one is patched to return a sentinel record so the
        # iteration count is what we measure.
        called_ids: list[str] = []

        def _fake_fetch_one(mat, tier, output_dir, mtlx_dir=None):
            called_ids.append(mat["id"])
            return MagicMock(id=mat["id"], status="ok", needs_mtlx_bake=False, texture_paths={})

        with (
            patch.object(gpuopen, "_cached_materials", return_value=_gpuopen_materials()),
            patch.object(gpuopen, "_fetch_one", side_effect=_fake_fetch_one),
        ):
            gpuopen.fetch(
                "1k",
                tmp_path,
                filter_ids=["uuid-bronze", "uuid-aluminum"],
            )
        assert sorted(called_ids) == ["uuid-aluminum", "uuid-bronze"]

    def test_no_filter_ids_iterates_full_corpus(self, tmp_path: Path) -> None:
        called_ids: list[str] = []

        def _fake_fetch_one(mat, tier, output_dir, mtlx_dir=None):
            called_ids.append(mat["id"])
            return MagicMock(id=mat["id"], status="ok", needs_mtlx_bake=False, texture_paths={})

        with (
            patch.object(gpuopen, "_cached_materials", return_value=_gpuopen_materials()),
            patch.object(gpuopen, "_fetch_one", side_effect=_fake_fetch_one),
        ):
            gpuopen.fetch("1k", tmp_path)
        assert len(called_ids) == 4

    def test_empty_filter_ids_is_noop(self, tmp_path: Path) -> None:
        # Empty list = no filter (matches the workflow input default).
        called_ids: list[str] = []

        def _fake_fetch_one(mat, tier, output_dir, mtlx_dir=None):
            called_ids.append(mat["id"])
            return MagicMock(id=mat["id"], status="ok", needs_mtlx_bake=False, texture_paths={})

        with (
            patch.object(gpuopen, "_cached_materials", return_value=_gpuopen_materials()),
            patch.object(gpuopen, "_fetch_one", side_effect=_fake_fetch_one),
        ):
            gpuopen.fetch("1k", tmp_path, filter_ids=[])
        assert len(called_ids) == 4

    def test_unmatched_filter_ids_raises(self, tmp_path: Path) -> None:
        with patch.object(gpuopen, "_cached_materials", return_value=_gpuopen_materials()):
            with pytest.raises(ValueError, match="filter_ids"):
                gpuopen.fetch("1k", tmp_path, filter_ids=["uuid-typo"])

    def test_filter_ids_applied_before_offset_limit(self, tmp_path: Path) -> None:
        # filter_ids selects 3 materials; limit=2 then takes the first
        # 2 of those 3. Confirms order-of-operations.
        called_ids: list[str] = []

        def _fake_fetch_one(mat, tier, output_dir, mtlx_dir=None):
            called_ids.append(mat["id"])
            return MagicMock(id=mat["id"], status="ok", needs_mtlx_bake=False, texture_paths={})

        with (
            patch.object(gpuopen, "_cached_materials", return_value=_gpuopen_materials()),
            patch.object(gpuopen, "_fetch_one", side_effect=_fake_fetch_one),
        ):
            gpuopen.fetch(
                "1k",
                tmp_path,
                filter_ids=["uuid-bronze", "uuid-aluminum", "uuid-foam"],
                limit=2,
            )
        assert len(called_ids) == 2
        # The two materials must be a subset of the filter list.
        assert set(called_ids) <= {"uuid-bronze", "uuid-aluminum", "uuid-foam"}


# ── ambientcg ──────────────────────────────────────────────────


class TestAmbientcgFilterIds:
    def test_filter_ids_restricts_loop(self, tmp_path: Path) -> None:
        called_ids: list[str] = []

        def _fake_fetch_one(entry, tier, output_dir, mtlx_dir=None):
            called_ids.append(entry["assetId"])
            return MagicMock(
                id=entry["assetId"], status="ok", needs_mtlx_bake=False, texture_paths={}
            )

        with (
            patch.object(ambientcg, "_cached_entries", return_value=_ambientcg_entries()),
            patch.object(
                ambientcg, "_filter_with_downloads", side_effect=lambda entries, tier: entries
            ),
            patch.object(ambientcg, "_fetch_one", side_effect=_fake_fetch_one),
        ):
            ambientcg.fetch("1k", tmp_path, filter_ids=["Metal001"])
        assert called_ids == ["Metal001"]

    def test_unmatched_filter_ids_raises(self, tmp_path: Path) -> None:
        with (
            patch.object(ambientcg, "_cached_entries", return_value=_ambientcg_entries()),
            patch.object(
                ambientcg, "_filter_with_downloads", side_effect=lambda entries, tier: entries
            ),
        ):
            with pytest.raises(ValueError, match="filter_ids"):
                ambientcg.fetch("1k", tmp_path, filter_ids=["TypoId"])


# ── polyhaven ──────────────────────────────────────────────────


class TestPolyhavenFilterIds:
    def test_filter_ids_restricts_loop(self, tmp_path: Path) -> None:
        called_ids: list[str] = []

        def _fake_fetch_one(slug, meta, tier, output_dir, mtlx_dir=None):
            called_ids.append(slug)
            return MagicMock(id=slug, status="ok", needs_mtlx_bake=False, texture_paths={})

        with (
            patch.object(polyhaven, "_cached_assets", return_value=_polyhaven_assets()),
            patch.object(polyhaven, "_fetch_one", side_effect=_fake_fetch_one),
        ):
            polyhaven.fetch("1k", tmp_path, filter_ids=["rusty_metal_02"])
        assert called_ids == ["rusty_metal_02"]

    def test_unmatched_filter_ids_raises(self, tmp_path: Path) -> None:
        with patch.object(polyhaven, "_cached_assets", return_value=_polyhaven_assets()):
            with pytest.raises(ValueError, match="filter_ids"):
                polyhaven.fetch("1k", tmp_path, filter_ids=["typo_slug"])
