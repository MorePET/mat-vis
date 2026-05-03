"""Regression gate for the Dagger ``bake()`` argv contract.

The Dagger module lives outside the main package, so ``_bake_cli.py``
holds the pure-Python helper that builds the ``mat-vis-baker hf-bake``
argv list. Importing it via file path keeps this test runnable in the
default ``uv run pytest`` invocation, no Dagger engine required.

Per-file substrate (ADR-0012, #189): the ``--legacy-tar`` and shard
flags were retired alongside the tar code. Today's argv contract is
the simpler {source, tier, work_dir, --release-tag, --repo-id, --offset,
--batch-size, [--limit], [--dry-run], [--allow-prod]} surface.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BAKE_CLI_PATH = (
    Path(__file__).resolve().parent.parent / ".dagger" / "src" / "mat_vis_ci" / "_bake_cli.py"
)


@pytest.fixture(scope="module")
def bake_argv():
    spec = importlib.util.spec_from_file_location("dagger_bake_cli", BAKE_CLI_PATH)
    if spec is None or spec.loader is None:
        pytest.skip("could not locate .dagger/src/mat_vis_ci/_bake_cli.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.bake_argv


class TestBakeArgvContract:
    """Locks the CLI argv shape so future refactors don't silently
    change the surface ``dagger call bake`` produces."""

    def test_default_per_file_no_optional_flags(self, bake_argv):
        """Per-file (default), no limit, scratch repo: no optional flags fire."""
        argv = bake_argv(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            repo_id="gerchowl/mat-vis-tst",
            offset=0,
            batch_size=50,
            limit=0,
            dry_run=False,
            allow_prod=False,
        )
        assert argv[:6] == ["uv", "run", "mat-vis-baker", "hf-bake", "polyhaven", "1k"]
        assert "/tmp/bake" in argv
        assert "--release-tag" in argv and "v0.0.0-test" in argv
        assert "--repo-id" in argv and "gerchowl/mat-vis-tst" in argv
        assert "--batch-size" in argv and "50" in argv
        # #228: --batch-max-bytes always emitted; default tracks the
        # 700 MiB ceiling.
        assert "--batch-max-bytes" in argv
        idx = argv.index("--batch-max-bytes")
        assert argv[idx + 1] == str(700 * 1024 * 1024)
        # No optional flags fired.
        for flag in ("--limit", "--dry-run", "--allow-prod"):
            assert flag not in argv, f"unexpected {flag} in default argv"

    def test_batch_max_bytes_propagates(self, bake_argv):
        """Custom byte ceiling makes it through to the CLI argv (#228)."""
        argv = bake_argv(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            repo_id="gerchowl/mat-vis-tst",
            offset=0,
            batch_size=300,
            batch_max_bytes=512 * 1024 * 1024,
            limit=0,
            dry_run=False,
            allow_prod=False,
        )
        idx = argv.index("--batch-max-bytes")
        assert argv[idx + 1] == str(512 * 1024 * 1024)

    def test_limit_emits_when_positive(self, bake_argv):
        argv = bake_argv(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            repo_id="gerchowl/mat-vis-tst",
            offset=0,
            batch_size=2,
            limit=2,
            dry_run=False,
            allow_prod=False,
        )
        i = argv.index("--limit")
        assert argv[i + 1] == "2"

    def test_dry_run_and_allow_prod_propagate(self, bake_argv):
        argv = bake_argv(
            source="ambientcg",
            tier="2k",
            release_tag="v2026.05.0",
            repo_id="gerchowl/mat-vis",
            offset=0,
            batch_size=50,
            limit=0,
            dry_run=True,
            allow_prod=True,
        )
        assert "--dry-run" in argv
        assert "--allow-prod" in argv
