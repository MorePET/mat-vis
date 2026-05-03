"""Audit + clean up mid-batch orphan LFS blobs (#190 / ADR-0012 follow-up).

ADR-0012's "Bad / accepted tradeoffs" notes that HF Hub uploads the LFS
blob *before* finalizing the commit. A mid-batch crash can therefore
leave orphan LFS blobs on the object store: bytes uploaded, but no
committed file references the blob. Xet chunk-dedup makes any future
re-upload bytes-free, so the only practical damage is the storage
accounting line on the repo. This module gives operators a way to see
and (with explicit confirmation) reclaim that space.

An orphan = an LFS blob (sha-256 content hash) returned by
``HfApi.list_lfs_files`` whose ``file_oid`` does NOT appear as the
``lfs.sha256`` of any file in the committed tree at ``revision``
(default: ``main``).

Note on the two oid attributes on ``LFSFileInfo``:

- ``LFSFileInfo.oid`` is a 40-char SHA-1 — the Git blob OID of the
  *pointer file* committed under refs/convert/lfs.
- ``LFSFileInfo.file_oid`` is a 64-char SHA-256 — the actual LFS
  content hash, which is what ``BlobLfsInfo.sha256`` returns from
  ``list_repo_tree``. So the orphan check must compare ``file_oid``
  against ``lfs.sha256`` (NOT ``oid`` against ``sha256``, which would
  always disagree because they are different hash functions).

Notes on the underlying API names — `huggingface_hub` (>=1.x) calls
these `list_lfs_files` and `permanently_delete_lfs_files`. The issue
description mentioned older spellings (``list_repo_lfs_files`` /
``delete_lfs_files``); we use the names actually present in the pinned
version. Same shape, different label.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

log = logging.getLogger("mat-vis-baker.audit_orphans")


def _guard_prod_target(repo_id: str, allow_prod: bool) -> None:
    """Refuse the canonical prod repo without ``allow_prod=True``.

    Mirrors ``hf_bake_per_file._guard_prod_target`` so the audit tool
    obeys the same safety rail. Auditing prod read-only is fine, but
    deleting against prod requires the explicit flag — and we keep the
    guard symmetric for both modes so operators don't develop a habit
    of running this against prod casually.
    """
    if "/" in repo_id:
        _owner, name = repo_id.rsplit("/", 1)
        if name == "mat-vis-tst" or (name.endswith("-tst") and name.startswith("mat-vis")):
            return
    if allow_prod:
        return
    raise ValueError(
        f"Refusing to audit non-scratch repo {repo_id!r} without "
        "allow_prod=True. Scratch repos are named .../mat-vis-tst "
        "(or .../mat-vis-*-tst); anything else (including the canonical "
        "gerchowl/mat-vis prod repo) requires explicit allow_prod=True."
    )


def _committed_lfs_oids(api: Any, repo_id: str, revision: str) -> set[str]:
    """Return every ``lfs.sha256`` referenced by the tree at ``revision``."""
    oids: set[str] = set()
    for entry in api.list_repo_tree(
        repo_id=repo_id,
        repo_type="dataset",
        revision=revision,
        recursive=True,
    ):
        lfs = getattr(entry, "lfs", None)
        if lfs is None:
            continue
        # `BlobLfsInfo.sha256` is the lowercased hex SHA-256 of the LFS
        # blob content; matches `LFSFileInfo.file_oid` from
        # `list_lfs_files` (NOT `LFSFileInfo.oid`, which is the SHA-1
        # Git OID of the pointer file).
        sha = getattr(lfs, "sha256", None)
        if sha:
            oids.add(sha)
    return oids


def audit_orphans(
    repo_id: str,
    revision: str | None = None,
    delete: bool = False,
    allow_prod: bool = False,
    hf_token: str | None = None,
    api: Any | None = None,
) -> dict:
    """Walk LFS blobs vs committed tree; report (and optionally delete) orphans.

    Args:
        repo_id: HF dataset repo (``owner/name``).
        revision: Git ref to audit against. Defaults to ``main``.
        delete: If True, permanently delete orphan blobs. Dry-run otherwise.
        allow_prod: Required to audit non-scratch repos (e.g. ``gerchowl/mat-vis``).
        hf_token: Optional token for ``HfApi``. Falls back to the cached login.
        api: Inject a custom ``HfApi`` (used by tests). Production callers
             leave this as ``None``; we'll construct one with ``hf_token``.

    Returns:
        ``{
            "repo_id": ...,
            "revision": ...,
            "total_lfs": N,             # all LFS blobs on the repo
            "referenced": M,            # blobs referenced by the tree
            "orphans": [oid, ...],      # sha-256 oids not referenced
            "deleted": K | None,        # number actually deleted (None on dry-run)
        }``
    """
    _guard_prod_target(repo_id, allow_prod)

    if api is None:
        from huggingface_hub import HfApi

        api = HfApi(token=hf_token)

    rev = revision or "main"

    log.info("listing LFS blobs on %s", repo_id)
    lfs_blobs = list(api.list_lfs_files(repo_id=repo_id, repo_type="dataset"))
    log.info("listing committed tree at %s@%s", repo_id, rev)
    referenced = _committed_lfs_oids(api, repo_id, rev)

    # Compare on `file_oid` (LFS content SHA-256), not `oid` (pointer-file
    # SHA-1). See module docstring for the two-attribute rationale (#221).
    orphan_blobs = [b for b in lfs_blobs if getattr(b, "file_oid", None) not in referenced]
    orphan_oids = [b.file_oid for b in orphan_blobs]

    deleted: int | None = None
    if delete and orphan_blobs:
        log.warning(
            "deleting %d orphan LFS blobs from %s (revision=%s)",
            len(orphan_blobs),
            repo_id,
            rev,
        )
        # `permanently_delete_lfs_files` is a permanent action that
        # rewrites history if any *committed* file ends up unreachable.
        # Since we filter to blobs *not* referenced by the tree, the
        # rewrite is a no-op for orphans — but we keep the API's default.
        api.permanently_delete_lfs_files(
            repo_id=repo_id,
            lfs_files=orphan_blobs,
            repo_type="dataset",
        )
        deleted = len(orphan_blobs)
    elif delete:
        # Nothing to delete — surface a 0 instead of None so callers
        # can distinguish "ran the delete path" from "dry-run".
        deleted = 0

    return {
        "repo_id": repo_id,
        "revision": rev,
        "total_lfs": len(lfs_blobs),
        "referenced": len(referenced),
        "orphans": orphan_oids,
        "deleted": deleted,
    }


def _confirm_delete() -> bool:
    """Ask for a literal ``DELETE`` on stdin. ``MAT_VIS_AUDIT_FORCE=1``
    skips the prompt for non-tty / scripted contexts."""
    if os.environ.get("MAT_VIS_AUDIT_FORCE") == "1":
        log.warning("MAT_VIS_AUDIT_FORCE=1: skipping interactive confirmation")
        return True
    try:
        reply = input("type DELETE to confirm: ")
    except EOFError:
        print(
            "no tty available; set MAT_VIS_AUDIT_FORCE=1 to bypass confirmation",
            file=sys.stderr,
        )
        return False
    return reply == "DELETE"
