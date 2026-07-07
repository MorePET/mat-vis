"""Atomic multi-file push to a Hugging Face dataset.

Wrapper around ``huggingface_hub.HfApi.create_commit`` that mirrors
what the v0.5.0 baker needs: push a list of ``(local_path,
path_in_repo)`` pairs to a named revision of a dataset repo as a
single atomic commit. If the target revision doesn't exist yet, it's
created as a branch from ``main`` first.

Atomicity is the substrate-level guarantee that makes the rowmap ↔
tar consistency invariant hold without a validator pass (ADR-0007,
bug class #79).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("mat-vis-baker.hf_push")


class TagShadowsBranchError(RuntimeError):
    """A tag shadows the intended branch revision, so HF rejects the commit.

    Our release flow reuses the same calver string for both the mutable
    working branch (incremental bakes) and the immutable consumer tag. When
    the tag already exists, ``HfApi.create_commit(revision=<name>)`` resolves
    the ambiguous revision to the immutable tag and rejects the branch commit
    with a raw ``BadRequestError``. ``push_to_hf`` converts that into this
    typed error carrying an actionable recovery hint (issue #117).
    """


def _is_tag_shadow_error(exc: Exception) -> bool:
    """True if ``exc`` is HF's "tag shadows branch" rejection (#117)."""
    msg = str(exc).lower()
    return "tag with the same name" in msg or "cannot commit to this branch" in msg


def push_to_hf(
    repo_id: str,
    files: list[tuple[Path, str]],
    revision: str,
    commit_message: str,
    *,
    token: str | None = None,
    create_branch_if_missing: bool = True,
    delete_paths: list[str] | None = None,
) -> str:
    """Push a set of files to an HF dataset revision atomically.

    Args:
        repo_id: e.g. ``"gerchowl/mat-vis"``.
        files: list of ``(local_path, path_in_repo)`` pairs. Every file
            lands in one ``create_commit`` call.
        revision: target branch/tag (e.g. ``"v2026.04.1"``).
        commit_message: commit message.
        token: HF access token. Falls back to the ``HF_TOKEN`` env var.
        create_branch_if_missing: if True and ``revision`` is not an
            existing branch, create it from ``main`` before committing.
        delete_paths: repo paths to delete in the same atomic commit.
            Used by ``merge-shards`` to drop shard artifacts once the
            merged tar lands.

    Returns:
        The commit SHA of the created commit, or ``""`` when neither
        ``files`` nor ``delete_paths`` has entries (no-op).
    """
    delete_paths = delete_paths or []
    if not files and not delete_paths:
        log.info("push_to_hf: no files and no deletes, skipping")
        return ""

    from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi
    from huggingface_hub.errors import HfHubHTTPError, RevisionNotFoundError

    resolved_token = token if token is not None else os.environ.get("HF_TOKEN")
    api = HfApi(token=resolved_token)

    if create_branch_if_missing:
        try:
            api.list_repo_commits(repo_id=repo_id, repo_type="dataset", revision=revision)
        except RevisionNotFoundError:
            log.info("creating branch %s on %s", revision, repo_id)
            api.create_branch(
                repo_id=repo_id,
                repo_type="dataset",
                branch=revision,
                revision="main",
                exist_ok=True,
            )

    operations: list = [
        CommitOperationAdd(path_in_repo=path_in_repo, path_or_fileobj=str(local_path))
        for local_path, path_in_repo in files
    ]
    operations.extend(CommitOperationDelete(path_in_repo=p) for p in delete_paths)

    try:
        commit_info = api.create_commit(
            repo_id=repo_id,
            repo_type="dataset",
            operations=operations,
            commit_message=commit_message,
            revision=revision,
        )
    except HfHubHTTPError as exc:
        if not _is_tag_shadow_error(exc):
            raise
        # #117: a tag shadows the intended branch. Fail with an actionable
        # typed error instead of leaking the raw 400 mid-bake.
        raise TagShadowsBranchError(
            f"cannot commit to branch {revision!r} on {repo_id!r}: a tag with the "
            f"same name shadows it, so Hugging Face resolved the revision to the "
            f"immutable tag and rejected the commit. Recover by deleting the tag "
            f"(HfApi.delete_tag(repo_id={repo_id!r}, tag={revision!r}, "
            f"repo_type='dataset')) before re-running, or target a branch-shaped "
            f"revision such as 'release/{revision}' and tag at HEAD once the bake "
            f"matrix completes. See issue #117."
        ) from exc

    sha = getattr(commit_info, "oid", "") or getattr(commit_info, "commit_oid", "")
    log.info(
        "push_to_hf: %d adds + %d deletes → %s@%s (%s)",
        len(files),
        len(delete_paths),
        repo_id,
        revision,
        sha[:12] if sha else "?",
    )
    return sha
