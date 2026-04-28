"""Format contract for the structured progress lines (#217).

Downstream parsers (dashboards, GH Actions log scrapers) grep for the
exact prefix tokens and key=value layout these lines emit. A regression
that re-orders fields or renames a key would silently break them, so
the format is asserted here against a fixed regex. Update both the
producer in ``mat_vis_baker.progress`` and these tests in lock-step
when the contract changes.
"""

from __future__ import annotations

import re

import pytest

from mat_vis_baker.progress import ProgressTracker, emit_bake_plan


_BAKE_PLAN_RE = re.compile(
    r"^bake_plan source=(?P<source>\S+) tier=(?P<tier>\S+) "
    r"total_materials=(?P<total>\d+) expected_files≈(?P<expected>\d+) "
    r"release_tag=(?P<tag>\S+) repo=(?P<repo>\S+)$"
)

_BAKE_PROGRESS_RE = re.compile(
    r"^bake_progress source=(?P<source>\S+) "
    r"done=(?P<done>\d+)/(?P<total>\d+) \((?P<pct>\d+)%\) "
    r"batches=(?P<batches>\d+) commits=(?P<commits>\d+) "
    r"bytes_pushed=(?P<bytes>[\d.]+)MiB "
    r"elapsed=(?P<elapsed>\d+m\d+s) "
    r"rate=(?P<rate>[\d.]+)mat/min eta=(?P<eta>\d+)m$"
)

_DERIVE_PLAN_RE = re.compile(
    r"^derive_plan source=\S+ tier=\S+ total_materials=\d+ "
    r"expected_files≈\d+ release_tag=\S+ repo=\S+$"
)

_DERIVE_PROGRESS_RE = re.compile(
    r"^derive_progress source=\S+ done=\d+/\d+ \(\d+%\) "
    r"batches=\d+ commits=\d+ bytes_pushed=[\d.]+MiB "
    r"elapsed=\d+m\d+s rate=[\d.]+mat/min eta=\d+m$"
)


class TestBakePlanFormat:
    def test_emit_bake_plan_matches_contract(self, capsys):
        emit_bake_plan(
            source="polyhaven",
            tier="1k",
            total_materials=1234,
            expected_files=8638,
            release_tag="v0.0.0-phase2",
            repo_id="gerchowl/mat-vis-tst",
            kind="bake",
        )
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1, out
        m = _BAKE_PLAN_RE.match(out[0])
        assert m is not None, f"line did not match contract: {out[0]!r}"
        assert m.group("source") == "polyhaven"
        assert m.group("tier") == "1k"
        assert m.group("total") == "1234"
        assert m.group("expected") == "8638"
        assert m.group("tag") == "v0.0.0-phase2"
        assert m.group("repo") == "gerchowl/mat-vis-tst"

    def test_derive_plan_uses_derive_prefix(self, capsys):
        """``kind=derive`` flips the prefix token but keeps every other
        field — same regex shape, different prefix."""
        emit_bake_plan(
            source="polyhaven",
            tier="512",
            total_materials=10,
            expected_files=70,
            release_tag="v0.0.0-test",
            repo_id="gerchowl/mat-vis-tst",
            kind="derive",
        )
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1, out
        assert _DERIVE_PLAN_RE.match(out[0]) is not None, out[0]


class TestBakeProgressFormat:
    def test_emit_progress_matches_contract(self, capsys):
        tracker = ProgressTracker(
            source="polyhaven",
            tier="1k",
            total_materials=100,
            kind="bake",
        )
        # Two batches so the rolling rate has two samples and renders
        # a non-trivial number.
        tracker.record_batch(materials=10, bytes_added=2 * 1024 * 1024)
        tracker.record_batch(materials=15, bytes_added=3 * 1024 * 1024)
        tracker.emit_progress()

        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1, out
        m = _BAKE_PROGRESS_RE.match(out[0])
        assert m is not None, f"line did not match contract: {out[0]!r}"
        assert m.group("source") == "polyhaven"
        assert m.group("done") == "25"
        assert m.group("total") == "100"
        assert m.group("pct") == "25"
        assert m.group("batches") == "2"
        assert m.group("commits") == "2"
        # 5 MiB total pushed across the two batches.
        assert float(m.group("bytes")) == pytest.approx(5.0, abs=0.1)

    def test_derive_progress_uses_derive_prefix(self, capsys):
        tracker = ProgressTracker(
            source="polyhaven",
            tier="512",
            total_materials=20,
            kind="derive",
        )
        tracker.record_batch(materials=5, bytes_added=1024)
        tracker.record_batch(materials=5, bytes_added=1024)
        tracker.emit_progress()
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1, out
        assert _DERIVE_PROGRESS_RE.match(out[0]) is not None, out[0]

    def test_progress_pct_clamps_when_done_exceeds_total(self, capsys):
        """Defensive: if a fetcher returns more materials than the
        discover() count predicted, percent should still be a finite
        integer (we don't clamp; it can read >100 — that's a useful
        signal). The line MUST still parse."""
        tracker = ProgressTracker(
            source="polyhaven",
            tier="1k",
            total_materials=10,
            kind="bake",
        )
        tracker.record_batch(materials=15, bytes_added=0)
        tracker.record_batch(materials=0, bytes_added=0)
        tracker.emit_progress()
        out = capsys.readouterr().out.strip().splitlines()
        assert _BAKE_PROGRESS_RE.match(out[0]) is not None, out[0]

    def test_progress_with_zero_total_does_not_divide_by_zero(self, capsys):
        """When discover() failed and total_materials=0, the line still
        emits with pct=0. Prevents ZeroDivisionError from killing the
        bake just because the upstream catalog probe blipped."""
        tracker = ProgressTracker(
            source="polyhaven",
            tier="1k",
            total_materials=0,
            kind="bake",
        )
        tracker.record_batch(materials=5, bytes_added=0)
        tracker.record_batch(materials=5, bytes_added=0)
        tracker.emit_progress()
        out = capsys.readouterr().out.strip().splitlines()
        assert _BAKE_PROGRESS_RE.match(out[0]) is not None, out[0]


class TestBakeDoneFormat:
    _BAKE_DONE_RE = re.compile(
        r"^bake_done source=\S+ tier=\S+ ok=\d+ failed=\d+ "
        r"skipped_preflight=\d+ elapsed=\d+m\d+s$"
    )
    _DERIVE_DONE_RE = re.compile(
        r"^derive_done source=\S+ tier=\S+ ok=\d+ failed=\d+ "
        r"skipped_preflight=\d+ elapsed=\d+m\d+s$"
    )

    def test_bake_done_matches_contract(self, capsys):
        tracker = ProgressTracker(source="polyhaven", tier="1k", total_materials=10, kind="bake")
        tracker.emit_done(ok=8, failed=1, skipped_preflight=1)
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1, out
        assert self._BAKE_DONE_RE.match(out[0]) is not None, out[0]

    def test_derive_done_matches_contract(self, capsys):
        tracker = ProgressTracker(source="polyhaven", tier="512", total_materials=10, kind="derive")
        tracker.emit_done(ok=10, failed=0, skipped_preflight=0)
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1, out
        assert self._DERIVE_DONE_RE.match(out[0]) is not None, out[0]


class TestRollingRate:
    """The 3-batch rolling window is the load-bearing detail that keeps
    first-batch warm-up bias out of the rate. Test it directly so a
    refactor that switches to a per-run average is caught."""

    def test_rate_uses_last_three_batches_only(self):
        import time as _time

        tracker = ProgressTracker(source="polyhaven", tier="1k", total_materials=1000, kind="bake")
        # Backdate the first batch by 10 minutes — the rolling window
        # should drop it once we add three more.
        tracker._window.append((_time.monotonic() - 600.0, 1))
        tracker.record_batch(materials=10, bytes_added=0)
        tracker.record_batch(materials=10, bytes_added=0)
        tracker.record_batch(materials=10, bytes_added=0)
        # Window now holds the three recent batches; the ancient one
        # has been evicted by maxlen=3. Rate must be high (recent
        # batches are sub-second apart in this test).
        rate = tracker._rolling_rate_per_min()
        assert rate > 100.0, f"rolling rate should reflect recent fast batches only, got {rate:.1f}"
