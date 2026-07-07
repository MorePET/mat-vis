"""Lock the v3-nested schema invariant at the serializer (#291).

Every source (ambientcg, polyhaven, gpuopen, physicallybased) must emit a
material's display name under the ``mat_vis`` envelope and **never** as a
top-level ``name`` field. This was the flat-v2 vs v3-nested inconsistency the
#284 client hotfix papered over; #291 confirms the migration is complete and
guards it from silently regressing.

These tests run against ``build_index`` — the single serialization choke point
every source funnels through — so the invariant holds for all sources at once,
hermetically, in every CI run. If a future change reintroduces a top-level
``name`` (or drops ``mat_vis.name``), this fails loudly.
"""

from __future__ import annotations

import pytest

from mat_vis_baker.common import MaterialRecord, MatVisBlock
from mat_vis_baker.index_builder import build_index

# The four production sources — the invariant is source-independent, but
# parametrizing documents that it is asserted for all of them.
ALL_SOURCES = ["ambientcg", "polyhaven", "gpuopen", "physicallybased"]


def _record(mid: str, name: str, source: str) -> MaterialRecord:
    return MaterialRecord(
        id=mid,
        source=source,
        mat_vis=MatVisBlock(name=name, category="stone"),
        available_tiers=["1k"],
        maps=["color"],
    )


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_name_is_nested_never_top_level(source: str) -> None:
    """The display name lives at ``entry['mat_vis']['name']`` and there is
    NO top-level ``name`` key — for every source."""
    entries = build_index([_record("Mat001", "Polished Marble", source)], source)
    assert len(entries) == 1
    entry = entries[0]

    # Nested name is present and carries the value.
    assert entry["mat_vis"]["name"] == "Polished Marble"
    # The flat-v2 shape must never reappear.
    assert "name" not in entry, (
        f"{source}: top-level 'name' leaked into the catalog entry — the v3 "
        f"migration (#291) requires the name under mat_vis only"
    )


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_stable_top_level_key_set_has_no_name(source: str) -> None:
    """Freeze the top-level key set so a stray ``name`` (or any flat-v2
    scalar) can't slip back in unnoticed."""
    entries = build_index([_record("Mat001", "Slate", source)], source)
    top_keys = set(entries[0].keys())
    # id/source/mat_vis/maps/available_tiers are the always-present core;
    # texture_hashes/upstream/status are conditional and absent here.
    assert top_keys == {"id", "source", "mat_vis", "maps", "available_tiers"}
    assert "name" not in top_keys
    assert "category" not in top_keys  # another flat-v2 field that must stay nested


def test_empty_name_still_nested_not_promoted() -> None:
    """A record with an empty name (extractor had none) keeps the nested
    slot — it must not fall back to synthesizing a top-level ``name``."""
    entries = build_index([_record("Mat001", "", "gpuopen")], "gpuopen")
    entry = entries[0]
    assert entry["mat_vis"]["name"] == ""
    assert "name" not in entry
