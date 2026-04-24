"""Per-file substrate baker (ADR-0012 / #182).

Covers the three load-bearing behaviours of ``bake_one_per_file``:

1. Every (source, tier, material, channel) baked texture lands as an
   individual HF file at ``<source>/<tier>/<mid>/<channel>.{png,ktx2}``.
   No tar, no rowmap.
2. A pre-flight tree scan of the target revision skips materials
   whose files are already committed — resumable-by-default across
   crashes / SIGTERMs / rate-limit stalls.
3. Commits happen in batches (default N=50 materials); each commit is
   a durable checkpoint. A `.tier_complete` sentinel file lands as the
   final commit per tier so clients can detect tier-level atomicity
   (restoring ADR-0007's invariant on top of the new substrate).

Pure-Python tests — HfApi is mocked; no HF calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# Import target — will initially fail (RED) until the module exists.
bake_module_available = False
try:
    from mat_vis_baker.hf_bake_per_file import bake_one_per_file  # type: ignore

    bake_module_available = True
except ImportError:
    bake_one_per_file = None  # type: ignore


pytestmark = pytest.mark.skipif(
    not bake_module_available,
    reason="RED phase — mat_vis_baker.hf_bake_per_file not yet implemented",
)


def _fake_record(mid: str, channels: dict[str, bytes], work_dir: Path):
    """Build a minimal ``MaterialRecord`` stub that has channel bytes
    on disk under ``work_dir / textures / <mid> / <channel>.png``."""
    from mat_vis_baker.common import (
        AttributionBlock,
        MatVisBlock,
        MaterialRecord,
    )

    d = work_dir / "textures" / mid
    d.mkdir(parents=True, exist_ok=True)
    paths = {}
    for ch, data in channels.items():
        p = d / f"{ch}.png"
        p.write_bytes(data)
        paths[ch] = p

    return MaterialRecord(
        id=mid,
        source="polyhaven",
        mat_vis=MatVisBlock(
            name=mid,
            category="other",
            upstream_id=mid,
            attribution=AttributionBlock(license_spdx="CC0-1.0"),
        ),
        texture_paths=paths,
        maps=list(channels.keys()),
        status="ok",
    )


class TestBakeOnePerFile:
    def test_writes_one_hf_file_per_channel(self, tmp_path):
        """Three materials × two channels = six CommitOperationAdd
        entries + one catalog JSON + one .tier_complete sentinel."""
        PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
        fake_records = [
            _fake_record(
                f"mat_{i}",
                {"color": PNG_MAGIC + b"\x00" * 100, "normal": PNG_MAGIC + b"\x00" * 80},
                tmp_path,
            )
            for i in range(3)
        ]

        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
        ):

            def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
                end = None if limit is None else offset + limit
                return fake_records[offset:end]

            fetcher.return_value = _sliced
            api = api_cls.return_value
            api.list_repo_tree.return_value = []  # nothing committed yet

            result = bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
            )

        # Union of all adds across all create_commit calls must contain
        # the 6 texture paths + the catalog + the tier-complete sentinel.
        all_adds: list[str] = []
        for call in api.create_commit.call_args_list:
            for op in call.kwargs.get("operations", call.args[-1] if call.args else []):
                all_adds.append(op.path_in_repo)

        expected_textures = {
            f"polyhaven/1k/mat_{i}/{ch}.png" for i in range(3) for ch in ("color", "normal")
        }
        assert expected_textures <= set(all_adds), (
            f"missing texture files. present={sorted(all_adds)}"
        )
        assert "polyhaven.json" in all_adds
        assert "polyhaven/1k/.tier_complete" in all_adds
        assert result["ok"] == 3
        assert result["failed"] == 0

    def test_preflight_skips_already_committed_materials(self, tmp_path):
        """If two of three materials already live on HF, baker only
        processes the missing one — by-design resume."""
        fake_records = [
            _fake_record(f"mat_{i}", {"color": b"PNG\x00" * 20}, tmp_path) for i in range(3)
        ]

        # Fake tree: mat_0 + mat_1 already have color.png committed.
        from huggingface_hub.hf_api import RepoFile

        existing_files = [
            RepoFile(path=f"polyhaven/1k/mat_{i}/color.png", size=80, oid="x") for i in range(2)
        ]

        bake_calls: list[str] = []

        def track_bake(rec, *a, **k):
            bake_calls.append(rec.id)
            return rec

        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=track_bake),
        ):

            def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
                end = None if limit is None else offset + limit
                return fake_records[offset:end]

            fetcher.return_value = _sliced
            api = api_cls.return_value
            api.list_repo_tree.return_value = existing_files

            bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
            )

        # bake_material called only for mat_2 (the missing one).
        assert bake_calls == ["mat_2"], (
            f"preflight should have skipped mat_0, mat_1; ran {bake_calls}"
        )

    def test_batch_commits_checkpoint_progress(self, tmp_path):
        """batch_size=2 across 5 materials → 3 batch commits +
        catalog commit + sentinel commit. Each batch is durable."""
        fake_records = [
            _fake_record(f"mat_{i}", {"color": b"PNG\x00" * 10}, tmp_path) for i in range(5)
        ]

        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
        ):

            def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
                end = None if limit is None else offset + limit
                return fake_records[offset:end]

            fetcher.return_value = _sliced
            api = api_cls.return_value
            api.list_repo_tree.return_value = []

            bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
                batch_size=2,
            )

        # Expect: 3 texture-batch commits (2+2+1) + 1 catalog commit
        # + 1 sentinel commit = 5 total.
        assert api.create_commit.call_count == 5, (
            f"expected 5 commits (3 batches + catalog + sentinel); "
            f"got {api.create_commit.call_count}"
        )

    def test_prod_target_requires_allow_prod(self, tmp_path):
        """Safety rail: non-*-tst targets require opt-in, same as the
        Dagger-level guard in #178 — enforced at the baker entry too."""
        with pytest.raises(ValueError, match="allow_prod"):
            bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v2026.05.0",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis",  # non-tst — refuse
            )

    def test_empty_fetcher_result_returns_no_materials_error(self, tmp_path):
        """Fetcher with nothing to bake — return early with an explicit
        error code rather than committing an empty sentinel."""
        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
        ):
            fetcher.return_value = lambda *a, **kw: []
            api = api_cls.return_value
            api.list_repo_tree.return_value = []

            result = bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
            )
        assert result.get("error")
        assert result.get("ok", 0) == 0
