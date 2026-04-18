"""Tests for the --category / --dry-run flags on ``mat-vis-baker all``.

The filter lives inline in ``cmd_all``'s streaming loop — hard to unit-test
end-to-end without network + disk. These tests cover the two behaviors
that *can* be tested cleanly:

- argparse accepts valid categories, rejects invalid ones
- the list-comprehension filter does what the docstring says when handed
  a mixed batch of MaterialRecords
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from mat_vis_baker.common import CANONICAL_CATEGORIES, MaterialRecord


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mat_vis_baker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


class TestCategoryArgParsing:
    def test_valid_category_accepted(self) -> None:
        # --help short-circuits before any bake work; we just need argparse
        # to not reject the choice.
        r = _run_cli("all", "ambientcg", "1k", "/tmp/nope", "--category", "metal", "--help")
        assert r.returncode == 0, r.stderr

    def test_invalid_category_rejected(self) -> None:
        r = _run_cli("all", "ambientcg", "1k", "/tmp/nope", "--category", "not-a-real-cat")
        assert r.returncode != 0
        assert "invalid choice" in (r.stderr + r.stdout)

    def test_all_canonical_categories_valid(self) -> None:
        """Argparse's choices= should track the CANONICAL_CATEGORIES set.
        If someone adds a new category to the enum, the CLI must accept it
        without a separate edit."""
        for cat in sorted(CANONICAL_CATEGORIES):
            r = _run_cli("all", "ambientcg", "1k", "/tmp/nope", "--category", cat, "--help")
            assert r.returncode == 0, f"{cat!r} rejected: {r.stderr}"

    def test_dry_run_accepted(self) -> None:
        r = _run_cli("all", "ambientcg", "1k", "/tmp/nope", "--dry-run", "--help")
        assert r.returncode == 0, r.stderr


class TestCategoryFilterLogic:
    """Exercise the exact filter expression used in cmd_all (line ~251)."""

    @pytest.fixture
    def mixed_batch(self) -> list[MaterialRecord]:
        def rec(mid: str, cat: str) -> MaterialRecord:
            return MaterialRecord(
                id=mid,
                source="ambientcg",
                name=mid,
                category=cat,
                tags=[],
                source_url="",
                source_license="CC0-1.0",
                last_updated="2026-04-18",
                available_tiers=["1k"],
                maps=["color"],
                texture_paths={},
            )

        return [
            rec("Rock001", "stone"),
            rec("Metal001", "metal"),
            rec("Rock002", "stone"),
            rec("Wood001", "wood"),
            rec("Stuff001", "other"),
        ]

    def test_filter_keeps_only_target_category(self, mixed_batch):
        target = "stone"
        kept = [rec for rec in mixed_batch if rec.category == target]
        assert {r.id for r in kept} == {"Rock001", "Rock002"}
        assert all(r.category == target for r in kept)

    def test_filter_empty_on_nonmatching_target(self, mixed_batch):
        kept = [rec for rec in mixed_batch if rec.category == "glass"]
        assert kept == []

    def test_filter_is_identity_when_target_is_none(self, mixed_batch):
        # category_filter=None in cmd_all means "no filter" — batch passes through.
        category_filter = None
        kept = (
            [rec for rec in mixed_batch if rec.category == category_filter]
            if category_filter
            else mixed_batch
        )
        assert kept is mixed_batch
