"""Tests for scripts/check_upstream_content_drift.py (mat-vis#295).

The script is a CI gate. We don't hit the network — every test
monkeypatches :func:`fetch_catalog` so the behavior we're locking in
is the diff + hash logic, not urllib.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_upstream_content_drift.py"


@pytest.fixture(scope="module")
def drift_mod():
    """Load the script as a module."""
    spec = importlib.util.spec_from_file_location("content_drift_gate", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["content_drift_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def _entry(mid: str, raw: dict | None = None) -> dict:
    """Build a minimal v3-shaped catalog entry with upstream.raw."""
    return {
        "id": mid,
        "source": "test",
        "mat_vis": {"name": mid},
        "upstream": {
            "source": "test",
            "schema_version": 1,
            "fetched_at": "2026-05-07T09:00:00Z",
            "raw": raw or {},
        },
    }


# ── stable_hash_upstream_raw ────────────────────────────────────


def test_hash_is_deterministic(drift_mod):
    raw = {"b": 2, "a": 1, "c": 3}
    h1 = drift_mod.stable_hash_upstream_raw(raw, frozenset())
    h2 = drift_mod.stable_hash_upstream_raw({"a": 1, "b": 2, "c": 3}, frozenset())
    assert h1 == h2  # key order independence


def test_hash_changes_on_value_edit(drift_mod):
    h_old = drift_mod.stable_hash_upstream_raw({"metalness": 1.0}, frozenset())
    h_new = drift_mod.stable_hash_upstream_raw({"metalness": 0.95}, frozenset())
    assert h_old != h_new


def test_hash_excludes_volatile_fields(drift_mod):
    raw_a = {"name": "X", "downloadCount": 100, "popularityScore": 0.5}
    raw_b = {"name": "X", "downloadCount": 999, "popularityScore": 0.99}
    volatile = frozenset({"downloadCount", "popularityScore"})
    assert drift_mod.stable_hash_upstream_raw(
        raw_a, volatile
    ) == drift_mod.stable_hash_upstream_raw(raw_b, volatile)


def test_hash_includes_upstream_own_timestamps(drift_mod):
    """Upstream's own timestamps (updated_date) ARE content drift —
    they change when upstream actually edits the material. Must NOT be
    excluded by the volatile filter."""
    raw_a = {"name": "X", "updated_date": "2025-01-01"}
    raw_b = {"name": "X", "updated_date": "2026-01-01"}
    h_a = drift_mod.stable_hash_upstream_raw(raw_a, frozenset())
    h_b = drift_mod.stable_hash_upstream_raw(raw_b, frozenset())
    assert h_a != h_b


# ── diff_content ────────────────────────────────────────────────


def test_diff_no_drift_returns_empty(drift_mod):
    prev = [_entry("a", {"x": 1}), _entry("b", {"x": 2})]
    cand = [_entry("a", {"x": 1}), _entry("b", {"x": 2})]
    assert drift_mod.diff_content("test", prev, cand) == []


def test_diff_detects_value_change(drift_mod):
    prev = [_entry("a", {"metalness": 1.0})]
    cand = [_entry("a", {"metalness": 0.95})]
    drifted = drift_mod.diff_content("test", prev, cand)
    assert len(drifted) == 1
    assert drifted[0]["id"] == "a"


def test_diff_ignores_added_materials(drift_mod):
    """A material in cand but not prev is an addition, not drift."""
    prev = [_entry("a", {"x": 1})]
    cand = [_entry("a", {"x": 1}), _entry("b", {"x": 2})]
    assert drift_mod.diff_content("test", prev, cand) == []


def test_diff_ignores_removed_materials(drift_mod):
    """A material in prev but not cand is a removal, not drift."""
    prev = [_entry("a", {"x": 1}), _entry("b", {"x": 2})]
    cand = [_entry("a", {"x": 1})]
    assert drift_mod.diff_content("test", prev, cand) == []


def test_diff_skips_pre_phase_c_records(drift_mod):
    """Records without upstream.raw on either side are skipped, not
    reported as drift."""
    prev = [{"id": "a", "source": "test", "mat_vis": {}}]  # no upstream block
    cand = [_entry("a", {"x": 1})]
    assert drift_mod.diff_content("test", prev, cand) == []


def test_diff_uses_per_source_volatile_allowlist(drift_mod):
    """ambientcg has downloadCount/popularityScore in its volatile set;
    those must not trigger drift."""
    prev = [_entry("a", {"name": "X", "downloadCount": 100, "popularityScore": 0.5})]
    cand = [_entry("a", {"name": "X", "downloadCount": 5000, "popularityScore": 0.99})]
    assert drift_mod.diff_content("ambientcg", prev, cand) == []


def test_diff_unknown_source_uses_empty_volatile(drift_mod):
    """An unknown source defaults to no volatile fields — every raw
    change is real drift."""
    prev = [_entry("a", {"foo": 1})]
    cand = [_entry("a", {"foo": 2})]
    drifted = drift_mod.diff_content("brand_new_source", prev, cand)
    assert len(drifted) == 1


def test_diff_reports_hashes(drift_mod):
    prev = [_entry("a", {"x": 1})]
    cand = [_entry("a", {"x": 2})]
    [d] = drift_mod.diff_content("test", prev, cand)
    assert "prev_hash" in d and "cand_hash" in d
    assert d["prev_hash"] != d["cand_hash"]
    assert len(d["prev_hash"]) == 64  # sha256 hex


# ── load_waivers ────────────────────────────────────────────────


def test_waivers_missing_path_returns_empty(drift_mod):
    assert drift_mod.load_waivers(None, "test") == set()


def test_waivers_nonexistent_file_returns_empty(drift_mod, tmp_path):
    assert drift_mod.load_waivers(tmp_path / "nope.yaml", "test") == set()


def test_waivers_empty_file_returns_empty(drift_mod, tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    assert drift_mod.load_waivers(p, "test") == set()


def test_waivers_parses_per_source(drift_mod, tmp_path):
    p = tmp_path / "waivers.yaml"
    p.write_text(
        "ambientcg:\n"
        '  - id: "Rock064"\n'
        '    reason: "AMD republished with corrected category"\n'
        '  - id: "Brick001"\n'
        '    reason: "upstream tag normalization"\n'
        "polyhaven:\n"
        '  - id: "some_material"\n'
        '    reason: "normal map re-export"\n'
    )
    assert drift_mod.load_waivers(p, "ambientcg") == {"Rock064", "Brick001"}
    assert drift_mod.load_waivers(p, "polyhaven") == {"some_material"}
    assert drift_mod.load_waivers(p, "gpuopen") == set()


# ── main(): end-to-end via monkeypatched fetch_catalog ──────────


@pytest.fixture
def patch_argv(monkeypatch):
    """Helper to set sys.argv for the gate's argparse main."""

    def _patch(*args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["check_upstream_content_drift.py", *args])

    return _patch


def test_main_no_drift_passes(drift_mod, monkeypatch, patch_argv, caplog):
    cand = [_entry("a", {"x": 1})]
    prev = [_entry("a", {"x": 1})]

    def fake_fetch(url):
        return cand if "candidate" in url else prev

    monkeypatch.setattr(drift_mod, "fetch_catalog", fake_fetch)
    patch_argv(
        "--source",
        "test",
        "--candidate",
        "https://x/candidate.json",
        "--previous",
        "https://x/previous.json",
    )
    with caplog.at_level("INFO", logger="content-drift-gate"):
        assert drift_mod.main() == 0
    assert any("no content drift" in r.message for r in caplog.records)


def test_main_drift_fails(drift_mod, monkeypatch, patch_argv, caplog):
    def fake_fetch(url):
        if "candidate" in url:
            return [_entry("a", {"metalness": 0.95})]
        return [_entry("a", {"metalness": 1.0})]

    monkeypatch.setattr(drift_mod, "fetch_catalog", fake_fetch)
    patch_argv(
        "--source",
        "test",
        "--candidate",
        "https://x/candidate.json",
        "--previous",
        "https://x/previous.json",
    )
    with caplog.at_level("ERROR", logger="content-drift-gate"):
        assert drift_mod.main() == 1
    assert any("FAIL content-drift" in r.message for r in caplog.records)


def test_main_first_cut_free_pass(drift_mod, monkeypatch, patch_argv, caplog):
    """If previous catalog 404s (first cut on a release line), exit 0."""

    def fake_fetch(url):
        if "candidate" in url:
            return [_entry("a", {"x": 1})]
        return None

    monkeypatch.setattr(drift_mod, "fetch_catalog", fake_fetch)
    patch_argv(
        "--source",
        "test",
        "--candidate",
        "https://x/candidate.json",
        "--previous",
        "https://x/previous.json",
    )
    with caplog.at_level("INFO", logger="content-drift-gate"):
        assert drift_mod.main() == 0
    assert any("first cut" in r.message for r in caplog.records)


def test_main_candidate_404_fails(drift_mod, monkeypatch, patch_argv):
    def fake_fetch(url):
        return None

    monkeypatch.setattr(drift_mod, "fetch_catalog", fake_fetch)
    patch_argv(
        "--source",
        "test",
        "--candidate",
        "https://x/candidate.json",
        "--previous",
        "https://x/previous.json",
    )
    assert drift_mod.main() == 2


def test_main_waiver_unblocks(drift_mod, monkeypatch, patch_argv, caplog, tmp_path):
    waivers = tmp_path / "w.yaml"
    waivers.write_text('test:\n  - id: "a"\n    reason: "OK"\n')

    def fake_fetch(url):
        if "candidate" in url:
            return [_entry("a", {"metalness": 0.95})]
        return [_entry("a", {"metalness": 1.0})]

    monkeypatch.setattr(drift_mod, "fetch_catalog", fake_fetch)
    patch_argv(
        "--source",
        "test",
        "--candidate",
        "https://x/candidate.json",
        "--previous",
        "https://x/previous.json",
        "--waivers",
        str(waivers),
    )
    with caplog.at_level("INFO", logger="content-drift-gate"):
        assert drift_mod.main() == 0
    assert any("waived: 1" in r.message for r in caplog.records)
