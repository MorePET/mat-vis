"""Layer 4 — adapter-output passthrough tests.

mat-vis#382 — RED phase. Drives the contract that the renderer-shaped
fields in ``fixtures/expected_pbr.yaml`` actually appear in
``to_threejs`` / ``to_gltf`` output dicts when fed scalars resolved
via :meth:`MatVisClient._scalars_for`. The Layer 3 test pins what
``_scalars_for`` reads; this layer pins what the adapters then emit
downstream — split into two test classes because Three.js and glTF
use different namespaces and audit verdict is "combined tests obscure
which adapter is wrong" (mat-vis#382 §"L4 split per renderer").

xfail-strict marker discipline: when the upstream Layer 3
passthrough is missing a field (mat-vis#380), the field also never
reaches the adapter output — same RED/GREEN flip cadence as
``test_layer3_scalars_for.py``. Strict means: today the missing-field
assertions XFAIL; once #381 lands and is rebased in, they XPASS-failed
(CI red), forcing the marker drop in the GREEN-phase commit.
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
from mat_vis_client.adapters import to_gltf, to_threejs


# ── Fixture loader ──────────────────────────────────────────────


_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "fixtures" / "expected_pbr.yaml"


def _load_fixture() -> dict[str, Any]:
    """Load ``fixtures/expected_pbr.yaml`` from the repo root.

    Skip the module rather than collection-error if PyYAML is unavailable
    (install ``mat-vis-client[test]``).
    """
    if yaml is None:
        pytest.skip("PyYAML not installed; install mat-vis-client[test]")
    if not _FIXTURE_PATH.exists():
        pytest.skip(f"fixture missing: {_FIXTURE_PATH}")
    with _FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _mock_entry(material: str, source: str) -> dict:
    """Same shape as ``test_layer3_scalars_for._mock_entry``.

    Duplicated rather than imported across test files so a single
    edit (e.g. adding a new physically-grounded field to the fixture)
    surfaces both Layer 3 and Layer 4 in lockstep — and so the L4
    suite remains runnable in isolation. Field values pass each
    fixture entry's physical bounds.
    """
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
        # the #294 / #299 textured-passthrough convention. Mirrors the
        # L3 mock entries; both files carry the same table so each
        # suite is runnable in isolation. (#384 routing-failure surface.)
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
    return {
        "id": material.lower().replace(" ", "_").replace("(", "").replace(")", ""),
        "mat_vis": {
            "name": material,
            "pbr": pbr,
        },
    }


def _resolve_dotted(d: dict, path: str) -> Any:
    """Walk a dotted key path through nested dicts; raise KeyError on miss.

    glTF asserts use dotted paths (e.g. ``extensions.KHR_materials_ior``)
    because the adapter materializes top-level extensions under
    ``material["extensions"][...]``. KeyError is the explicit "field
    dropped" signal the test surfaces to the assert.
    """
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(path)
        cur = cur[part]
    return cur


def _scalars_via_client(source: str, material: str) -> dict:
    """End-to-end fixture → ``_scalars_for`` adapter input dict.

    The adapter contract is "consume what _scalars_for emits and
    pass it through." Driving from ``_scalars_for`` (instead of
    constructing a synthetic scalars dict directly) keeps the L4
    test honest: any regression *upstream* (e.g. a new
    case-insensitivity bug in mat-vis#368, or a future field
    rename) shows up here as well, not just in L3.
    """
    entry = _mock_entry(material, source)
    client = MatVisClient()
    with patch.object(client, "index", return_value=[entry]):
        return client._scalars_for(source, material)


def _flatten_renderer_cases(renderer: str) -> list[tuple[str, str, str]]:
    """``[(source, material, renderer_key), ...]`` for one renderer.

    Mirrors ``_flatten_fixture_cases`` in the L3 suite. Producing
    one row per (renderer-key) keeps failure messages crisp:
    ``gpuopen-Glass-transmission`` rather than per-material lump.
    """
    fx = _load_fixture()
    rows: list[tuple[str, str, str]] = []
    for entry in fx["entries"]:
        for key in entry["renderer_keys"].get(renderer, []):
            rows.append((entry["source"], entry["material"], key))
    return rows


_THREEJS_CASES = (
    _flatten_renderer_cases("threejs") if yaml is not None and _FIXTURE_PATH.exists() else []
)
_THREEJS_IDS = [f"{s}-{m}-{k}" for s, m, k in _THREEJS_CASES]
_GLTF_CASES = _flatten_renderer_cases("gltf") if yaml is not None and _FIXTURE_PATH.exists() else []
_GLTF_IDS = [f"{s}-{m}-{k}" for s, m, k in _GLTF_CASES]


# ── Layer 4 — Three.js adapter output ────────────────────────────


class TestLayer4_ToThreejs:
    """``to_threejs(scalars, textures)`` emits each fixture renderer key.

    Split into its own class (per audit verdict, mat-vis#382) so the
    test report localizes Three.js-specific regressions distinctly
    from glTF — combined tests obscured which adapter was wrong.
    """

    @pytest.mark.parametrize(("source", "material", "key"), _THREEJS_CASES, ids=_THREEJS_IDS)
    def test_threejs_key_present_in_output(
        self,
        source: str,
        material: str,
        key: str,
    ) -> None:
        scalars = _scalars_via_client(source, material)
        out = to_threejs(scalars, textures=None)
        assert key in out, (
            f"to_threejs({source!r}, {material!r}) missing {key!r}; "
            f"got keys {sorted(out)} — the renderer-shaped passthrough "
            "asserted by fixtures/expected_pbr.yaml.renderer_keys.threejs."
        )


# ── Layer 4 — glTF adapter output ────────────────────────────────


class TestLayer4_ToGltf:
    """``to_gltf(scalars, textures)`` emits each fixture renderer key.

    glTF keys use dotted paths (e.g. ``extensions.KHR_materials_ior``)
    because the adapter nests KHR extensions under
    ``material["extensions"]``. ``_resolve_dotted`` walks the path;
    KeyError on any missing segment surfaces as the assertion failure.
    """

    @pytest.mark.parametrize(("source", "material", "key"), _GLTF_CASES, ids=_GLTF_IDS)
    def test_gltf_key_present_in_output(
        self,
        source: str,
        material: str,
        key: str,
    ) -> None:
        scalars = _scalars_via_client(source, material)
        out = to_gltf(scalars, textures=None)
        try:
            _resolve_dotted(out, key)
        except KeyError:
            pytest.fail(
                f"to_gltf({source!r}, {material!r}) missing dotted key "
                f"{key!r}; output={out!r} — the renderer-shaped passthrough "
                "asserted by fixtures/expected_pbr.yaml.renderer_keys.gltf."
            )
