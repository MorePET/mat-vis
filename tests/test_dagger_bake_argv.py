"""Regression gate for the Dagger ``bake()`` argv contract (#185).

The Dagger module lives outside the main package, so ``_bake_cli.py``
holds the pure-Python helper that builds the ``mat-vis-baker hf-bake``
argv list. Importing it via file path keeps this test runnable in the
default ``uv run pytest`` invocation, no Dagger engine required.
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

    def test_default_per_file_no_shard(self, bake_argv):
        """Per-file (default), no shard, no limit, scratch repo."""
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
            legacy_tar=False,
            shard_index=-1,
            shard_total=-1,
        )
        assert argv[:6] == ["uv", "run", "mat-vis-baker", "hf-bake", "polyhaven", "1k"]
        assert "/tmp/bake" in argv
        assert "--release-tag" in argv and "v0.0.0-test" in argv
        assert "--repo-id" in argv and "gerchowl/mat-vis-tst" in argv
        assert "--batch-size" in argv and "50" in argv
        # No optional flags fired.
        for flag in (
            "--limit",
            "--dry-run",
            "--allow-prod",
            "--legacy-tar",
            "--shard-index",
            "--shard-total",
        ):
            assert flag not in argv, f"unexpected {flag} in default argv"

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
            legacy_tar=False,
            shard_index=-1,
            shard_total=-1,
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
            legacy_tar=False,
            shard_index=-1,
            shard_total=-1,
        )
        assert "--dry-run" in argv
        assert "--allow-prod" in argv

    def test_legacy_tar_passthrough(self, bake_argv):
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
            legacy_tar=True,
            shard_index=-1,
            shard_total=-1,
        )
        assert "--legacy-tar" in argv

    def test_shard_flags_pair(self, bake_argv):
        """Both shard flags or neither — never one alone."""
        argv_pair = bake_argv(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            repo_id="gerchowl/mat-vis-tst",
            offset=0,
            batch_size=50,
            limit=0,
            dry_run=False,
            allow_prod=False,
            legacy_tar=False,
            shard_index=2,
            shard_total=4,
        )
        assert "--shard-index" in argv_pair and "2" in argv_pair
        assert "--shard-total" in argv_pair and "4" in argv_pair

        argv_solo = bake_argv(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            repo_id="gerchowl/mat-vis-tst",
            offset=0,
            batch_size=50,
            limit=0,
            dry_run=False,
            allow_prod=False,
            legacy_tar=False,
            shard_index=2,
            shard_total=-1,
        )
        assert "--shard-index" not in argv_solo
        assert "--shard-total" not in argv_solo
