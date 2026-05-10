"""Layer 3 — ``MatVisClient._scalars_for`` field-passthrough tests.

mat-vis#382 — RED phase. These tests pin the contract that
``_scalars_for`` reads *all* PBR scalars the substrate emits, not just
the four (roughness/metalness/ior/color_rgb) the pre-#381 client did.
The bug they catch is mat-vis#380: glass / acrylic / clearcoat
materials silently rendered as opaque-default-grey because the
load-bearing fields (``transmission``, ``thickness``, ``dispersion``,
``clearcoat_roughness``) never reached the adapter.

Driven entirely off ``fixtures/expected_pbr.yaml`` (the single source of
truth shared with Layer 4 and, P1, with Layer 1+2). Bounds are
*physical* — Glass transmission ≥ 0.5 because that is what glass IS,
not what the substrate currently happens to emit. That's load-bearing:
lenient bounds (``min: 0``) would defeat the bug-catcher.

xfail-strict marker discipline (mat-vis#382 §"TDD discipline"):
    * On current ``dev`` (pre-#381): assertions on transmission /
      dispersion / thickness / clearcoat_roughness fail → reported as
      XFAIL → CI green (the strict marker means CI doesn't break).
    * Once #381 lands and is rebased into this branch: the same
      assertions pass → reported as XPASS-failed (strict) → CI red,
      forcing the follow-up commit that drops the marker.

Tests stay deterministic by mocking ``client.index()`` with a v3
catalog entry shaped like the substrate emits — same pattern as
``test_scalar_only_lookup.py``. No network IO, no live HF.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML ships via the [test] extra
    yaml = None  # type: ignore[assignment]

from mat_vis_client import MatVisClient


# ── Fixture loader ──────────────────────────────────────────────


_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "fixtures" / "expected_pbr.yaml"


def _load_fixture() -> dict[str, Any]:
    """Load ``fixtures/expected_pbr.yaml`` from the repo root.

    The fixture lives at the project root rather than inside the
    Python client tree because it is shared with the (P1) baker-side
    Layer 1+2 tests (mat-vis#382). Skip the whole module if PyYAML
    is unavailable rather than failing to collect — install the
    ``mat-vis-client[test]`` extra.
    """
    if yaml is None:
        pytest.skip("PyYAML not installed; install mat-vis-client[test]")
    if not _FIXTURE_PATH.exists():
        pytest.skip(f"fixture missing: {_FIXTURE_PATH}")
    with _FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _mock_entry(material: str, source: str) -> dict:
    """Build a minimal v3 catalog entry exercising the full PBR field set.

    The entry deliberately carries every field a real substrate
    record might carry (``transmission``, ``thickness``, ``dispersion``,
    ``clearcoat``, ``clearcoat_roughness``, ``specular_intensity``,
    ``specular_color``, ``emissive``) so ``_scalars_for`` has data to
    pass through. Field values are chosen to land inside the fixture's
    physical bounds for every material — the test asserts the bound
    is met *and* that the field reached the scalars dict at all
    (the latter is what catches mat-vis#380).
    """
    # Per-material PBR shape mirroring substrate output. Values are
    # intentionally inside the fixture's physical bounds so a correct
    # passthrough implementation passes; the bug surface is the
    # *passthrough* itself, not the value.
    pbr_by_material: dict[tuple[str, str], dict] = {
        ("physicallybased", "Glass"): {
            "roughness": 0.01,
            "ior": 1.5,
            "transmission": 1.0,
            "thickness": 1.0,
            "color_rgb": [0.8, 0.8, 0.8],
        },
        ("physicallybased", "Plastic (Acrylic)"): {
            "roughness": 0.4,
            "ior": 1.4905,
            "transmission": 1.0,
            "color_rgb": [1.0, 1.0, 1.0],
        },
        ("gpuopen", "Glass"): {
            "roughness": 0.05,
            "ior": 1.5,
            "transmission": 0.8,
            "dispersion": 0.25,
            "thickness": 10.0,
            "specular_intensity": 0.5,
            "specular_color": [1.0, 1.0, 1.0],
        },
        ("physicallybased", "Aluminum"): {
            "roughness": 0.18,
            "metalness": 1.0,
            "color_rgb": [0.91, 0.92, 0.92],
        },
        ("gpuopen", "Chrome"): {
            "roughness": 0.05,
            "metalness": 1.0,
            "color_rgb": [0.55, 0.56, 0.55],
        },
        ("gpuopen", "Gold"): {
            "roughness": 0.10,
            "metalness": 1.0,
            "color_rgb": [1.0, 0.78, 0.34],
        },
        ("gpuopen", "Aluminum Brushed"): {
            "roughness": 0.4,
            "metalness": 1.0,
            "color_rgb": [0.89, 0.89, 0.89],
        },
        ("gpuopen", "Bronze Oxydized"): {
            "roughness": 0.6,
            "metalness": 1.0,
            "color_rgb": [1.0, 1.0, 1.0],
        },
        ("gpuopen", "Stainless Steel Brushed"): {
            "roughness": 0.5,
            "metalness": 1.0,
            "color_rgb": [1.0, 1.0, 1.0],
        },
        ("gpuopen", "Perforated Metal"): {
            "roughness": 0.5,
            "metalness": 1.0,
            "clearcoat_roughness": 0.1,
            "specular_intensity": 1.0,
            "dispersion": 0.0,
            "color_rgb": [0.7, 0.7, 0.7],
        },
        # ambientcg textured triad — neutral-multiplier identity per
        # the #294 / #299 textured-passthrough convention. Substrate
        # emits color=[1,1,1], metalness=1.0, roughness=1.0 so textures
        # act as pure multipliers. Mirrors fixtures/expected_pbr.yaml
        # entries 11-13 (#384 routing-failure surface).
        ("ambientcg", "Metal 007"): {
            "roughness": 1.0,
            "metalness": 1.0,
            "metalness_source": "texture",
            "color_rgb": [1.0, 1.0, 1.0],
        },
        ("ambientcg", "Fabric 004"): {
            "roughness": 1.0,
            "metalness": 1.0,
            "metalness_source": "texture",
            "color_rgb": [1.0, 1.0, 1.0],
        },
        ("ambientcg", "Metal Plates 006"): {
            "roughness": 1.0,
            "metalness": 1.0,
            "metalness_source": "texture",
            "color_rgb": [1.0, 1.0, 1.0],
        },
    }
    pbr = pbr_by_material[(source, material)]
    # Substrate stores normalized lowercase ids; display name lives
    # under ``mat_vis.name``. The lookup path tested here resolves
    # via the case-insensitive name match (mat-vis#368).
    return {
        "id": material.lower().replace(" ", "_").replace("(", "").replace(")", ""),
        "mat_vis": {
            "name": material,
            "pbr": pbr,
        },
    }


def _flatten_fixture_cases() -> list[tuple[str, str, str, dict]]:
    """Yield ``(source, material, field, bounds)`` rows for parametrize.

    One pytest case per (source, material, expected-field) combination.
    That gives us a row-per-assertion in the test report so a single
    failing field (e.g. Glass.transmission) is named explicitly rather
    than buried under a per-material aggregate.
    """
    fx = _load_fixture()
    rows: list[tuple[str, str, str, dict]] = []
    for entry in fx["entries"]:
        source = entry["source"]
        material = entry["material"]
        for field, bounds in entry["expected"].items():
            rows.append((source, material, field, bounds))
    return rows


# Build the parametrize id list once at module-collection time so each
# case appears as ``physicallybased-Glass-transmission`` etc. The
# fixture's xfail status is decided per-row inside the test body via
# ``request.applymarker`` — strict-xfail at the class level would mark
# every row, including the ones that already pass on dev (e.g.
# ``roughness``, ``metalness``), and strict + XPASS would break CI now.
_CASES = _flatten_fixture_cases() if yaml is not None and _FIXTURE_PATH.exists() else []
_CASE_IDS = [f"{s}-{m}-{f}" for s, m, f, _ in _CASES]


# ── Layer 3: scalars_for passthrough ───────────────────────────


@pytest.mark.parametrize(("source", "material", "field", "bounds"), _CASES, ids=_CASE_IDS)
def test_scalars_for_field_within_bounds(
    request: pytest.FixtureRequest,
    source: str,
    material: str,
    field: str,
    bounds: dict,
) -> None:
    """``_scalars_for(source, material)[field]`` is present and within bounds.

    Drives the Layer 3 contract from ``fixtures/expected_pbr.yaml``:
    each fixture entry's ``expected.<field>: {min, max}`` becomes one
    row here. Tests caught mat-vis#380 in the RED phase; PR #381
    fixed the passthrough and the xfail markers were dropped (this
    file's GREEN follow-up).
    """
    entry = _mock_entry(material, source)
    client = MatVisClient()
    with patch.object(client, "index", return_value=[entry]):
        scalars = client._scalars_for(source, material)

    assert field in scalars, (
        f"_scalars_for({source!r}, {material!r}) dropped {field!r}; "
        f"got keys {sorted(scalars)} — substrate emitted it but the adapter "
        "scalars dict never received it. This is the mat-vis#380 surface."
    )
    value = scalars[field]
    assert isinstance(value, (int, float)), (
        f"{field!r} should be numeric, got {type(value).__name__}={value!r}"
    )
    if "min" in bounds:
        assert value >= bounds["min"], (
            f"{source}/{material}.{field}={value} below physical floor "
            f"{bounds['min']} from fixtures/expected_pbr.yaml"
        )
    if "max" in bounds:
        assert value <= bounds["max"], (
            f"{source}/{material}.{field}={value} above physical ceiling "
            f"{bounds['max']} from fixtures/expected_pbr.yaml"
        )
