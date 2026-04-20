"""Tests for scripts/check_upstream_schema_drift.py (ADR-0011 / mat-vis#152 phase-c).

The script is a CI gate. We don't hit the network — every test monkeypatches
:func:`_http_get` and :func:`_latest_release_tag` so the behavior we're
locking in is the diff logic itself, not urllib.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_upstream_schema_drift.py"


@pytest.fixture(scope="module")
def drift_module():
    """Load the script as a module — it's not a real package."""
    spec = importlib.util.spec_from_file_location("drift_gate", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["drift_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def _entry(mid: str, *, upstream_raw: dict | None = None, mv_overrides: dict | None = None) -> dict:
    """Build a v3-envelope index entry with optional upstream + mat_vis overrides."""
    mv = {
        "name": mid,
        "category": "other",
        "tags": [],
        "description": None,
        "physical": {"dimensions_m": None, "max_resolution_px": None},
        "pbr": {
            "color_rgb": None,
            "roughness": None,
            "metalness": None,
            "ior": None,
            "specular_f0": None,
            "transmission": None,
            "complex_ior": None,
        },
        "attribution": {"authors": [], "license_spdx": "CC0-1.0", "source_url": ""},
        "dates": {"published": None, "updated": None},
        "upstream_id": mid,
    }
    if mv_overrides:
        for key, val in mv_overrides.items():
            # shallow merge: "pbr.roughness" → mv["pbr"]["roughness"]
            if "." in key:
                block, leaf = key.split(".", 1)
                mv[block][leaf] = val
            else:
                mv[key] = val
    entry = {"id": mid, "source": "ambientcg", "mat_vis": mv, "maps": []}
    if upstream_raw is not None:
        entry["upstream"] = {
            "source": "ambientcg",
            "schema_version": 1,
            "fetched_at": "2026-04-20T16:00:00Z",
            "raw": upstream_raw,
        }
    return entry


def _write_catalog(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "candidate.json"
    p.write_text(json.dumps(entries))
    return p


# ── first-run free passes ───────────────────────────────────────


def test_no_previous_tag_is_free_pass(drift_module, tmp_path, monkeypatch):
    candidate = _write_catalog(tmp_path, [_entry("A", upstream_raw={"x": 1})])
    monkeypatch.setattr(drift_module, "_latest_release_tag", lambda: None)
    assert drift_module.run("ambientcg", str(candidate)) == 0


def test_previous_catalog_fetch_fails_is_free_pass(drift_module, tmp_path, monkeypatch):
    candidate = _write_catalog(tmp_path, [_entry("A", upstream_raw={"x": 1})])
    monkeypatch.setattr(drift_module, "_fetch_previous_catalog", lambda *a, **kw: None)
    assert drift_module.run("ambientcg", str(candidate), previous_tag="v0.5.0") == 0


def test_previous_without_upstream_block_is_free_pass(drift_module, tmp_path, monkeypatch):
    """Pre-Phase-C releases are the ONE moment we skip the gate."""
    candidate = _write_catalog(tmp_path, [_entry("A", upstream_raw={"x": 1})])
    # Previous catalog carries mat_vis but no upstream block.
    prev = [_entry("A")]
    monkeypatch.setattr(drift_module, "_fetch_previous_catalog", lambda *a, **kw: prev)
    assert drift_module.run("ambientcg", str(candidate), previous_tag="v0.5.0") == 0


# ── new upstream keys: warn, don't fail ─────────────────────────


def test_new_upstream_keys_warn_but_pass(drift_module, tmp_path, monkeypatch, caplog):
    prev = [_entry("A", upstream_raw={"x": 1, "y": 2})]
    candidate = _write_catalog(
        tmp_path,
        [_entry("A", upstream_raw={"x": 1, "y": 2, "z": 3})],
    )
    monkeypatch.setattr(drift_module, "_fetch_previous_catalog", lambda *a, **kw: prev)
    with caplog.at_level("WARNING"):
        rc = drift_module.run("ambientcg", str(candidate), previous_tag="v0.6.0")
    assert rc == 0
    assert any("NEW key" in msg and "z" in msg for msg in caplog.messages)


# ── removed mat_vis keys: fail ──────────────────────────────────


def test_removed_mat_vis_key_fails(drift_module, tmp_path, monkeypatch):
    prev = [_entry("A", upstream_raw={"x": 1})]
    # candidate drops ``pbr.complex_ior`` entirely
    cand_entry = _entry("A", upstream_raw={"x": 1})
    cand_entry["mat_vis"]["pbr"].pop("complex_ior")
    candidate = _write_catalog(tmp_path, [cand_entry])
    monkeypatch.setattr(drift_module, "_fetch_previous_catalog", lambda *a, **kw: prev)
    assert drift_module.run("ambientcg", str(candidate), previous_tag="v0.6.0") == 1


# ── presence regression: fail ───────────────────────────────────


def test_presence_regression_fails(drift_module, tmp_path, monkeypatch):
    """20 records populated ``pbr.roughness`` in prev; only 10 in candidate →
    100% → 50%, a 50pp drop, well above the 5pp threshold."""
    prev = [
        _entry(f"M{i}", upstream_raw={"x": 1}, mv_overrides={"pbr.roughness": 0.5})
        for i in range(20)
    ]
    cand = [
        _entry(f"M{i}", upstream_raw={"x": 1}, mv_overrides={"pbr.roughness": 0.5})
        for i in range(10)
    ] + [_entry(f"M{i}", upstream_raw={"x": 1}) for i in range(10, 20)]
    candidate = _write_catalog(tmp_path, cand)
    monkeypatch.setattr(drift_module, "_fetch_previous_catalog", lambda *a, **kw: prev)
    assert drift_module.run("ambientcg", str(candidate), previous_tag="v0.6.0") == 1


def test_presence_regression_within_threshold_passes(drift_module, tmp_path, monkeypatch):
    """5pp drop is the boundary — NOT strictly greater, so this passes."""
    prev = [
        _entry(f"M{i}", upstream_raw={"x": 1}, mv_overrides={"pbr.roughness": 0.5})
        for i in range(20)
    ]
    # 19/20 = 95%, prev was 100%, 5pp drop is NOT > 5.
    cand = [
        _entry(f"M{i}", upstream_raw={"x": 1}, mv_overrides={"pbr.roughness": 0.5})
        for i in range(19)
    ] + [_entry("M19", upstream_raw={"x": 1})]
    candidate = _write_catalog(tmp_path, cand)
    monkeypatch.setattr(drift_module, "_fetch_previous_catalog", lambda *a, **kw: prev)
    assert drift_module.run("ambientcg", str(candidate), previous_tag="v0.6.0") == 0


# ── happy path ─────────────────────────────────────────────────


def test_identical_catalog_passes(drift_module, tmp_path, monkeypatch):
    cat = [
        _entry("A", upstream_raw={"x": 1, "y": 2}, mv_overrides={"pbr.roughness": 0.5}),
        _entry("B", upstream_raw={"x": 3, "y": 4}, mv_overrides={"pbr.roughness": 0.7}),
    ]
    monkeypatch.setattr(drift_module, "_fetch_previous_catalog", lambda *a, **kw: cat)
    candidate = _write_catalog(tmp_path, cat)
    assert drift_module.run("ambientcg", str(candidate), previous_tag="v0.6.0") == 0
