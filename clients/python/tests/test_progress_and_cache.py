"""Tests for the joint #312 + #355 cache observability surface.

Covers:

- ``on_event`` callback dispatch on download (#312)
- ``ClientEvent`` taxonomy (kinds + fields)
- Ready-made reporters (silent / log / tty / mcp)
- ``cache_check()`` JSON shape (#355)
- ``cache_clear(stale_only=True)`` only removes orphan layouts
- Version-namespaced cache scope; orphan-layout detection at init
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mat_vis_client import MatVisClient
from mat_vis_client.progress import (
    ClientEvent,
    log_reporter,
    mcp_reporter,
    silent_reporter,
    tty_reporter,
)


# ── ClientEvent ───────────────────────────────────────────────────


class TestClientEvent:
    def test_minimal_construction(self):
        e = ClientEvent(kind="download_start", source="gpuopen")
        assert e.kind == "download_start"
        assert e.source == "gpuopen"
        assert e.material is None  # defaulted

    def test_str_repr_compact(self):
        e = ClientEvent(
            kind="download_start",
            source="gpuopen",
            material="Aluminum Brushed",
            channel="color",
            tier="1k",
            bytes_total=3_277_401,
        )
        s = str(e)
        assert "download_start" in s
        assert "gpuopen/Aluminum Brushed/color" in s
        assert "tier=1k" in s
        assert "size=3277401" in s

    def test_frozen(self):
        e = ClientEvent(kind="download_start")
        with pytest.raises(Exception):
            e.kind = "download_end"  # type: ignore[misc]


# ── reporters ─────────────────────────────────────────────────────


class TestSilentReporter:
    def test_drops_every_event(self):
        report = silent_reporter()
        # No raise, no return
        report(ClientEvent(kind="download_start"))
        report(ClientEvent(kind="cache_stale_detected"))


class TestLogReporter:
    def test_emits_log_line_at_specified_level(self, caplog):
        logger = logging.getLogger("mat-vis-test-logreporter")
        report = log_reporter(logger=logger, level=logging.WARNING)
        with caplog.at_level(logging.WARNING, logger=logger.name):
            report(ClientEvent(kind="download_start", source="gpuopen"))
        assert any("download_start" in r.message for r in caplog.records)


class TestTtyReporter:
    def test_falls_back_to_log_when_stream_not_tty(self, caplog):
        # io.StringIO has no isatty so tty_reporter falls back to log.
        stream = io.StringIO()
        report = tty_reporter(stream=stream)
        with caplog.at_level(logging.INFO, logger="mat-vis-client"):
            report(ClientEvent(kind="download_start", source="gpuopen"))
        # Stream wasn't written; log line was emitted.
        assert stream.getvalue() == ""
        assert any("download_start" in r.message for r in caplog.records)


class TestMcpReporter:
    def test_emits_dict_with_event_key(self):
        emitted = []
        report = mcp_reporter(emit=emitted.append)
        report(
            ClientEvent(
                kind="download_start",
                source="gpuopen",
                material="Aluminum Brushed",
                channel="color",
                bytes_total=1024,
            )
        )
        assert len(emitted) == 1
        e = emitted[0]
        assert e["event"] == "download_start"
        assert e["source"] == "gpuopen"
        assert e["material"] == "Aluminum Brushed"
        assert e["channel"] == "color"
        assert e["bytes_total"] == 1024

    def test_omits_none_fields(self):
        emitted = []
        report = mcp_reporter(emit=emitted.append)
        report(ClientEvent(kind="download_start", source="gpuopen"))
        e = emitted[0]
        assert "material" not in e  # was None
        assert "tier" not in e  # was None

    def test_emits_detail_when_present(self):
        emitted = []
        report = mcp_reporter(emit=emitted.append)
        report(
            ClientEvent(
                kind="cache_stale_detected",
                detail={"layouts": ["latest", "v0.5"], "bytes": 1024},
            )
        )
        e = emitted[0]
        assert e["detail"] == {"layouts": ["latest", "v0.5"], "bytes": 1024}


# ── on_event integration with MatVisClient ──────────────────────


class TestClientOnEventInit:
    def test_silent_default_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(cache_dir=Path(tmp), tag="v2026.04.2")
            assert client._on_event is None

    def test_legacy_layout_emits_cache_stale_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Plant a legacy layout at the OLD path (no v0.X segment).
            (Path(tmp) / "v2026.04.0").mkdir()
            (Path(tmp) / "v2026.04.0" / "marker").write_text("x")
            events = []
            MatVisClient(
                cache_dir=Path(tmp),
                tag="v2026.04.2",
                on_event=events.append,
            )
            kinds = [e.kind for e in events]
            assert "cache_stale_detected" in kinds
            stale = next(e for e in events if e.kind == "cache_stale_detected")
            assert "v2026.04.0" in stale.detail["layouts"]
            assert stale.detail["bytes"] > 0

    def test_no_legacy_layout_no_stale_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            MatVisClient(
                cache_dir=Path(tmp),
                tag="v2026.04.2",
                on_event=events.append,
            )
            kinds = [e.kind for e in events]
            assert "cache_stale_detected" not in kinds


# ── cache_check ───────────────────────────────────────────────────


class TestCacheCheck:
    def test_returns_json_serializable_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(cache_dir=Path(tmp), tag="v2026.04.2")
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(b'{"sources":{}}', '"e"'),
            ):
                status = client.cache_check()
            # JSON-clean primitives only.
            json.dumps(status)
            assert "manifest_in_sync" in status
            assert "indexes_in_sync" in status
            assert "schema_version" in status
            assert "pinned_tag" in status
            assert "stale_layouts" in status
            assert "stale_bytes" in status
            assert "recommend" in status

    def test_recommend_clear_stale_when_orphans_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "v2026.04.0").mkdir()  # orphan
            client = MatVisClient(cache_dir=Path(tmp), tag="v2026.04.2")
            # Mock manifest in-sync so the only signal is stale layouts.
            client._cache_write_manifest(b'{"sources":{}}', etag='"e"')
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(None, '"e"'),  # 304
            ):
                status = client.cache_check()
            assert "v2026.04.0" in status["stale_layouts"]
            assert status["recommend"] == "clear-stale"

    def test_recommend_ok_when_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(cache_dir=Path(tmp), tag="v2026.04.2")
            client._cache_write_manifest(b'{"sources":{}}', etag='"e"')
            with patch(
                "mat_vis_client.client._get_with_etag",
                return_value=(None, '"e"'),
            ):
                status = client.cache_check()
            assert status["recommend"] == "ok"


# ── cache_clear stale_only ────────────────────────────────────────


class TestCacheClearStaleOnly:
    def test_keeps_current_version_drops_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Plant orphan + current.
            orphan = Path(tmp) / "v2026.04.0"
            orphan.mkdir()
            (orphan / "ghost").write_text("x" * 1024)

            # Plant current (write something into the v0.X/<tag>/ scope).
            client = MatVisClient(cache_dir=Path(tmp), tag="v2026.04.2")
            (client._cache_scope).mkdir(parents=True, exist_ok=True)
            (client._cache_scope / "real").write_text("y" * 2048)

            freed = client.cache_clear(stale_only=True)
            assert freed >= 1024
            assert not orphan.exists()
            # Current scope still there.
            assert client._cache_scope.exists()
            assert (client._cache_scope / "real").exists()

    def test_default_clear_removes_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(cache_dir=Path(tmp), tag="v2026.04.2")
            client._cache_scope.mkdir(parents=True, exist_ok=True)
            (client._cache_scope / "x").write_text("data")
            freed = client.cache_clear()  # stale_only=False by default
            assert freed > 0
            assert not Path(tmp).exists() or not any(Path(tmp).iterdir())


# ── reviewer-driven follow-up: defaults reach silent consumers ────


class TestGetClientDefaults:
    """Independent reviewer of #358 caught: bernhard reaches mat-vis
    via pymat's singleton, never wiring on_event= himself, so the
    silent-by-default behavior left him exactly as broken as before
    #287's silent log.info. Pin the post-fix behavior here so a
    future refactor can't quietly remove it."""

    def test_get_client_singleton_wires_tty_reporter_by_default(self, monkeypatch):
        """get_client() must produce a client with on_event set —
        otherwise bernhard's #312 repro stays broken even after #358."""
        # Reset singleton so the test runs with a fresh init.
        import mat_vis_client

        monkeypatch.setattr(mat_vis_client, "_client", None)
        monkeypatch.delenv("MAT_VIS_NO_PROGRESS", raising=False)

        c = mat_vis_client.get_client()
        assert c._on_event is not None, (
            "get_client() must wire on_event= by default — silent default leaves "
            "bernhard's #312 repro broken (his pymat singleton path can't see events)"
        )

    def test_mat_vis_no_progress_env_var_opts_out(self, monkeypatch):
        """MAT_VIS_NO_PROGRESS=1 forces silent_reporter — the explicit
        opt-out for CI/scripted contexts that don't want any output."""
        import mat_vis_client

        monkeypatch.setattr(mat_vis_client, "_client", None)
        monkeypatch.setenv("MAT_VIS_NO_PROGRESS", "1")

        c = mat_vis_client.get_client()
        assert c._on_event is not None
        # silent_reporter is a lambda — calling it must be a no-op.
        # (Can't check identity directly because each call returns a
        # fresh lambda; assert behavior instead.)
        c._on_event(ClientEvent(kind="download_start"))  # no exception, no output


class TestLegacyLayoutAlsoLogsWarning:
    """Reviewer caught: silent consumers (no on_event=) get NO signal
    that orphan layouts exist. WARN-level log line ensures the warning
    reaches default-configured consumers — root logger sits at WARNING
    by default."""

    def test_orphan_layout_emits_log_warning_even_without_on_event(self, caplog, tmp_path):
        # Plant orphan layout.
        orphan = tmp_path / "v2026.04.0"
        orphan.mkdir()
        (orphan / "marker").write_text("x" * 1024)

        with caplog.at_level(logging.WARNING, logger="mat-vis-client"):
            # No on_event= — the silent-default path bernhard hits
            # via pymat's singleton.
            MatVisClient(cache_dir=tmp_path, tag="v2026.04.2")

        # The WARN line includes the layout count + cache_dir in the
        # message + args. Asserting the warning fired at all (with
        # "legacy layout" content) is sufficient — the path printed is
        # tmp_path which contains the orphan dir name implicitly.
        assert any("legacy layout" in r.getMessage() for r in caplog.records), (
            "WARN line should fire even when on_event=None"
        )

    def test_orphan_layout_emits_both_log_and_event_when_on_event_wired(self, caplog, tmp_path):
        """When a reporter IS wired, both surfaces fire — log line for
        passive observers, event for active consumers (tqdm UI, etc.)."""
        orphan = tmp_path / "v2026.04.0"
        orphan.mkdir()
        (orphan / "marker").write_text("x" * 1024)

        events = []
        with caplog.at_level(logging.WARNING, logger="mat-vis-client"):
            MatVisClient(cache_dir=tmp_path, tag="v2026.04.2", on_event=events.append)

        # Event fired
        assert any(e.kind == "cache_stale_detected" for e in events)
        # Log line fired
        assert any("legacy layout" in r.message for r in caplog.records)
