"""Match dict-subclass + unified search/asset surface (#359).

The contract:

- ``Match`` is a dict-subclass with smart ``__str__`` and stable identity
  + namespace properties. ``isinstance(m, dict)`` stays True so all existing
  ``entry["mat_vis"]["pbr"]["..."]`` access keeps working.
- ``client.search()`` returns ``list[Match]``. New filters: ``query=`` (fuzzy
  text), ``name=`` (substring), ``tag=`` (material-tag substring),
  ``is_conductor=``, ``has_map=``, ``transmission_range=``, ``dispersion_range=``.
- ``score=`` → ``distance=`` (hard rename, no alias).
- ``client.index()`` returns ``list[Match]`` (consistent with search()).
- ``client.asset()`` is polymorphic — accepts a ``Match``, a ``"source/id"``
  string ref, or explicit ``source=, id=, tier=`` kwargs.
- ``materials()`` is unchanged (returns ``list[str]``, protects
  downstream tests).
- ``SourceNotFoundError`` gains fuzzy did-you-mean for typos like
  ``ambient_cg`` → ``ambientcg``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mat_vis_client import MatVisClient


# ── Test fixtures ──────────────────────────────────────────────


def _entry(
    mid: str,
    *,
    name: str | None = None,
    cat: str = "stone",
    r: float | None = 0.5,
    m: float | None = 0.0,
    tags: list[str] | None = None,
    is_conductor: bool | None = None,
    transmission: float | None = None,
    dispersion: float | None = None,
    tiers: list[str] | None = None,
    maps: list[str] | None = None,
    source: str = "ambientcg",
) -> dict:
    return {
        "id": mid,
        "source": source,
        "mat_vis": {
            "name": name or mid,
            "category": cat,
            "tags": tags or [],
            "description": None,
            "physical": {"dimensions_m": None, "max_resolution_px": None},
            "pbr": {
                "color_rgb": None,
                "roughness": r,
                "metalness": m,
                "ior": None,
                "specular_f0": None,
                "transmission": transmission,
                "complex_ior": None,
                "is_conductor": is_conductor,
                "metalness_mean": None,
                "metalness_source": None,
                "clearcoat_roughness": None,
                "specular_intensity": None,
                "specular_color": None,
                "thickness": None,
                "dispersion": dispersion,
            },
            "attribution": {
                "authors": [],
                "license_spdx": "CC0-1.0",
                "source_url": "",
            },
            "dates": {"published": None, "updated": None},
            "upstream_id": mid,
        },
        "available_tiers": tiers if tiers is not None else ["1k"],
        "maps": maps or ["color"],
    }


CORPUS = [
    _entry("Rock064", cat="stone", r=0.78, m=0.0, tags=["rock", "rough"]),
    _entry("Brass001", cat="metal", r=0.10, m=1.0, tags=["brass", "polished"], is_conductor=True),
    _entry(
        "BrassRusted",
        name="Brass Rusted",
        cat="metal",
        r=0.45,
        m=0.9,
        tags=["brass", "rusted"],
        is_conductor=True,
    ),
    _entry(
        "Glass001",
        cat="glass",
        r=0.0,
        m=0.0,
        transmission=1.0,
        dispersion=0.05,
        maps=["color", "opacity"],
    ),
    _entry("Wood002", cat="wood", r=0.7, m=0.0, tags=["oak"]),
]


def _patch_search_deps(c: MatVisClient, corpus: list[dict] = CORPUS):
    """Stack the three patches search() needs: sources(), index(), categories()."""
    cats = frozenset(e["mat_vis"]["category"] for e in corpus)
    return (
        patch.object(c, "sources", return_value=["ambientcg"]),
        patch.object(c, "index", return_value=corpus),
        patch.object(c, "categories", return_value=cats),
    )


# ── Match class ────────────────────────────────────────────────


def test_match_is_dict_subclass():
    """Match inherits from dict — full subscript access stays."""
    from mat_vis_client import Match

    m = Match(CORPUS[0])
    assert isinstance(m, dict)
    assert isinstance(m, Match)
    # Existing access pattern keeps working.
    assert m["mat_vis"]["pbr"]["roughness"] == 0.78
    assert m["id"] == "Rock064"


def test_match_identity_properties():
    """id / source / ref — the three identity props."""
    from mat_vis_client import Match

    m = Match(CORPUS[0])
    assert m.id == "Rock064"
    assert m.source == "ambientcg"
    assert m.ref == "ambientcg/Rock064"


def test_match_namespace_pointers():
    """mat_vis / pbr / physical / attribution / dates / maps."""
    from mat_vis_client import Match

    m = Match(CORPUS[0])
    assert m.mat_vis is m["mat_vis"]
    assert m.pbr is m["mat_vis"]["pbr"]
    assert m.physical is m["mat_vis"]["physical"]
    assert m.attribution is m["mat_vis"]["attribution"]
    assert m.dates is m["mat_vis"]["dates"]
    assert m.maps == ["color"]


def test_match_tiers_property():
    from mat_vis_client import Match

    m = Match(CORPUS[0])
    assert m.tiers == ["1k"]


def test_match_upstream_optional():
    """upstream prop returns None when block absent (most entries)."""
    from mat_vis_client import Match

    m = Match(CORPUS[0])
    assert m.upstream is None
    with_upstream = dict(CORPUS[0])
    with_upstream["upstream"] = {"raw": {"foo": "bar"}}
    m2 = Match(with_upstream)
    assert m2.upstream == {"raw": {"foo": "bar"}}


def test_match_str_includes_ref_and_pbr_summary():
    """__str__ is a one-line human summary, NOT the full dict dump."""
    from mat_vis_client import Match

    m = Match(CORPUS[1])  # Brass001
    s = str(m)
    # Must include ref (the canonical fetch handle) and a hint of category.
    assert "ambientcg/Brass001" in s
    assert "metal" in s
    # Must NOT be the raw dict dump.
    assert "{'id':" not in s


def test_match_no_pbr_field_properties():
    """Only identity + namespace pointers are properties.

    No ``m.roughness`` / ``m.metalness`` / ``m.ior`` etc. — those would
    duplicate substrate shape and bind Match to PBR field churn (#316,
    #340 added 8 fields in two months). Use ``m.pbr["roughness"]``.
    """
    from mat_vis_client import Match

    m = Match(CORPUS[0])
    assert not hasattr(m, "roughness")
    assert not hasattr(m, "metalness")
    assert not hasattr(m, "ior")


# ── search() returns list[Match] ───────────────────────────────


def test_search_returns_list_of_match():
    from mat_vis_client import MatVisClient, Match

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg")
    assert results, "expected non-empty result for unfiltered source"
    for r in results:
        assert isinstance(r, Match)
        assert isinstance(r, dict)


def test_search_no_query_stable_id_sort():
    """Without query=, results sort by id ascending."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg")
    ids = [m.id for m in results]
    assert ids == sorted(ids)


# ── search() new structural filters ────────────────────────────


def test_search_filter_by_name_substring():
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", name="brass")
    ids = {m.id for m in results}
    assert ids == {"Brass001", "BrassRusted"}


def test_search_filter_by_tag_substring():
    """tag= substring-matches any tag in mat_vis.tags."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", tag="rust")
    ids = {m.id for m in results}
    assert ids == {"BrassRusted"}


# ── tag= ↔ release= rename ─────────────────────────────────────


def test_search_release_kwarg_dispatches_to_at():
    """release= scopes the call to a pinned revision via .at()."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()

    class _FakePinned:
        calls: list[dict] = []

        def search(self, category=None, **kwargs):
            _FakePinned.calls.append({"category": category, **kwargs})
            return []

    with patch.object(c, "at", return_value=_FakePinned()) as p_at:
        out = c.search(source="ambientcg", release="v2026.04.99")
    p_at.assert_called_once_with("v2026.04.99")
    assert out == []


def test_search_old_tag_release_override_kwarg_is_gone():
    """tag= now means material-tag filter, NOT release-tag override.

    Don't silently misroute. The intent is clear: tag is for material
    tags now; use release= for release-tag scoping.
    """
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    sentinel_called = []
    with p_sources, p_index, p_cats:
        with patch.object(c, "at", side_effect=lambda *a, **k: sentinel_called.append(a)):
            results = c.search(source="ambientcg", tag="v2026.04.99")
    # tag= treated as material-tag substring; no material has that tag → empty.
    assert results == []
    assert sentinel_called == []  # .at() was NOT called


def test_search_filter_by_is_conductor():
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", is_conductor=True)
    ids = {m.id for m in results}
    assert ids == {"Brass001", "BrassRusted"}


def test_search_filter_by_has_map():
    """has_map='opacity' filters to materials whose maps[] includes 'opacity'."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", has_map="opacity")
    ids = {m.id for m in results}
    assert ids == {"Glass001"}


def test_search_filter_by_transmission_range():
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", transmission_range=(0.5, 1.0))
    ids = {m.id for m in results}
    assert ids == {"Glass001"}


def test_search_filter_by_dispersion_range():
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", dispersion_range=(0.0, 0.1))
    ids = {m.id for m in results}
    assert ids == {"Glass001"}


# ── search() query= fuzzy text ─────────────────────────────────


def test_search_query_substring_fallback():
    """Without rapidfuzz, query= is a token-AND substring on (name, tags).

    'brass' should match both Brass001 and BrassRusted (name match) and
    rank by simple substring presence.
    """
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", query="brass")
    ids = {m.id for m in results}
    assert ids == {"Brass001", "BrassRusted"}


def test_search_query_token_AND_across_fields():
    """query='rusted brass' tokens AND-narrow: must appear together."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", query="rusted brass")
    # Only BrassRusted has both tokens (name has 'brass', tag has 'rusted').
    ids = [m.id for m in results]
    assert ids == ["BrassRusted"]


def test_search_query_with_filters_AND_narrows():
    """Structural filters AND-narrow before query= ranks within."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", category="metal", query="brass")
    ids = {m.id for m in results}
    assert ids == {"Brass001", "BrassRusted"}  # Wood002 / Rock064 / Glass001 excluded


# ── score → distance rename ────────────────────────────────────


def test_search_distance_attaches_when_scalar_passed():
    """distance=True with a scalar attaches a 'distance' field; sorts ascending."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        results = c.search(source="ambientcg", category="metal", roughness=0.1, distance=True)
    # Brass001 (0.10) must sort before BrassRusted (0.45) when distance=True.
    ids = [m.id for m in results]
    assert ids[0] == "Brass001"
    for r in results:
        assert "distance" in r


def test_search_score_kwarg_is_gone():
    """Hard rename: ``score=`` raises TypeError, no alias."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    p_sources, p_index, p_cats = _patch_search_deps(c)
    with p_sources, p_index, p_cats:
        with pytest.raises(TypeError):
            c.search(source="ambientcg", roughness=0.5, score=True)


# ── index() returns list[Match] ────────────────────────────────


def test_index_returns_list_of_match():
    """index() returns Match objects, just like search()."""
    from mat_vis_client import MatVisClient, Match

    c = MatVisClient()
    with patch.object(c, "_load_index_raw", return_value=CORPUS):
        results = c.index("ambientcg")
    assert results
    for r in results:
        assert isinstance(r, Match)


# ── asset() polymorphism ───────────────────────────────────────


def test_asset_accepts_match_handle():
    """asset(match) — Match carries source + id, no kwargs needed."""
    from mat_vis_client import MatVisClient, Match

    c = MatVisClient()
    m = Match(CORPUS[0])
    a = c.asset(m)
    assert a.source == "ambientcg"
    assert a.material_id == "Rock064"
    # mat-vis#374: default tier is "auto" since 0.7.0. The Match path
    # used to peek at Match.tiers and pick "1k" if present; now it
    # defers to the client-level auto resolver on first .textures
    # access (single source of truth for tier selection).
    assert a.tier == "auto"


def test_asset_accepts_string_ref():
    """asset('source/id') — string handle form."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    a = c.asset("ambientcg/Rock064")
    assert a.source == "ambientcg"
    assert a.material_id == "Rock064"


def test_asset_accepts_kwargs():
    """asset(source=, id=, tier=) — explicit form."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    a = c.asset(source="ambientcg", id="Rock064", tier="2k")
    assert a.source == "ambientcg"
    assert a.material_id == "Rock064"
    assert a.tier == "2k"


def test_asset_legacy_positional_still_works():
    """Three-positional form preserved (existing callers don't break)."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    a = c.asset("ambientcg", "Rock064", "1k")
    assert a.source == "ambientcg"
    assert a.material_id == "Rock064"
    assert a.tier == "1k"


def test_asset_match_with_explicit_tier_overrides_default():
    """Match handle + explicit tier= kwarg → tier from kwarg wins."""
    from mat_vis_client import MatVisClient, Match

    c = MatVisClient()
    m = Match(CORPUS[0])  # tiers=['1k']
    a = c.asset(m, tier="2k")
    assert a.tier == "2k"


def test_asset_malformed_string_ref_raises_value_error():
    """'ambientcg-Rock064' (no slash) is a syntax error, not lookup."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    with pytest.raises(ValueError, match="source/id"):
        c.asset("ambientcg-Rock064")


# ── source did-you-mean ────────────────────────────────────────


def test_source_not_found_includes_did_you_mean():
    """SourceNotFoundError fuzzy-suggests typos like ambient_cg → ambientcg."""
    from mat_vis_client import MatVisClient, SourceNotFoundError

    c = MatVisClient()
    fake_manifest = {
        "sources": {
            "ambientcg": {"tiers": {"1k": {}}},
            "polyhaven": {"tiers": {"1k": {}}},
            "physicallybased": {"tiers": {"scalar": {}}},
        }
    }
    with patch.object(MatVisClient, "manifest", fake_manifest):
        with pytest.raises(SourceNotFoundError) as exc_info:
            c.tiers("ambient_cg")
    err = exc_info.value
    assert "ambientcg" in str(err)
    assert hasattr(err, "candidates")
    assert "ambientcg" in err.candidates


# ── materials() unchanged contract ─────────────────────────────


def test_materials_still_returns_list_of_str():
    """Downstream tests rely on list[str]. Unchanged."""
    from mat_vis_client import MatVisClient

    c = MatVisClient()
    fake_manifest = {"sources": {"ambientcg": {"tiers": {"1k": {}}}}}
    with patch.object(MatVisClient, "manifest", fake_manifest):
        with patch.object(c, "_load_index_raw", return_value=CORPUS):
            ids = c.materials("ambientcg")
    assert ids
    assert all(isinstance(x, str) for x in ids)
    # IDs only, not refs (source/id) — single-source call, source is implicit.
    assert "ambientcg/" not in ids[0]
