"""Tests for the hf_push primitive (ADR-0007 Phase 1).

All HF API calls are mocked — these tests must not touch the network.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mat_vis_baker.hf_push import TagShadowsBranchError, push_to_hf


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


def test_tag_shadow_raises_typed_error(fake_files: list[tuple[Path, str]]) -> None:
    """#117: a tag shadowing the branch → typed error, not the raw 400."""
    from huggingface_hub.errors import HfHubHTTPError

    with patch("huggingface_hub.HfApi") as api_cls:
        api = api_cls.return_value
        api.list_repo_commits.return_value = [MagicMock()]  # branch exists
        api.create_commit.side_effect = HfHubHTTPError(
            "You cannot commit to this branch because a tag with the same "
            "name exists (Request ID: Root=1-deadbeef)",
            response=MagicMock(status_code=400),
        )

        with pytest.raises(TagShadowsBranchError) as ei:
            push_to_hf(
                repo_id="gerchowl/mat-vis",
                files=fake_files,
                revision="v2026.04.1",
                commit_message="m",
                token="t0k",
            )

    # The typed error must be actionable — name the recovery paths + the issue.
    msg = str(ei.value)
    assert "v2026.04.1" in msg
    assert "delete_tag" in msg
    assert "release/" in msg
    assert "#117" in msg
    # And preserve the underlying HF error as the cause.
    assert ei.value.__cause__ is not None


def test_non_shadow_http_error_propagates(fake_files: list[tuple[Path, str]]) -> None:
    """An unrelated HF 4xx/5xx is NOT swallowed into the typed error."""
    from huggingface_hub.errors import HfHubHTTPError

    with patch("huggingface_hub.HfApi") as api_cls:
        api = api_cls.return_value
        api.list_repo_commits.return_value = [MagicMock()]
        api.create_commit.side_effect = HfHubHTTPError(
            "500 Internal Server Error", response=MagicMock(status_code=500)
        )

        with pytest.raises(HfHubHTTPError):
            push_to_hf(
                repo_id="gerchowl/mat-vis",
                files=fake_files,
                revision="v2026.04.1",
                commit_message="m",
                token="t0k",
            )


# --- AC3: live-network regression, skipped by default -----------------------
# Opt in with MAT_VIS_LIVE_HF=1 and a HF_TOKEN carrying WRITE scope on the
# scratch dataset. Creates a throwaway branch + shadowing tag, asserts
# push_to_hf raises the typed error (not a raw BadRequestError), and tears
# everything down. Never touches prod.
_LIVE_REPO = "gerchowl/mat-vis-tst"
_LIVE_NAME = "v0.0.0-issue117-pytest"


@pytest.mark.skipif(
    os.environ.get("MAT_VIS_LIVE_HF") != "1" or not os.environ.get("HF_TOKEN"),
    reason="live HF regression — set MAT_VIS_LIVE_HF=1 + HF_TOKEN (write on scratch repo)",
)
def test_tag_shadow_live_regression(tmp_path: Path) -> None:
    from huggingface_hub import HfApi
    from huggingface_hub.utils import HfHubHTTPError

    assert _LIVE_REPO != "gerchowl/mat-vis", "refuse to run against prod"
    api = HfApi(token=os.environ["HF_TOKEN"])

    def _teardown() -> None:
        for fn, kw in (
            (api.delete_tag, {"tag": _LIVE_NAME}),
            (api.delete_branch, {"branch": _LIVE_NAME}),
        ):
            try:
                fn(repo_id=_LIVE_REPO, repo_type="dataset", **kw)
            except HfHubHTTPError:
                pass

    payload = tmp_path / "probe.txt"
    payload.write_text("issue-117 pytest live probe\n")

    _teardown()  # fresh start
    try:
        api.create_branch(
            repo_id=_LIVE_REPO, repo_type="dataset", branch=_LIVE_NAME,
            revision="main", exist_ok=True,
        )
        api.create_tag(
            repo_id=_LIVE_REPO, repo_type="dataset", tag=_LIVE_NAME,
            revision="main", exist_ok=True,
        )
        with pytest.raises(TagShadowsBranchError):
            push_to_hf(
                repo_id=_LIVE_REPO,
                files=[(payload, "issue117/probe.txt")],
                revision=_LIVE_NAME,
                commit_message="issue117 live regression",
                token=os.environ["HF_TOKEN"],
            )
    finally:
        _teardown()
