"""Tests for the hf_push primitive (ADR-0007 Phase 1).

All HF API calls are mocked — these tests must not touch the network.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mat_vis_baker.hf_push import push_to_hf


@pytest.fixture
def fake_files(tmp_path: Path) -> list[tuple[Path, str]]:
    paths = []
    for i, name in enumerate(["manifest.json", "a.tar", "a-rowmap.json"]):
        p = tmp_path / name
        p.write_bytes(f"payload-{i}".encode())
        paths.append((p, name))
    return paths


def test_three_files_one_create_commit_call(fake_files: list[tuple[Path, str]]) -> None:
    with patch("huggingface_hub.HfApi") as api_cls:
        api = api_cls.return_value
        api.list_repo_commits.return_value = [MagicMock()]
        api.create_commit.return_value = MagicMock(oid="abc123def456")

        sha = push_to_hf(
            repo_id="gerchowl/mat-vis",
            files=fake_files,
            revision="v2026.05.0",
            commit_message="test",
            token="t0k",
        )

        assert sha == "abc123def456"
        assert api.create_commit.call_count == 1
        kwargs = api.create_commit.call_args.kwargs
        assert kwargs["repo_id"] == "gerchowl/mat-vis"
        assert kwargs["repo_type"] == "dataset"
        assert kwargs["revision"] == "v2026.05.0"
        assert len(kwargs["operations"]) == 3


def test_empty_file_list_is_noop() -> None:
    with patch("huggingface_hub.HfApi") as api_cls:
        sha = push_to_hf(
            repo_id="gerchowl/mat-vis",
            files=[],
            revision="v2026.05.0",
            commit_message="noop",
            token="t0k",
        )
    assert sha == ""
    api_cls.assert_not_called()


def test_explicit_token_overrides_env(
    monkeypatch: pytest.MonkeyPatch, fake_files: list[tuple[Path, str]]
) -> None:
    monkeypatch.setenv("HF_TOKEN", "from-env")

    with patch("huggingface_hub.HfApi") as api_cls:
        api = api_cls.return_value
        api.list_repo_commits.return_value = [MagicMock()]
        api.create_commit.return_value = MagicMock(oid="sha1")

        push_to_hf(
            repo_id="gerchowl/mat-vis",
            files=fake_files,
            revision="main",
            commit_message="m",
            token="explicit-token",
        )

        api_cls.assert_called_once_with(token="explicit-token")


def test_env_token_used_when_none_passed(
    monkeypatch: pytest.MonkeyPatch, fake_files: list[tuple[Path, str]]
) -> None:
    monkeypatch.setenv("HF_TOKEN", "from-env")

    with patch("huggingface_hub.HfApi") as api_cls:
        api = api_cls.return_value
        api.list_repo_commits.return_value = [MagicMock()]
        api.create_commit.return_value = MagicMock(oid="sha1")

        push_to_hf(
            repo_id="gerchowl/mat-vis",
            files=fake_files,
            revision="main",
            commit_message="m",
        )

        api_cls.assert_called_once_with(token="from-env")


def test_missing_revision_creates_branch(fake_files: list[tuple[Path, str]]) -> None:
    from huggingface_hub.errors import RevisionNotFoundError

    with patch("huggingface_hub.HfApi") as api_cls:
        api = api_cls.return_value
        api.list_repo_commits.side_effect = RevisionNotFoundError(
            "nope", response=MagicMock(status_code=404)
        )
        api.create_commit.return_value = MagicMock(oid="sha1")

        push_to_hf(
            repo_id="gerchowl/mat-vis",
            files=fake_files,
            revision="v2026.05.0",
            commit_message="m",
            token="t0k",
        )

        api.create_branch.assert_called_once()
        api.create_commit.assert_called_once()
        assert api.create_branch.call_args.kwargs["branch"] == "v2026.05.0"
        assert api.create_branch.call_args.kwargs["revision"] == "main"


def test_no_branch_creation_when_disabled(fake_files: list[tuple[Path, str]]) -> None:
    from huggingface_hub.errors import RevisionNotFoundError

    with patch("huggingface_hub.HfApi") as api_cls:
        api = api_cls.return_value
        api.list_repo_commits.side_effect = RevisionNotFoundError(
            "nope", response=MagicMock(status_code=404)
        )
        api.create_commit.side_effect = RevisionNotFoundError(
            "still nope", response=MagicMock(status_code=404)
        )

        with pytest.raises(RevisionNotFoundError):
            push_to_hf(
                repo_id="gerchowl/mat-vis",
                files=fake_files,
                revision="v2026.05.0",
                commit_message="m",
                token="t0k",
                create_branch_if_missing=False,
            )

        api.create_branch.assert_not_called()
