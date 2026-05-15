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
    filter_ids: str = "",
    batch_max_bytes: int = 700 * 1024 * 1024,
    metrics_path: str | None = None,
    force_rebake: bool = False,
) -> list[str]:
    """Build the ``mat-vis-baker hf-bake`` argv list.

    Pulled out of ``MatVisCi.bake`` so the contract is unit-testable
    without spinning up a Dagger engine. The Dagger function is then
    a thin shell over this + container exec.

    Sentinel: ``limit <= 0`` drops ``--limit``. Sharding flags were
    retired with the legacy tar substrate (#189 / ADR-0012) — per-file
    bakes use pre-flight tree scan + batch commits as the resumability
    primitive instead of shard-N-of-K artifacts.

    #228: ``--batch-max-bytes`` is always emitted. The CLI defaults
    match this default, but emitting unconditionally keeps the Dagger
    surface explicit (and lets a workflow input override it).

    #263 phase C: ``--metrics-path`` is emitted only when explicitly
    set so the existing test surface (which never passes a metrics
    path) stays unchanged.
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
        "--batch-max-bytes",
        str(batch_max_bytes),
    ]
    if limit > 0:
        argv += ["--limit", str(limit)]
    if filter_ids:
        argv += ["--filter-ids", filter_ids]
    if dry_run:
        argv.append("--dry-run")
    if allow_prod:
        argv.append("--allow-prod")
    if metrics_path:
        argv += ["--metrics-path", metrics_path]
    if force_rebake:
        argv.append("--force-rebake")
    return argv
