"""Guard against drift between docs/specs/ source of truth and baker's _spec copy.

The CLIENT intentionally has no schema file — it discovers taxonomy from the
release manifest at runtime. Only the BAKER needs the schema packaged for
bake-time validation (before the manifest exists).
"""

from __future__ import annotations

import filecmp
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "docs" / "specs" / "index-schema.json"
BAKER_COPY = REPO / "src" / "mat_vis_baker" / "_spec" / "index-schema.json"


def test_source_of_truth_exists():
    assert SOURCE.exists(), f"canonical schema missing at {SOURCE}"


def test_baker_copy_matches_source():
    assert BAKER_COPY.exists(), (
        f"packaged copy missing at {BAKER_COPY}. Run `scripts/sync-spec.py`."
    )
    assert filecmp.cmp(SOURCE, BAKER_COPY, shallow=False), (
        f"{BAKER_COPY.relative_to(REPO)} drifted from {SOURCE.relative_to(REPO)}. "
        "Run `scripts/sync-spec.py` to re-sync."
    )


def test_baker_spec_loader_returns_expected_taxonomy():
    from mat_vis_baker.spec import CATEGORIES, CHANNELS, SOURCES

    assert set(CATEGORIES()) == {
        "metal",
        "wood",
        "stone",
        "fabric",
        "plastic",
        "concrete",
        "ceramic",
        "glass",
        "organic",
        "other",
    }
    assert set(CHANNELS()) == {
        "color",
        "normal",
        "roughness",
        "metalness",
        "ao",
        "displacement",
        "emission",
    }
    assert set(SOURCES()) == {"ambientcg", "polyhaven", "gpuopen", "physicallybased"}


def test_source_schema_is_valid_json():
    with open(SOURCE) as f:
        data = json.load(f)
    props = data["items"]["properties"]
    assert "enum" in props["category"]
    assert "enum" in props["source"]
    assert "enum" in props["maps"]["items"]
