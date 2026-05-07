"""Tests for the v3 index builder (ADR-0011 / mat-vis#152 Phase A).

The Layer-1 contract says the ``mat_vis`` block has a *stable key set* —
``None`` / empty values where upstream has nothing, never absent keys.
That's half the contract clients depend on to avoid `KeyError` on fields
they expect to find (#152). These tests lock it in.
"""

from __future__ import annotations

from mat_vis_baker.common import (
    AttributionBlock,
    DatesBlock,
    MaterialRecord,
    MatVisBlock,
    PBRBlock,
    PhysicalBlock,
    UpstreamBlock,
)
from mat_vis_baker.index_builder import build_index


def _minimal_rec(mid: str = "Rock064", source: str = "ambientcg") -> MaterialRecord:
    return MaterialRecord(
        id=mid,
        source=source,
        mat_vis=MatVisBlock(name=mid, upstream_id=mid),
        maps=["color"],
    )


def test_build_index_emits_v3_envelope() -> None:
    entries = build_index([_minimal_rec()], source="ambientcg")
    assert len(entries) == 1
    e = entries[0]
    assert set(e.keys()) >= {"id", "source", "mat_vis", "maps"}
    # v2 top-level fields must be gone
    assert "category" not in e
    assert "tags" not in e
    assert "name" not in e
    assert "color_hex" not in e
    assert "roughness" not in e
    assert "metalness" not in e
    assert "ior" not in e
    assert "source_url" not in e
    assert "source_license" not in e
    assert "last_updated" not in e
    assert "source_mtlx_url" not in e


def test_mat_vis_block_has_stable_key_set() -> None:
    """Every mat_vis.* key present with null/empty defaults; no absences."""
    entries = build_index([_minimal_rec()], source="ambientcg")
    mv = entries[0]["mat_vis"]
    # Top-level mat_vis keys (every one is part of the contract)
    assert set(mv.keys()) == {
        "name",
        "category",
        "tags",
        "description",
        "physical",
        "pbr",
        "attribution",
        "dates",
        "upstream_id",
    }
    # Nested PhysicalBlock
    assert set(mv["physical"].keys()) == {"dimensions_m", "max_resolution_px"}
    # Nested PBRBlock — additive Phase 1 fields (#316) for procedural-PBR
    # library-browser facets are part of the stable shape now.
    assert set(mv["pbr"].keys()) == {
        "color_rgb",
        "roughness",
        "metalness",
        "ior",
        "specular_f0",
        "transmission",
        "complex_ior",
        "is_conductor",
        "metalness_mean",
        "metalness_source",
    }
    # Nested AttributionBlock
    assert set(mv["attribution"].keys()) == {"authors", "license_spdx", "source_url"}
    # Nested DatesBlock
    assert set(mv["dates"].keys()) == {"published", "updated"}


def test_mat_vis_block_defaults_are_null_or_empty() -> None:
    """Missing-upstream values serialize as None/empty, not absent."""
    entries = build_index([_minimal_rec()], source="ambientcg")
    mv = entries[0]["mat_vis"]
    assert mv["description"] is None
    assert mv["physical"]["dimensions_m"] is None
    assert mv["physical"]["max_resolution_px"] is None
    assert mv["pbr"]["color_rgb"] is None
    assert mv["pbr"]["roughness"] is None
    assert mv["pbr"]["metalness"] is None
    assert mv["pbr"]["ior"] is None
    assert mv["pbr"]["specular_f0"] is None
    assert mv["pbr"]["transmission"] is None
    assert mv["pbr"]["complex_ior"] is None
    # Phase 1 procedural-PBR fields (#316) — additive, default-None.
    assert mv["pbr"]["is_conductor"] is None
    assert mv["pbr"]["metalness_mean"] is None
    assert mv["pbr"]["metalness_source"] is None
    assert mv["attribution"]["authors"] == []
    assert mv["dates"]["published"] is None
    assert mv["dates"]["updated"] is None


def test_mat_vis_block_populated_values_flow_through() -> None:
    """Populated blocks serialize to the expected shape."""
    rec = MaterialRecord(
        id="Aluminum",
        source="physicallybased",
        mat_vis=MatVisBlock(
            name="Aluminum",
            category="metal",
            tags=["aluminium", "mirror"],
            description="polished Al",
            physical=PhysicalBlock(dimensions_m=[0.5, 0.5, None], max_resolution_px=[1024, 1024]),
            pbr=PBRBlock(
                color_rgb=[0.91, 0.92, 0.92],
                roughness=0.1,
                metalness=1.0,
                ior=1.39,
                specular_f0=[0.91, 0.92, 0.92],
                transmission=0.0,
                complex_ior=[1.39, 0.5, 1.39, 0.5, 1.39, 0.5],
            ),
            attribution=AttributionBlock(
                authors=["PB"],
                license_spdx="CC0-1.0",
                source_url="https://physicallybased.info",
            ),
            dates=DatesBlock(published="2023-01-01", updated="2024-06-01"),
            upstream_id="Aluminum",
        ),
        maps=[],
    )
    entry = build_index([rec], source="physicallybased")[0]
    mv = entry["mat_vis"]
    assert mv["name"] == "Aluminum"
    assert mv["category"] == "metal"
    assert mv["pbr"]["complex_ior"] == [1.39, 0.5, 1.39, 0.5, 1.39, 0.5]
    assert mv["physical"]["max_resolution_px"] == [1024, 1024]
    assert mv["dates"]["published"] == "2023-01-01"


def test_failed_record_keeps_status_marker_and_stable_keys() -> None:
    rec = MaterialRecord(
        id="Broken",
        source="ambientcg",
        mat_vis=MatVisBlock(name="Broken", upstream_id="Broken"),
        status="failed",
    )
    entry = build_index([rec], source="ambientcg")[0]
    assert entry["status"] == "failed"
    # Even failed records carry the full mat_vis stable key set.
    assert "mat_vis" in entry
    assert "pbr" in entry["mat_vis"]


def test_entries_sorted_by_id() -> None:
    recs = [_minimal_rec("Zzz"), _minimal_rec("Aaa"), _minimal_rec("Mmm")]
    entries = build_index(recs, source="ambientcg")
    assert [e["id"] for e in entries] == ["Aaa", "Mmm", "Zzz"]


def test_available_tiers_absent_when_empty() -> None:
    """Record with no tiers omits the field entirely (ADR-0007 rationale)."""
    entry = build_index([_minimal_rec()], source="ambientcg")[0]
    assert "available_tiers" not in entry


def test_available_tiers_present_when_populated() -> None:
    rec = _minimal_rec()
    rec.available_tiers = ["1k", "2k"]
    entry = build_index([rec], source="ambientcg")[0]
    assert entry["available_tiers"] == ["1k", "2k"]


# ── upstream mirror (Phase C, mat-vis#152) ──────────────────────


def test_upstream_block_omitted_when_not_set() -> None:
    """Records without an ``upstream`` block don't emit the key at all
    (pre-v3 backfill stays valid). The contract is: when the key is
    present, all four subkeys are present; when absent, callers know
    to fall back."""
    entry = build_index([_minimal_rec()], source="ambientcg")[0]
    assert "upstream" not in entry


def test_upstream_block_emitted_with_stable_key_set() -> None:
    rec = _minimal_rec()
    rec.upstream = UpstreamBlock(
        source="ambientcg",
        schema_version=1,
        fetched_at="2026-04-20T16:00:00Z",
        raw={"assetId": "Rock064", "displayName": "Rock 064"},
    )
    entry = build_index([rec], source="ambientcg")[0]
    assert "upstream" in entry
    upstream = entry["upstream"]
    assert set(upstream.keys()) == {"source", "schema_version", "fetched_at", "raw"}
    assert upstream["source"] == "ambientcg"
    assert upstream["schema_version"] == 1
    assert upstream["fetched_at"] == "2026-04-20T16:00:00Z"
    assert upstream["raw"]["assetId"] == "Rock064"


def test_upstream_block_empty_raw_emits_empty_dict() -> None:
    """When the allowlist emptied everything, ``raw`` is ``{}`` (preferred
    over ``None`` — stable downstream shape)."""
    rec = _minimal_rec()
    rec.upstream = UpstreamBlock(
        source="ambientcg",
        schema_version=1,
        fetched_at="2026-04-20T16:00:00Z",
        raw={},
    )
    entry = build_index([rec], source="ambientcg")[0]
    assert entry["upstream"]["raw"] == {}


def test_upstream_block_carries_through_failed_records() -> None:
    """Schema-diff runs on failed records too (allowlist drift applies
    regardless of download success)."""
    rec = MaterialRecord(
        id="Broken",
        source="ambientcg",
        mat_vis=MatVisBlock(name="Broken", upstream_id="Broken"),
        upstream=UpstreamBlock(
            source="ambientcg",
            schema_version=1,
            fetched_at="2026-04-20T16:00:00Z",
            raw={"assetId": "Broken"},
        ),
        status="failed",
    )
    entry = build_index([rec], source="ambientcg")[0]
    assert entry["status"] == "failed"
    assert entry["upstream"]["raw"] == {"assetId": "Broken"}
