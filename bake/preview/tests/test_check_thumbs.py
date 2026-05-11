"""Tests for the duplicate-bytes detector + allow-list + sentinel gate (#385).

All synthetic — no network, no Playwright. Each test scaffolds a tmpdir
that mimics a bake output (`<src>/<material>/thumb.png`) and drives
``check_thumbs.check`` with explicit overrides.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image

# bake/ is not a Python package (no top-level __init__.py); load
# check_thumbs.py directly by file path to avoid forcing a package layout
# the rest of the repo doesn't use.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHECK_THUMBS_PATH = _REPO_ROOT / "bake" / "preview" / "utils" / "check_thumbs.py"
_spec = importlib.util.spec_from_file_location("check_thumbs", _CHECK_THUMBS_PATH)
assert _spec and _spec.loader
check_thumbs = importlib.util.module_from_spec(_spec)
sys.modules["check_thumbs"] = check_thumbs
_spec.loader.exec_module(check_thumbs)

SENTINEL_NAME = check_thumbs.SENTINEL_NAME
JSON_REPORT_NAME = check_thumbs.JSON_REPORT_NAME


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _solid_png(path: Path, color: tuple[int, int, int], size: int = 256) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (size, size), color).save(path, format="PNG", optimize=True)


def _drop_sentinel(out_dir: Path, totals: dict | None = None) -> None:
    payload = {
        "totals": totals or {"ok": 1, "skipped": 0, "errors": 0},
        "source_results": {},
        "baked_at": "2026-05-11T00:00:00Z",
    }
    (out_dir / SENTINEL_NAME).write_text(json.dumps(payload))


def _make_bake(
    tmp_path: Path,
    *,
    n_unique: int = 5,
    n_duplicates: int = 0,
    drop_sentinel: bool = True,
) -> Path:
    """Build a fake bake directory.

    - Writes ``n_unique`` distinct solid-color PNGs across one source.
    - If ``n_duplicates`` > 0, copies the *bytes* of the first PNG that
      many extra times under different material IDs (so md5 matches).
    """
    out = tmp_path / "thumbs"
    src = out / "fakesrc"
    paths: list[Path] = []
    for i in range(n_unique):
        # Distinct primary colour per i — keeps md5 different and keeps
        # the bytes well below the fingerprint distance threshold.
        p = src / f"mat_{i:03d}" / "thumb.png"
        _solid_png(p, ((i * 37) % 256, (i * 71) % 256, (i * 113) % 256))
        paths.append(p)

    if n_duplicates and paths:
        src_bytes = paths[0].read_bytes()
        for j in range(n_duplicates):
            dup = src / f"dup_{j:03d}" / "thumb.png"
            dup.parent.mkdir(parents=True, exist_ok=True)
            dup.write_bytes(src_bytes)

    if drop_sentinel:
        _drop_sentinel(out)
    return out


# ---------------------------------------------------------------------------
# Detector unit-test (no orchestration).
# ---------------------------------------------------------------------------


def test_detect_duplicate_renders_finds_byte_identical(tmp_path: Path) -> None:
    out = _make_bake(tmp_path, n_unique=5, n_duplicates=1)
    buckets = check_thumbs.detect_duplicate_renders(out)
    assert len(buckets) == 1
    digest, paths = buckets[0]
    assert len(digest) == 32
    assert len(paths) == 2  # the original + 1 duplicate


def test_detect_duplicate_renders_clean_returns_empty(tmp_path: Path) -> None:
    out = _make_bake(tmp_path, n_unique=6, n_duplicates=0)
    assert check_thumbs.detect_duplicate_renders(out) == []


def test_detect_duplicate_renders_size_prefilter_doesnt_drop_dupes(tmp_path: Path) -> None:
    """Same-size + same-bytes must still bucket; same-size + different bytes must NOT."""
    out = tmp_path / "thumbs"
    src = out / "fakesrc"
    # Two files with the same dimensions+colour → identical bytes → bucket.
    _solid_png(src / "a" / "thumb.png", (10, 20, 30))
    _solid_png(src / "b" / "thumb.png", (10, 20, 30))
    # Third file, same dimensions, different colour → distinct md5.
    _solid_png(src / "c" / "thumb.png", (200, 100, 50))
    _drop_sentinel(out)
    buckets = check_thumbs.detect_duplicate_renders(out)
    assert len(buckets) == 1
    assert len(buckets[0][1]) == 2


# ---------------------------------------------------------------------------
# Top-level check() — exit codes.
# ---------------------------------------------------------------------------


def test_check_clean_returns_exit_ok(tmp_path: Path) -> None:
    out = _make_bake(tmp_path, n_unique=6)
    json_out = tmp_path / "report.json"
    rc = check_thumbs.check(out, allowlist=[], json_out=json_out)
    assert rc == check_thumbs.EXIT_OK
    payload = json.loads(json_out.read_text())
    assert payload["exit_code"] == 0
    assert payload["duplicate_buckets"] == []
    assert payload["fingerprint_hits"] == []
    assert payload["checked"] == 6


def test_check_duplicate_returns_exit_duplicate(tmp_path: Path) -> None:
    out = _make_bake(tmp_path, n_unique=5, n_duplicates=1)
    json_out = tmp_path / "report.json"
    rc = check_thumbs.check(out, allowlist=[], json_out=json_out)
    assert rc == check_thumbs.EXIT_DUPLICATE
    payload = json.loads(json_out.read_text())
    assert payload["exit_code"] == 1
    assert len(payload["duplicate_buckets"]) == 1
    assert payload["duplicate_buckets"][0]["count"] == 2
    assert "suggested" in payload["duplicate_buckets"][0]


def test_check_fingerprint_hit_returns_exit_fingerprint(tmp_path: Path) -> None:
    """Drop the real ``blank_default_grey.png`` into the bake — fingerprint
    matcher must fire and win the exit-code priority."""
    out = _make_bake(tmp_path, n_unique=5, n_duplicates=0)
    fp_src = check_thumbs.ASSETS / "blank_default_grey.png"
    if not fp_src.exists():
        pytest.skip(f"fingerprint asset missing: {fp_src}")
    dst = out / "fakesrc" / "leaked_default" / "thumb.png"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fp_src, dst)
    json_out = tmp_path / "report.json"
    rc = check_thumbs.check(out, allowlist=[], json_out=json_out)
    assert rc == check_thumbs.EXIT_FINGERPRINT
    payload = json.loads(json_out.read_text())
    assert payload["exit_code"] == 2
    assert len(payload["fingerprint_hits"]) >= 1


def test_check_missing_sentinel_returns_exit_no_sentinel(tmp_path: Path) -> None:
    out = _make_bake(tmp_path, n_unique=3, n_duplicates=0, drop_sentinel=False)
    json_out = tmp_path / "report.json"
    rc = check_thumbs.check(out, allowlist=[], json_out=json_out)
    assert rc == check_thumbs.EXIT_NO_SENTINEL
    payload = json.loads(json_out.read_text())
    assert payload["exit_code"] == 3
    assert payload["exit_reason"] == "missing_sentinel"


def test_check_no_sentinel_can_be_disabled(tmp_path: Path) -> None:
    """``--no-sentinel`` (CLI) / ``require_sentinel=False`` lets ad-hoc
    audits run against legacy directories without the sentinel."""
    out = _make_bake(tmp_path, n_unique=4, n_duplicates=0, drop_sentinel=False)
    rc = check_thumbs.check(out, allowlist=[], require_sentinel=False)
    assert rc == check_thumbs.EXIT_OK


# ---------------------------------------------------------------------------
# Allow-list.
# ---------------------------------------------------------------------------


def test_check_allowlisted_duplicate_passes(tmp_path: Path) -> None:
    out = _make_bake(tmp_path, n_unique=5, n_duplicates=1)
    # Compute the duplicate's md5 the way the detector will see it.
    buckets = check_thumbs.detect_duplicate_renders(out)
    assert len(buckets) == 1
    dup_md5 = buckets[0][0]

    allowlist = [
        {
            "md5": dup_md5,
            "reason": "synthetic test fixture",
            "ticket": "#385",
            "until": "v9999.99.0",
        }
    ]
    json_out = tmp_path / "report.json"
    rc = check_thumbs.check(out, allowlist=allowlist, json_out=json_out)
    assert rc == check_thumbs.EXIT_OK
    payload = json.loads(json_out.read_text())
    assert payload["exit_code"] == 0
    assert len(payload["allowlisted"]) == 1
    assert payload["allowlisted"][0]["md5"] == dup_md5
    assert payload["allowlisted"][0]["ticket"] == "#385"


def test_stale_allowlist_entry_logs_warning_no_fail(tmp_path: Path, caplog) -> None:
    out = _make_bake(tmp_path, n_unique=4, n_duplicates=0)
    allowlist = [
        {
            "md5": "0" * 32,
            "reason": "expired",
            "ticket": "#999",
            "until": "v2026.01.0",
        }
    ]
    with caplog.at_level("WARNING", logger="check-thumbs"):
        rc = check_thumbs.check(
            out,
            allowlist=allowlist,
            release_tag="v2026.05.0",
        )
    assert rc == check_thumbs.EXIT_OK
    assert any("stale allow-list entry" in rec.message for rec in caplog.records)


def test_load_allowlist_validates_required_fields(tmp_path: Path) -> None:
    p = tmp_path / "allow.yml"
    p.write_text(
        "allowed:\n"
        '  - md5: "deadbeefdeadbeefdeadbeefdeadbeef"\n'
        '    reason: "missing ticket and until"\n'
    )
    with pytest.raises(check_thumbs.AllowlistError, match="missing required field"):
        check_thumbs.load_allowlist(p)


def test_load_allowlist_validates_md5_format(tmp_path: Path) -> None:
    p = tmp_path / "allow.yml"
    p.write_text(
        "allowed:\n"
        '  - md5: "NOT_A_HEX_DIGEST"\n'
        '    reason: "bad format"\n'
        '    ticket: "#1"\n'
        '    until: "v2026.05.0"\n'
    )
    with pytest.raises(check_thumbs.AllowlistError, match="32-char lowercase hex"):
        check_thumbs.load_allowlist(p)


def test_load_allowlist_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "allow.yml"
    p.write_text(
        "# leading comment\n"
        "allowed:\n"
        '  - md5: "0123456789abcdef0123456789abcdef"\n'
        '    reason: "two upstream entries are the same material"\n'
        '    ticket: "#999"\n'
        '    until: "v2026.05.0"\n'
    )
    entries = check_thumbs.load_allowlist(p)
    assert entries == [
        {
            "md5": "0123456789abcdef0123456789abcdef",
            "reason": "two upstream entries are the same material",
            "ticket": "#999",
            "until": "v2026.05.0",
        }
    ]


def test_load_allowlist_missing_file_returns_empty(tmp_path: Path) -> None:
    assert check_thumbs.load_allowlist(tmp_path / "nope.yml") == []


def test_committed_allowlist_loads(tmp_path: Path) -> None:
    """The shipped ``thumb_check_allow.yml`` must always parse — guard
    against `git mv` shenanigans during a refactor."""
    entries = check_thumbs.load_allowlist(check_thumbs.ALLOWLIST_PATH)
    # Empty list is fine; the shipped file is `allowed: []` until ops adds one.
    assert isinstance(entries, list)


# ---------------------------------------------------------------------------
# Suggested-investigation heuristic.
# ---------------------------------------------------------------------------


def test_suggest_root_cause_scalar_collapse(tmp_path: Path) -> None:
    """All N baked thumbs identical → "scalar collapse"."""
    out = _make_bake(tmp_path, n_unique=1, n_duplicates=2)  # 3 total, all same bytes
    buckets = check_thumbs.detect_duplicate_renders(out)
    assert len(buckets) == 1
    suggestion = check_thumbs._suggest_root_cause(buckets[0][1], total_baked=3, thumbs_dir=out)
    assert "scalar collapse" in suggestion


def test_suggest_root_cause_orchestrator_closure_three_same_source(tmp_path: Path) -> None:
    """3 byte-identical thumbs all under one source → "orchestrator closure"."""
    out = tmp_path / "thumbs"
    src = out / "ambientcg"
    # Make three byte-identical thumbs by writing the raw bytes (avoids
    # any per-file PNG metadata drift that PIL might insert).
    _solid_png(src / "a" / "thumb.png", (50, 50, 50))
    raw = (src / "a" / "thumb.png").read_bytes()
    for sub in ("b", "c"):
        d = src / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / "thumb.png").write_bytes(raw)
    # Plus a few unrelated thumbs so total != 3 (i.e. not scalar collapse).
    for i in range(20):
        _solid_png(src / f"mat_{i}" / "thumb.png", ((i * 13) % 256, 0, 0))
    _drop_sentinel(out)
    buckets = check_thumbs.detect_duplicate_renders(out)
    target = next((b for b in buckets if len(b[1]) == 3), None)
    assert target is not None
    suggestion = check_thumbs._suggest_root_cause(target[1], total_baked=23, thumbs_dir=out)
    assert "orchestrator closure bug" in suggestion


# ---------------------------------------------------------------------------
# CLI entrypoint (round-trip).
# ---------------------------------------------------------------------------


def test_cli_emits_default_json_report_under_thumbs_dir(tmp_path: Path) -> None:
    out = _make_bake(tmp_path, n_unique=4, n_duplicates=0)
    rc = check_thumbs.main([str(out)])
    assert rc == check_thumbs.EXIT_OK
    assert (out / JSON_REPORT_NAME).is_file()


def test_cli_dump_stale_allowlist_runs(tmp_path: Path, capsys) -> None:
    """`--dump-stale-allowlist` must work without a `thumbs_dir` arg."""
    rc = check_thumbs.main(["--dump-stale-allowlist", "--release-tag", "v2026.05.0"])
    assert rc == check_thumbs.EXIT_OK
    out = capsys.readouterr().out
    assert "no stale" in out.lower() or "stale" in out.lower()
