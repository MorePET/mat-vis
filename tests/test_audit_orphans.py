"""Tests for ``mat_vis_baker.audit_orphans`` (#190 / ADR-0012 follow-up).

All HF traffic is mocked — no live network calls. Mirrors the style of
``tests/test_bake_one_routing.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mat_vis_baker.audit_orphans import audit_orphans


def _fake_lfs_blob(oid: str, filename: str = "x") -> SimpleNamespace:
    """Mimic ``huggingface_hub.hf_api.LFSFileInfo`` enough for the auditor."""
    return SimpleNamespace(
        oid=oid,
        file_oid=oid,
        filename=filename,
        size=1,
        ref="main",
        pushed_at=None,
    )


def _fake_repo_file(path: str, lfs_oid: str | None) -> SimpleNamespace:
    """Mimic ``RepoFile``; ``lfs.sha256`` matches what the auditor checks."""
    lfs = SimpleNamespace(sha256=lfs_oid, size=1, pointer_size=130) if lfs_oid else None
    return SimpleNamespace(path=path, size=1, blob_id="g" + (lfs_oid or "0"), lfs=lfs)


def _make_api(lfs_blobs, tree_entries) -> MagicMock:
    api = MagicMock()
    api.list_lfs_files.return_value = list(lfs_blobs)
    api.list_repo_tree.return_value = list(tree_entries)
    return api


class TestAuditOrphans:
    def test_empty_repo_no_lfs(self) -> None:
        api = _make_api(lfs_blobs=[], tree_entries=[])
        result = audit_orphans(repo_id="gerchowl/mat-vis-tst", api=api)

        assert result["total_lfs"] == 0
        assert result["referenced"] == 0
        assert result["orphans"] == []
        assert result["deleted"] is None
        assert result["repo_id"] == "gerchowl/mat-vis-tst"
        assert result["revision"] == "main"
        api.permanently_delete_lfs_files.assert_not_called()

    def test_all_lfs_referenced_no_orphans(self) -> None:
        blobs = [_fake_lfs_blob("oid-a"), _fake_lfs_blob("oid-b")]
        tree = [
            _fake_repo_file("a.png", "oid-a"),
            _fake_repo_file("b.png", "oid-b"),
            # Non-LFS file should not contribute to referenced count.
            _fake_repo_file("manifest.json", None),
        ]
        api = _make_api(lfs_blobs=blobs, tree_entries=tree)

        result = audit_orphans(repo_id="gerchowl/mat-vis-tst", api=api)

        assert result["total_lfs"] == 2
        assert result["referenced"] == 2
        assert result["orphans"] == []
        assert result["deleted"] is None
        api.permanently_delete_lfs_files.assert_not_called()

    def test_unreferenced_lfs_is_orphan_dry_run(self) -> None:
        blobs = [
            _fake_lfs_blob("oid-keep"),
            _fake_lfs_blob("oid-orphan", filename="crashed.png"),
        ]
        tree = [_fake_repo_file("kept.png", "oid-keep")]
        api = _make_api(lfs_blobs=blobs, tree_entries=tree)

        result = audit_orphans(repo_id="gerchowl/mat-vis-tst", api=api)

        assert result["total_lfs"] == 2
        assert result["referenced"] == 1
        assert result["orphans"] == ["oid-orphan"]
        # Dry-run: deleted stays None — distinguishes "didn't run delete"
        # from "ran delete, found 0".
        assert result["deleted"] is None
        api.permanently_delete_lfs_files.assert_not_called()

    def test_delete_calls_permanently_delete(self) -> None:
        blobs = [
            _fake_lfs_blob("oid-keep"),
            _fake_lfs_blob("oid-orphan-1"),
            _fake_lfs_blob("oid-orphan-2"),
        ]
        tree = [_fake_repo_file("kept.png", "oid-keep")]
        api = _make_api(lfs_blobs=blobs, tree_entries=tree)

        result = audit_orphans(repo_id="gerchowl/mat-vis-tst", api=api, delete=True)

        assert sorted(result["orphans"]) == ["oid-orphan-1", "oid-orphan-2"]
        assert result["deleted"] == 2
        api.permanently_delete_lfs_files.assert_called_once()
        kwargs = api.permanently_delete_lfs_files.call_args.kwargs
        assert kwargs["repo_id"] == "gerchowl/mat-vis-tst"
        assert kwargs["repo_type"] == "dataset"
        passed_oids = {b.oid for b in kwargs["lfs_files"]}
        assert passed_oids == {"oid-orphan-1", "oid-orphan-2"}

    def test_delete_with_no_orphans_records_zero(self) -> None:
        """``delete=True`` but nothing to delete → ``deleted=0``, no API call."""
        blobs = [_fake_lfs_blob("oid-a")]
        tree = [_fake_repo_file("a.png", "oid-a")]
        api = _make_api(lfs_blobs=blobs, tree_entries=tree)

        result = audit_orphans(repo_id="gerchowl/mat-vis-tst", api=api, delete=True)

        assert result["orphans"] == []
        assert result["deleted"] == 0
        api.permanently_delete_lfs_files.assert_not_called()

    def test_prod_guard_refuses_canonical_repo(self) -> None:
        api = _make_api(lfs_blobs=[], tree_entries=[])

        with pytest.raises(ValueError, match="Refusing to audit non-scratch repo"):
            audit_orphans(repo_id="gerchowl/mat-vis", api=api)

        # API must NOT be called when the guard refuses.
        api.list_lfs_files.assert_not_called()
        api.list_repo_tree.assert_not_called()

    def test_prod_guard_allow_prod_lets_through(self) -> None:
        api = _make_api(lfs_blobs=[], tree_entries=[])
        result = audit_orphans(repo_id="gerchowl/mat-vis", api=api, allow_prod=True)
        assert result["orphans"] == []

    def test_revision_threaded_through(self) -> None:
        api = _make_api(lfs_blobs=[], tree_entries=[])
        audit_orphans(
            repo_id="gerchowl/mat-vis-tst",
            api=api,
            revision="v2026.05.0",
        )
        kwargs = api.list_repo_tree.call_args.kwargs
        assert kwargs["revision"] == "v2026.05.0"
        assert kwargs["recursive"] is True
        assert kwargs["repo_type"] == "dataset"

    def test_scratch_repo_pattern_matrix(self) -> None:
        """Mirror the scratch-repo regex from hf_bake_per_file._guard_prod_target."""
        api = _make_api(lfs_blobs=[], tree_entries=[])
        # Both scratch patterns must pass without allow_prod.
        audit_orphans(repo_id="gerchowl/mat-vis-tst", api=api)
        audit_orphans(repo_id="gerchowl/mat-vis-pr-tst", api=api)
