"""Scalar-source bake tests for ``hf_bake.bake_scalar_source`` (#251).

Pre-#251 ``bake_scalar_source`` only pushed ``<source>.json`` and never
touched ``release-manifest.json``. That worked when the client
reconstructed the manifest from a tree listing, but #239 switched the
client to read ``release-manifest.json`` directly, so a scalar bake
silently left its source invisible to clients.

These tests pin the post-#251 contract:

1. ``bake_scalar_source`` produces TWO commits — the catalog (one file)
   and the manifest (one file).
2. The manifest commit merges a ``{"catalog": "<source>.json", "tiers":
   {"scalar": {"complete": True}}}`` entry into whatever the existing
   manifest already holds.
3. The manifest commit opts into HF's ``parent_commit`` lock for CAS
   (matching the per-file baker's pattern).

Pure-Python: every HF API call is mocked. No HF traffic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from mat_vis_baker.hf_bake import bake_scalar_source


def _fake_record(mid: str):
    """Minimal scalar-shape MaterialRecord — no textures."""
    from mat_vis_baker.common import (
        AttributionBlock,
        MaterialRecord,
        MatVisBlock,
    )

    return MaterialRecord(
        id=mid,
        source="physicallybased",
        mat_vis=MatVisBlock(
            name=mid,
            category="other",
            upstream_id=mid,
            attribution=AttributionBlock(license_spdx="CC0-1.0"),
        ),
        texture_paths={},
        maps=[],
        status="ok",
    )


def _make_commit_info(oid: str = "deadbeefcafe") -> MagicMock:
    info = MagicMock()
    info.oid = oid
    info.commit_oid = oid
    return info


class TestBakeScalarSource:
    """#251: scalar bakes must update release-manifest.json."""

    def test_emits_catalog_and_manifest_commits(self, tmp_path: Path) -> None:
        """Two commits land: one with ``<source>.json``, one with
        ``release-manifest.json``. Pre-#251 only the catalog commit
        existed and clients never saw the source post-#239."""
        records = [_fake_record(f"mat_{i}") for i in range(3)]

        with (
            patch("mat_vis_baker.hf_bake._get_fetcher") as fetcher,
            patch("huggingface_hub.HfApi") as push_api_cls,
            patch("mat_vis_baker.hf_bake.HfApi") as bake_api_cls,
        ):
            fetcher.return_value = lambda: records

            push_api = push_api_cls.return_value
            push_api.list_repo_commits.return_value = [MagicMock()]
            push_api.create_commit.return_value = _make_commit_info("catalogsha")

            bake_api = bake_api_cls.return_value
            # No prior manifest on the branch — _fetch_manifest_with_parent
            # falls back to {} and parent_sha=None.
            bake_api.repo_info.side_effect = Exception("revision missing — fresh branch")
            bake_api.hf_hub_download.side_effect = Exception("404 — no manifest yet")
            bake_api.create_commit.return_value = _make_commit_info("manifestsha")

            result = bake_scalar_source(
                source="physicallybased",
                release_tag="v2026.04.2",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
                hf_token="fake",
            )

        # Catalog commit went via push_to_hf → push_api.create_commit.
        assert push_api.create_commit.call_count == 1, (
            f"expected 1 catalog commit via push_to_hf; got {push_api.create_commit.call_count}"
        )
        catalog_call = push_api.create_commit.call_args
        catalog_ops = catalog_call.kwargs["operations"]
        catalog_paths = {op.path_in_repo for op in catalog_ops}
        assert catalog_paths == {"physicallybased.json"}, catalog_paths

        # Manifest commit went via the bake-side HfApi → bake_api.create_commit.
        assert bake_api.create_commit.call_count == 1, (
            f"expected 1 manifest commit via bake-side HfApi; "
            f"got {bake_api.create_commit.call_count}"
        )
        manifest_call = bake_api.create_commit.call_args
        manifest_ops = manifest_call.kwargs["operations"]
        manifest_paths = {op.path_in_repo for op in manifest_ops}
        assert manifest_paths == {"release-manifest.json"}, manifest_paths

        # Result surfaces both SHAs and the materials count.
        assert result["materials"] == len(records)
        assert result["catalog_commit"] == "catalogsha"
        assert result["manifest_commit"] == "manifestsha"

    def test_manifest_payload_carries_scalar_tier_entry(self, tmp_path: Path) -> None:
        """The on-disk manifest written into the commit operation must
        contain ``{"sources": {"physicallybased": {"catalog":
        "physicallybased.json", "tiers": {"scalar": {"complete":
        True}}}}}``. Asserting on the file content (not just the
        commit op path) catches a regression that commits an empty or
        wrong-shape manifest."""
        records = [_fake_record("mat_a"), _fake_record("mat_b")]

        with (
            patch("mat_vis_baker.hf_bake._get_fetcher") as fetcher,
            patch("huggingface_hub.HfApi") as push_api_cls,
            patch("mat_vis_baker.hf_bake.HfApi") as bake_api_cls,
        ):
            fetcher.return_value = lambda: records

            push_api = push_api_cls.return_value
            push_api.list_repo_commits.return_value = [MagicMock()]
            push_api.create_commit.return_value = _make_commit_info("catalogsha")

            bake_api = bake_api_cls.return_value
            bake_api.repo_info.side_effect = Exception("fresh")
            bake_api.hf_hub_download.side_effect = Exception("404")
            bake_api.create_commit.return_value = _make_commit_info("manifestsha")

            bake_scalar_source(
                source="physicallybased",
                release_tag="v2026.04.2",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
                hf_token="fake",
            )

        manifest_path = tmp_path / "release-manifest.json"
        assert manifest_path.exists(), "manifest file must be written before commit"
        manifest = json.loads(manifest_path.read_text())

        assert manifest["release_tag"] == "v2026.04.2"
        assert manifest["schema_version"] == 3
        assert "physicallybased" in manifest["sources"]
        entry = manifest["sources"]["physicallybased"]
        assert entry["catalog"] == "physicallybased.json"
        assert entry["tiers"] == {"scalar": {"complete": True}}

    def test_manifest_merges_into_existing_sources(self, tmp_path: Path) -> None:
        """When the release tag already has a manifest with other
        sources baked, ``bake_scalar_source`` MUST preserve them and
        only add/overwrite its own entry — anything else regresses
        the multi-source race fix from #207/#208."""
        records = [_fake_record("mat_a")]

        existing_manifest = {
            "schema_version": 3,
            "release_tag": "v2026.04.2",
            "sources": {
                "polyhaven": {
                    "catalog": "polyhaven.json",
                    "tiers": {"1k": {"complete": True}, "2k": {"complete": True}},
                },
                "ambientcg": {
                    "catalog": "ambientcg.json",
                    "tiers": {"2k": {"complete": True}},
                },
            },
        }
        existing_path = tmp_path / "existing-release-manifest.json"
        existing_path.write_text(json.dumps(existing_manifest))

        with (
            patch("mat_vis_baker.hf_bake._get_fetcher") as fetcher,
            patch("huggingface_hub.HfApi") as push_api_cls,
            patch("mat_vis_baker.hf_bake.HfApi") as bake_api_cls,
        ):
            fetcher.return_value = lambda: records

            push_api = push_api_cls.return_value
            push_api.list_repo_commits.return_value = [MagicMock()]
            push_api.create_commit.return_value = _make_commit_info("catalogsha")

            bake_api = bake_api_cls.return_value
            # Existing manifest + parent SHA — _fetch_manifest_with_parent
            # returns ({polyhaven, ambientcg}, "parentsha").
            bake_api.repo_info.return_value = MagicMock(sha="parentsha")
            bake_api.hf_hub_download.return_value = str(existing_path)
            bake_api.create_commit.return_value = _make_commit_info("manifestsha")

            bake_scalar_source(
                source="physicallybased",
                release_tag="v2026.04.2",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
                hf_token="fake",
            )

        manifest = json.loads((tmp_path / "release-manifest.json").read_text())
        sources = manifest["sources"]
        # Pre-existing sources preserved.
        assert "polyhaven" in sources
        assert sources["polyhaven"]["tiers"] == {
            "1k": {"complete": True},
            "2k": {"complete": True},
        }
        assert "ambientcg" in sources
        assert sources["ambientcg"]["tiers"] == {"2k": {"complete": True}}
        # Scalar entry layered in.
        assert sources["physicallybased"] == {
            "catalog": "physicallybased.json",
            "tiers": {"scalar": {"complete": True}},
        }

        # Manifest commit must opt into parent_commit CAS.
        manifest_call = bake_api.create_commit.call_args
        assert manifest_call.kwargs.get("parent_commit") == "parentsha", (
            "manifest commit must pass parent_commit for CAS retry"
        )
