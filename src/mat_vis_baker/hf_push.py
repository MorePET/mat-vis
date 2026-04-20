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
    from huggingface_hub.errors import RevisionNotFoundError

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

    commit_info = api.create_commit(
        repo_id=repo_id,
        repo_type="dataset",
        operations=operations,
        commit_message=commit_message,
        revision=revision,
    )

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
