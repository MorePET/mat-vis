"""Routing tests for ``hf_bake.bake_one`` (ADR-0012).

Textured sources route to ``bake_one_per_file``; scalar sources route
to ``bake_scalar_source``. The legacy tar branch was retired in #189.

Pure-Python: every downstream callable is mocked. No HF traffic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from mat_vis_baker.hf_bake import bake_one


class TestBakeOneRouting:
    def test_textured_source_routes_per_file(self, tmp_path: Path) -> None:
        with (
            patch("mat_vis_baker.hf_bake_per_file.bake_one_per_file") as per_file,
            patch("mat_vis_baker.hf_bake.bake_scalar_source") as scalar,
        ):
            per_file.return_value = {"ok": 2, "failed": 0, "skipped_preflight": 0}

            result = bake_one(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
                hf_token="t",
                limit=2,
            )

        assert per_file.called, "textured source must route to bake_one_per_file"
        assert not scalar.called
        kwargs = per_file.call_args.kwargs
        assert kwargs["source"] == "polyhaven"
        assert kwargs["tier"] == "1k"
        assert kwargs["repo_id"] == "gerchowl/mat-vis-tst"
        assert kwargs["allow_prod"] is False
        assert result["ok"] == 2

    def test_scalar_source_routes_to_scalar_baker(self, tmp_path: Path) -> None:
        with (
            patch("mat_vis_baker.hf_bake.bake_scalar_source") as scalar,
            patch("mat_vis_baker.hf_bake_per_file.bake_one_per_file") as per_file,
        ):
            scalar.return_value = {"commit": "abc", "materials": 86}

            result = bake_one(
                source="physicallybased",
                tier="scalar",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
            )

        assert scalar.called
        assert not per_file.called
        assert result["materials"] == 86

    def test_allow_prod_propagates_to_per_file(self, tmp_path: Path) -> None:
        with patch("mat_vis_baker.hf_bake_per_file.bake_one_per_file") as per_file:
            per_file.return_value = {"ok": 1, "failed": 0, "skipped_preflight": 0}

            bake_one(
                source="ambientcg",
                tier="2k",
                release_tag="v2026.05.0",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis",  # prod
                allow_prod=True,
            )

        assert per_file.call_args.kwargs["allow_prod"] is True
