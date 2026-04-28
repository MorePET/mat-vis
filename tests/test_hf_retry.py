"""Unit tests for the HF 429 retry/backoff helper (#225).

The helper wraps ``api.create_commit`` with bounded retries on 429,
parses ``Retry-After`` from the header (with a body-text fallback and
exponential-backoff fallback after that), and emits one structured
``bake_throttle`` log line per retry. Every non-429 exception
re-raises unchanged so the existing 412 (CAS precondition) retry loop
in :mod:`mat_vis_baker.hf_bake_per_file` keeps working.

These tests must NOT touch the real HF API — phase-4 derives are
running against ``gerchowl/mat-vis-tst@v0.0.0-phase3`` and any traffic
would fight for the very rate budget this module exists to defend.
Pure mocks throughout.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from huggingface_hub.errors import HfHubHTTPError

from mat_vis_baker import hf_retry
from mat_vis_baker.hf_retry import _create_commit_with_backoff


_THROTTLE_RE = re.compile(
    r"^bake_throttle source=(?P<source>\S+) "
    r"retry=(?P<attempt>\d+)/(?P<max>\d+) "
    r"reason=429 wait_s=(?P<wait>[\d.]+) "
    r"elapsed=(?P<elapsed>\d+m\d+s)$"
)


def _make_429(retry_after: str | None = "30", body: str = "") -> HfHubHTTPError:
    """Build a fake 429 with the same attribute surface
    huggingface_hub's real HfHubHTTPError exposes — status_code,
    headers (dict-like), text. SimpleNamespace is enough; the helper
    only does ``.get`` on headers and reads ``.text`` defensively.

    HfHubHTTPError.__init__ touches ``response.headers.get`` for a few
    diagnostic IDs and ``response.request`` — supply both as
    no-ops so the constructor doesn't choke."""
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    response = SimpleNamespace(
        status_code=429,
        headers=headers,
        text=body,
        request=SimpleNamespace(method="POST", url="https://huggingface.co/test"),
    )
    return HfHubHTTPError("429 Too Many Requests", response=response)  # type: ignore[arg-type]


def _make_412() -> Exception:
    """A 412 CAS precondition surfaces as a plain Exception with the
    string ``"412 Precondition Failed"`` in the existing baker code —
    the CAS loop matches on string content, not type. Use the same
    shape so the test reflects production behaviour."""
    return Exception("412 Precondition Failed: parent_commit mismatch")


# ── basic 429 retry path ─────────────────────────────────────────


def test_retry_then_succeed_returns_commit(capsys):
    """One 429 → sleep → second call returns the commit."""
    api = MagicMock()
    sleeps: list[float] = []
    sentinel = SimpleNamespace(oid="abc123")
    api.create_commit.side_effect = [_make_429(retry_after="30"), sentinel]

    result = _create_commit_with_backoff(
        api,
        source="ambientcg",
        _sleep=sleeps.append,
        repo_id="x/y",
    )

    assert result is sentinel
    assert api.create_commit.call_count == 2
    assert sleeps == [30.0]
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    m = _THROTTLE_RE.match(out[0])
    assert m is not None, f"throttle line did not match contract: {out[0]!r}"
    assert m.group("source") == "ambientcg"
    assert m.group("attempt") == "1"
    assert m.group("wait") == "30.0"


def test_retry_after_header_http_date_is_parsed(capsys):
    """``Retry-After`` may be an HTTP-date instead of seconds; the
    helper must compute the delta from now and sleep that long."""
    import email.utils
    import time

    future = email.utils.formatdate(time.time() + 45, usegmt=True)
    api = MagicMock()
    sleeps: list[float] = []
    sentinel = SimpleNamespace(oid="abc")
    api.create_commit.side_effect = [_make_429(retry_after=future), sentinel]

    result = _create_commit_with_backoff(api, source="polyhaven", _sleep=sleeps.append)
    assert result is sentinel
    # Allow a few seconds slack for clock drift in CI.
    assert len(sleeps) == 1
    assert 30.0 <= sleeps[0] <= 60.0


def test_body_fallback_when_header_missing(capsys):
    """Header absent but body says ``"Retry after 30 seconds"`` → use
    the body value (matches the real #225 stack trace shape)."""
    api = MagicMock()
    sleeps: list[float] = []
    sentinel = SimpleNamespace(oid="abc")
    api.create_commit.side_effect = [
        _make_429(
            retry_after=None,
            body=(
                "You have exceeded the rate limit for repository commits "
                "(128 per hour). Retry after 30 seconds (647/1000 requests "
                "remaining in current 300s window)."
            ),
        ),
        sentinel,
    ]

    result = _create_commit_with_backoff(api, source="gpuopen", _sleep=sleeps.append)
    assert result is sentinel
    assert sleeps == [30.0]


def test_exponential_backoff_when_no_hint(capsys, monkeypatch):
    """Both header and body silent → exponential backoff with jitter,
    floored at 30s and capped at 120s. Pin random for determinism."""
    monkeypatch.setattr(hf_retry.random, "random", lambda: 0.5)  # zero jitter

    api = MagicMock()
    sleeps: list[float] = []
    sentinel = SimpleNamespace(oid="abc")
    api.create_commit.side_effect = [
        _make_429(retry_after=None, body=""),
        sentinel,
    ]

    result = _create_commit_with_backoff(api, source="ambientcg", _sleep=sleeps.append)
    assert result is sentinel
    assert len(sleeps) == 1
    assert sleeps[0] == 30.0  # floor


# ── exhaustion path ──────────────────────────────────────────────


def test_429_forever_raises_after_max_retries(capsys):
    """``max_retries`` consecutive 429s → re-raise the original
    HfHubHTTPError so the caller sees the real HF response."""
    api = MagicMock()
    sleeps: list[float] = []
    api.create_commit.side_effect = [_make_429(retry_after="30") for _ in range(10)]

    with pytest.raises(HfHubHTTPError):
        _create_commit_with_backoff(
            api,
            source="polyhaven",
            max_retries=3,
            _sleep=sleeps.append,
        )

    # 3 attempts; sleeps only happen between retries → 2 sleeps emitted.
    assert api.create_commit.call_count == 3
    assert len(sleeps) == 2
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    for i, line in enumerate(out, start=1):
        m = _THROTTLE_RE.match(line)
        assert m is not None, line
        assert m.group("attempt") == str(i)
        assert m.group("max") == "3"


# ── non-429 propagation path ─────────────────────────────────────


def test_412_reraises_immediately_no_retry(capsys):
    """412 from create_commit must re-raise unchanged — the existing
    CAS retry loop in ``bake_one_per_file`` matches on string content,
    so the helper must let it through to that handler."""
    api = MagicMock()
    sleeps: list[float] = []
    exc = _make_412()
    api.create_commit.side_effect = exc

    with pytest.raises(Exception) as excinfo:
        _create_commit_with_backoff(api, source="ambientcg", _sleep=sleeps.append)

    assert excinfo.value is exc
    assert api.create_commit.call_count == 1
    assert sleeps == []
    assert capsys.readouterr().out == ""


def test_other_HfHubHTTPError_status_codes_reraise(capsys):
    """A 500 / 401 / etc. surfacing as HfHubHTTPError must NOT be
    retried — only 429 is throttle. Authentication failures retried
    silently would mask real config bugs."""
    api = MagicMock()
    response = SimpleNamespace(
        status_code=500,
        headers={},
        text="server boom",
        request=SimpleNamespace(method="POST", url="https://huggingface.co/test"),
    )
    err = HfHubHTTPError("500", response=response)  # type: ignore[arg-type]
    api.create_commit.side_effect = err

    with pytest.raises(HfHubHTTPError):
        _create_commit_with_backoff(api, source="x", _sleep=lambda _w: None)
    assert api.create_commit.call_count == 1


# ── log-line format contract ─────────────────────────────────────


def test_throttle_line_matches_progress_module_format(capsys):
    """Format mirrors :mod:`mat_vis_baker.progress` — single line,
    leading prefix token, ``key=value`` pairs, no trailing newline
    fluff. Downstream log scrapers grep on this exact shape."""
    api = MagicMock()
    sentinel = SimpleNamespace(oid="abc")
    api.create_commit.side_effect = [
        _make_429(retry_after="30"),
        _make_429(retry_after="30"),
        sentinel,
    ]

    _create_commit_with_backoff(api, source="ambientcg", _sleep=lambda _w: None)

    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    for line in out:
        assert _THROTTLE_RE.match(line) is not None, line


def test_default_source_is_unknown(capsys):
    """Helper accepts ``source`` as optional; absent → ``unknown`` so
    callers that haven't been updated yet still emit valid lines."""
    api = MagicMock()
    sentinel = SimpleNamespace(oid="abc")
    api.create_commit.side_effect = [_make_429(retry_after="30"), sentinel]

    _create_commit_with_backoff(api, _sleep=lambda _w: None)

    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    m = _THROTTLE_RE.match(out[0])
    assert m is not None
    assert m.group("source") == "unknown"


# ── 412 CAS loop integration ─────────────────────────────────────


def test_cas_412_loop_still_works_with_helper_wrapped_call(capsys):
    """Smoke test: when the helper wraps a create_commit that the CAS
    loop in ``bake_one_per_file`` calls, a 412 still bubbles up and
    the loop's ``"412"/"precondition"/"parent_commit"`` matcher fires.

    Don't spin up the whole baker — just simulate the call chain so a
    refactor that accidentally swallows 412 in the helper trips this.
    """
    api = MagicMock()
    api.create_commit.side_effect = _make_412()

    # Re-implement the CAS matcher inline (matches lines 492-509 of
    # hf_bake_per_file.py at HEAD). If the helper ever swallows 412,
    # this branch will never fire and the assert will trip.
    matched_412 = False
    try:
        _create_commit_with_backoff(api, source="ambientcg", _sleep=lambda _w: None)
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        matched_412 = "412" in msg or "precondition" in msg or "parent_commit" in msg

    assert matched_412, "412 must propagate through the helper to the CAS loop"
    assert api.create_commit.call_count == 1
