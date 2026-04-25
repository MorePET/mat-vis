"""Pure-Python helpers for the Dagger ``bake()`` op.

Lives in its own module so it can be unit-tested without Dagger Python
SDK in the venv. ``main.py`` (which decorates ``MatVisCi`` with Dagger's
runtime types) imports from here; the test imports here directly.
"""

from __future__ import annotations


def bake_argv(
    *,
    source: str,
    tier: str,
    release_tag: str,
    repo_id: str,
    offset: int,
    batch_size: int,
    limit: int,
    dry_run: bool,
    allow_prod: bool,
    legacy_tar: bool,
    shard_index: int,
    shard_total: int,
) -> list[str]:
    """Build the ``mat-vis-baker hf-bake`` argv list.

    Pulled out of ``MatVisCi.bake`` so the contract is unit-testable
    without spinning up a Dagger engine. The Dagger function is then
    a thin shell over this + container exec.

    Sentinels:
      - ``limit <= 0`` drops ``--limit``.
      - Both ``shard_index < 0`` and ``shard_total < 0`` drops both
        shard flags. Sharding is a no-op under per-file substrate
        (#184 / ADR-0012); passthrough kept for ``legacy_tar=True``
        callers only.
    """
    argv: list[str] = [
        "uv",
        "run",
        "mat-vis-baker",
        "hf-bake",
        source,
        tier,
        "/tmp/bake",
        "--release-tag",
        release_tag,
        "--repo-id",
        repo_id,
        "--offset",
        str(offset),
        "--batch-size",
        str(batch_size),
    ]
    if limit > 0:
        argv += ["--limit", str(limit)]
    if dry_run:
        argv.append("--dry-run")
    if allow_prod:
        argv.append("--allow-prod")
    if legacy_tar:
        argv.append("--legacy-tar")
    if shard_index >= 0 and shard_total >= 0:
        argv += ["--shard-index", str(shard_index), "--shard-total", str(shard_total)]
    return argv
