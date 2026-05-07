"""Tests for typed mat_vis_client errors (#85 item 2).

Every error surfaced to callers should be a ``MatVisError`` subclass —
no bare ``urllib.error.HTTPError`` / ``urllib.error.URLError`` leakage.
Error types carry structured attributes (url, code, kind, key, available)
so callers can branch without string-matching messages.
"""

from __future__ import annotations

from unittest.mock import patch
import urllib.error

import pytest

from mat_vis_client import MatVisError, RateLimitError
from mat_vis_client.client import _lookup


# ── Error hierarchy ────────────────────────────────────────────


def test_not_found_error_is_matvis_error():
    from mat_vis_client import NotFoundError

    assert issubclass(NotFoundError, MatVisError)


def test_material_not_found_error_hierarchy():
    from mat_vis_client import MaterialNotFoundError, NotFoundError

    assert issubclass(MaterialNotFoundError, NotFoundError)


def test_tier_source_channel_not_found_hierarchy():
    from mat_vis_client import (
        ChannelNotFoundError,
        NotFoundError,
        SourceNotFoundError,
        TierNotFoundError,
    )

    assert issubclass(SourceNotFoundError, NotFoundError)
    assert issubclass(TierNotFoundError, NotFoundError)
    assert issubclass(ChannelNotFoundError, NotFoundError)


def test_http_fetch_error_is_matvis_error():
    from mat_vis_client import HTTPFetchError

    assert issubclass(HTTPFetchError, MatVisError)


def test_network_error_is_matvis_error():
    from mat_vis_client import NetworkError

    assert issubclass(NetworkError, MatVisError)


# ── NotFoundError structure ────────────────────────────────────


def test_material_not_found_carries_structured_fields():
    from mat_vis_client import MaterialNotFoundError

    err = MaterialNotFoundError(
        key="Rock999", available=["Rock064", "Rock063"], context="ambientcg/1k"
    )
    assert err.key == "Rock999"
    assert err.available == ["Rock064", "Rock063"]
    assert err.context == "ambientcg/1k"
    # Message includes available list (actionable)
    msg = str(err)
    assert "Rock999" in msg
    assert "Rock064" in msg  # "did you mean" hint


# ── _lookup raises typed subclasses by kind ────────────────────


def test_lookup_raises_material_not_found_for_material_kind():
    from mat_vis_client import MaterialNotFoundError

    with pytest.raises(MaterialNotFoundError) as exc:
        _lookup({"Rock064": {}}, "Rock999", kind="material", context="ambientcg/1k")
    assert exc.value.key == "Rock999"
    assert "Rock064" in exc.value.available


def test_lookup_raises_source_not_found_for_source_kind():
    from mat_vis_client import SourceNotFoundError

    with pytest.raises(SourceNotFoundError):
        _lookup({"ambientcg": {}}, "polyhaven", kind="source")


def test_lookup_raises_tier_not_found_for_tier_kind():
    from mat_vis_client import TierNotFoundError

    with pytest.raises(TierNotFoundError):
        _lookup({"1k": {}}, "8k", kind="tier")


def test_lookup_raises_channel_not_found_for_channel_kind():
    from mat_vis_client import ChannelNotFoundError

    with pytest.raises(ChannelNotFoundError):
        _lookup({"color": {}}, "colosr", kind="channel", context="Rock064")


def test_lookup_falls_back_to_matvis_error_for_unknown_kind():
    """Backwards compat: unknown kinds still raise MatVisError."""
    with pytest.raises(MatVisError):
        _lookup({"a": 1}, "b", kind="widget")


# ── HTTP errors are wrapped ────────────────────────────────────


def test_get_wraps_http_error_into_http_fetch_error():
    """_get() must translate urllib.HTTPError into HTTPFetchError for non-rate-limit codes."""
    from mat_vis_client import HTTPFetchError
    from mat_vis_client.client import _get

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(HTTPFetchError) as exc:
            _get("https://example.com/missing.png")
        assert exc.value.code == 404
        assert exc.value.url == "https://example.com/missing.png"


def test_get_wraps_url_error_into_network_error():
    """_get() must translate urllib.URLError (after retries) into NetworkError."""
    from mat_vis_client import NetworkError
    from mat_vis_client.client import _get

    def fake_urlopen(req, timeout=60):
        raise urllib.error.URLError("connection reset")

    # patch sleep so retries are instant
    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("time.sleep"),
    ):
        with pytest.raises(NetworkError):
            _get("https://example.com/x")


def test_rate_limit_still_raises_rate_limit_error():
    """Rate-limited 429 after retries → RateLimitError (unchanged behavior)."""
    from mat_vis_client.client import _get

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {"Retry-After": "1"}, None
        )

    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("time.sleep"),
    ):
        with pytest.raises(RateLimitError):
            _get("https://example.com/x")


# ── Name-resolution errors (mat-vis #141, #143, #144) ─────────


def _client_with_index(rowmap_materials: dict, index_entries: list[dict]):
    """Build a MatVisClient with a baked v3 catalog, no network.

    Per-file substrate (#186 / ADR-0012): the rowmap is gone — material
    staging is signaled by ``available_tiers`` on each catalog entry.
    The ``rowmap_materials`` arg is kept for back-compat with existing
    tests: a material id appears in ``rowmap_materials`` ⇒ its catalog
    entry gets ``available_tiers=["1k"]``; otherwise the entry has no
    tier and ``MaterialNotStagedError`` fires.
    """
    from mat_vis_client import MatVisClient
    import tempfile
    from pathlib import Path

    manifest = {
        "schema_version": 3,
        "release_tag": "v2026.04.0",
        "sources": {
            "gpuopen": {
                "catalog": "gpuopen.json",
                "tiers": {"1k": {"complete": True}},
            }
        },
    }

    enriched: list[dict] = []
    for entry in index_entries:
        out = dict(entry)
        out.setdefault("source", "gpuopen")
        if entry.get("id") in rowmap_materials:
            out["available_tiers"] = ["1k"]
            out["maps"] = sorted(rowmap_materials[entry["id"]].keys())
        else:
            out["available_tiers"] = []
            out["maps"] = []
        enriched.append(out)

    tmp = Path(tempfile.mkdtemp(prefix="mat-vis-test-resolver-"))
    client = MatVisClient(cache_dir=tmp)
    client._manifest = manifest
    client._indexes = {"gpuopen": enriched}
    # Pre-mark the tier-complete sentinel as seen so fetch_texture skips
    # the live HEAD probe in unit tests.
    client._tier_complete[("gpuopen", "1k")] = True
    return client


def test_unknown_material_error_raised_for_unknown_id():
    """Not in rowmap, not in index → UnknownMaterialError."""
    from mat_vis_client import UnknownMaterialError, MaterialNotFoundError

    client = _client_with_index(
        rowmap_materials={"25b88a68": {"color": {"offset": 0, "length": 10}}},
        index_entries=[{"id": "25b88a68", "name": "Aluminum Corrugated"}],
    )
    with pytest.raises(UnknownMaterialError) as exc:
        client.fetch_all_textures("gpuopen", "bogus-id", tier="1k")
    assert exc.value.key == "bogus-id"
    # Subclass of MaterialNotFoundError so legacy guards still fire
    assert isinstance(exc.value, MaterialNotFoundError)


def test_material_not_staged_error_when_id_in_index_but_not_rowmap():
    """In index but not staged in rowmap → MaterialNotStagedError."""
    from mat_vis_client import MaterialNotStagedError

    client = _client_with_index(
        rowmap_materials={},  # nothing baked yet
        index_entries=[{"id": "25b88a68", "name": "Aluminum Corrugated"}],
    )
    with pytest.raises(MaterialNotStagedError) as exc:
        client.fetch_all_textures("gpuopen", "25b88a68", tier="1k")
    assert exc.value.source == "gpuopen"
    assert exc.value.material_id == "25b88a68"
    assert exc.value.tier == "1k"


def test_name_resolves_to_uuid_and_fetches_channels():
    """py-mat#90's 'Best option': fetch by name → resolves to UUID → works."""
    from unittest.mock import patch

    client = _client_with_index(
        rowmap_materials={
            "25b88a68": {"color": {"offset": 0, "length": 10, "tar_file": "gpuopen-1k.tar"}}
        },
        index_entries=[{"id": "25b88a68", "mat_vis": {"name": "Aluminum Corrugated"}}],
    )
    with patch.object(client, "fetch_texture", return_value=b"\x89PNG") as ft:
        out = client.fetch_all_textures("gpuopen", "Aluminum Corrugated", tier="1k")
    assert out == {"color": b"\x89PNG"}
    ft.assert_called_once_with("gpuopen", "25b88a68", "color", "1k")


def test_name_match_is_normalized_case_and_whitespace():
    """Name matching is NFKC + casefold + strip."""
    from unittest.mock import patch

    client = _client_with_index(
        rowmap_materials={"abc": {"color": {"offset": 0, "length": 10}}},
        index_entries=[{"id": "abc", "mat_vis": {"name": "Aluminum Corrugated"}}],
    )
    with patch.object(client, "fetch_texture", return_value=b""):
        client.fetch_all_textures("gpuopen", "  aluminum CORRUGATED  ", tier="1k")


def test_ambiguous_material_error_lists_candidates():
    """Two index entries share a normalized name → AmbiguousMaterialError."""
    from mat_vis_client import AmbiguousMaterialError

    client = _client_with_index(
        rowmap_materials={
            "uuid-a": {"color": {"offset": 0, "length": 10}},
            "uuid-b": {"color": {"offset": 0, "length": 10}},
        },
        index_entries=[
            {"id": "uuid-a", "mat_vis": {"name": "Brick Wall"}},
            {"id": "uuid-b", "mat_vis": {"name": "brick wall"}},
        ],
    )
    with pytest.raises(AmbiguousMaterialError) as exc:
        client.fetch_all_textures("gpuopen", "Brick Wall", tier="1k")
    assert exc.value.source == "gpuopen"
    # #286: candidates list is now human names (with id fallback) — UUIDs
    # were unhelpful when each entry had a perfectly good display name.
    assert sorted(exc.value.candidates) == ["Brick Wall", "brick wall"]
    assert "Brick Wall" in str(exc.value) and "brick wall" in str(exc.value)


# ── fetch_texture surfaces typed errors ────────────────────────


def test_fetch_texture_404_raises_material_not_found_or_http_fetch_error():
    """A 404 during fetch must surface as HTTPFetchError (not urllib leakage)."""
    from mat_vis_client import HTTPFetchError, MatVisClient

    MOCK_MANIFEST = {
        "schema_version": 3,
        "release_tag": "v2026.04.0",
        "sources": {
            "ambientcg": {
                "catalog": "ambientcg.json",
                "tiers": {"1k": {"complete": True}},
            },
        },
    }

    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="mat-vis-test-errors-"))
    client = MatVisClient(cache_dir=tmp)
    # Inject manifest + catalog + sentinel so no network call happens for
    # the metadata path; only the texture GET hits urlopen and 404s.
    client._manifest = MOCK_MANIFEST
    client._indexes = {
        "ambientcg": [
            {
                "id": "Rock064",
                "source": "ambientcg",
                "mat_vis": {"name": "Rock064", "category": "stone"},
                "available_tiers": ["1k"],
                "maps": ["color"],
            }
        ]
    }
    client._tier_complete[("ambientcg", "1k")] = True

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("time.sleep"),
    ):
        with pytest.raises(HTTPFetchError) as exc:
            client.fetch_texture("ambientcg", "Rock064", "color", tier="1k")
        # Must NOT be a bare urllib error
        assert not isinstance(exc.value, urllib.error.HTTPError)
        # But HTTPFetchError carries the code
        assert exc.value.code == 404


# ── Hotfix #280: MaterialNotStagedError keeps user-given name ──


def test_material_not_staged_error_preserves_user_given_name():
    """When the user passes a human name (not the UUID) and the resolved
    entry exists but is unstaged, the raised error should preserve the
    original name they typed — losing it makes batch debugging painful
    (#280).
    """
    from mat_vis_client import MaterialNotStagedError

    client = _client_with_index(
        rowmap_materials={},  # nothing baked → unstaged
        index_entries=[
            {"id": "34f2c1f9-aaaa-bbbb-cccc-deadbeefcafe", "mat_vis": {"name": "Chrome"}},
        ],
    )
    with pytest.raises(MaterialNotStagedError) as exc:
        client.fetch_all_textures("gpuopen", "Chrome", tier="1k")
    # Resolved id is still the canonical attribute (existing API).
    assert exc.value.material_id == "34f2c1f9-aaaa-bbbb-cccc-deadbeefcafe"
    # New: original_name field carries the user's input.
    assert exc.value.original_name == "Chrome"
    # And the error string mentions both — so a batch log shows which
    # human-named material in the run hit this.
    msg = str(exc.value)
    assert "Chrome" in msg
    assert "34f2c1f9-aaaa-bbbb-cccc-deadbeefcafe" in msg


def test_material_not_staged_error_direct_uuid_unchanged():
    """When the user passes the UUID directly, original_name is None and
    the message is the legacy single-id form (back-compat)."""
    from mat_vis_client import MaterialNotStagedError

    client = _client_with_index(
        rowmap_materials={},
        index_entries=[{"id": "25b88a68", "name": "Aluminum"}],
    )
    with pytest.raises(MaterialNotStagedError) as exc:
        client.fetch_all_textures("gpuopen", "25b88a68", tier="1k")
    assert exc.value.material_id == "25b88a68"
    assert exc.value.original_name is None
    msg = str(exc.value)
    # Legacy phrasing: no "(resolved id ...)" parenthetical.
    assert "resolved id" not in msg


# ── Hotfix #284: ambientCG/polyhaven flat-v2 name addressability ──


def test_resolve_name_falls_back_to_top_level_name_field():
    """ambientcg/polyhaven catalogs use a flat v2 schema with top-level
    ``name`` (no ``mat_vis`` envelope). Name lookup must hit those too
    (#284).
    """
    from unittest.mock import patch

    client = _client_with_index(
        rowmap_materials={"Bricks104": {"color": {"offset": 0, "length": 10}}},
        # NB: no mat_vis envelope; flat top-level name field.
        index_entries=[{"id": "Bricks104", "name": "Bricks 104"}],
    )
    with patch.object(client, "fetch_texture", return_value=b"\x89PNG") as ft:
        out = client.fetch_all_textures("gpuopen", "Bricks 104", tier="1k")
    assert out == {"color": b"\x89PNG"}
    ft.assert_called_once_with("gpuopen", "Bricks104", "color", "1k")


def test_resolve_name_prefers_mat_vis_name_over_top_level():
    """When both ``mat_vis.name`` and top-level ``name`` are present, the
    canonical envelope wins (v3 entries shouldn't regress)."""
    from unittest.mock import patch

    client = _client_with_index(
        rowmap_materials={"abc": {"color": {"offset": 0, "length": 10}}},
        index_entries=[
            {
                "id": "abc",
                "name": "Top Level",
                "mat_vis": {"name": "Envelope Name"},
            }
        ],
    )
    with patch.object(client, "fetch_texture", return_value=b""):
        # Envelope name resolves
        client.fetch_all_textures("gpuopen", "Envelope Name", tier="1k")


# ── Hotfix #286: error lists show names + close-matches, not UUIDs ──


def test_unknown_material_error_lists_names_not_uuids_with_close_matches():
    """When the user typos a name (the bernhard repro: ``"TH Large Red
    Bricks"`` missing the colon), the surfaced ``available`` list and
    the rendered message should contain human names — and prioritize
    a close-match suggestion (#286).
    """
    import re
    from mat_vis_client import UnknownMaterialError

    client = _client_with_index(
        rowmap_materials={
            "uuid-aa-aaaaaaaa-aaaa-aaaaaaaaaaaa": {"color": {"offset": 0, "length": 10}},
            "uuid-bb-bbbbbbbb-bbbb-bbbbbbbbbbbb": {"color": {"offset": 0, "length": 10}},
            "uuid-cc-cccccccc-cccc-cccccccccccc": {"color": {"offset": 0, "length": 10}},
        },
        index_entries=[
            {
                "id": "uuid-aa-aaaaaaaa-aaaa-aaaaaaaaaaaa",
                "mat_vis": {"name": "TH: Large Red Bricks"},
            },
            {
                "id": "uuid-bb-bbbbbbbb-bbbb-bbbbbbbbbbbb",
                "mat_vis": {"name": "TH: Small Red Bricks"},
            },
            {"id": "uuid-cc-cccccccc-cccc-cccccccccccc", "mat_vis": {"name": "Concrete Slab"}},
        ],
    )
    with pytest.raises(UnknownMaterialError) as exc:
        client.fetch_all_textures("gpuopen", "TH Large Red Bricks", tier="1k")

    msg = str(exc.value)
    # Close match should appear in message (and ideally before any
    # full-list dump).
    assert "TH: Large Red Bricks" in msg
    # No raw UUID-looking strings should be in the message.
    assert not re.search(r"uuid-[a-f0-9-]{8,}", msg)
    # Available list should also be names, not UUIDs.
    assert "TH: Large Red Bricks" in exc.value.available
    assert all(not a.startswith("uuid-") for a in exc.value.available)


def test_unknown_material_error_caps_full_list_when_huge():
    """A 200-entry catalog should not vomit all 200 names into the
    message — cap it (#286)."""
    from mat_vis_client import UnknownMaterialError

    rowmap = {f"uuid-{i:03d}": {"color": {"offset": 0, "length": 10}} for i in range(200)}
    entries = [
        {"id": f"uuid-{i:03d}", "mat_vis": {"name": f"NameThing{i:03d}"}} for i in range(200)
    ]
    client = _client_with_index(rowmap_materials=rowmap, index_entries=entries)
    with pytest.raises(UnknownMaterialError) as exc:
        client.fetch_all_textures("gpuopen", "totally-unknown-key-xyz", tier="1k")
    msg = str(exc.value)
    # Sentinel that we truncated (allow either format).
    assert "more)" in msg or "..." in msg
    # And the full message shouldn't contain *every* name.
    assert msg.count("NameThing") < 200


def test_ambiguous_material_error_lists_names():
    """Ambiguous-name candidates should be listed by human name when
    available, not raw ids (#286)."""
    from mat_vis_client import AmbiguousMaterialError

    client = _client_with_index(
        rowmap_materials={
            "uuid-x": {"color": {"offset": 0, "length": 10}},
            "uuid-y": {"color": {"offset": 0, "length": 10}},
        },
        index_entries=[
            # Two distinct flat-v2 entries that normalize to the same name.
            {"id": "uuid-x", "name": "Brick Wall"},
            {"id": "uuid-y", "name": "brick wall"},
        ],
    )
    with pytest.raises(AmbiguousMaterialError) as exc:
        client.fetch_all_textures("gpuopen", "Brick Wall", tier="1k")
    # Candidates should be names, not UUIDs.
    assert any("Brick" in c for c in exc.value.candidates)


# ── mat-vis#332: MaterialNotStagedError should list available tiers ──


# ── mat-vis#332: MaterialNotStagedError lists available tiers ──


def test_material_not_staged_error_lists_available_tiers() -> None:
    """``MaterialNotStagedError`` includes an ``Available tiers: [...]``
    line so the user knows which tiers ARE staged.

    bernhard mat-vis#311 sub-bullet "Unclear error messages":
    expectation was ``... is not staged for tier '3k'.
    Available tiers: ['1k', '2k', ...]``. Pre-#332 the message said
    "Needs a re-bake" (actionable only for maintainers).

    Closes mat-vis#332.
    """
    from mat_vis_client import MaterialNotStagedError

    err = MaterialNotStagedError(
        source="gpuopen",
        material_id="c12edfda-a5bd-4469-8147-4a6540a0a213",
        tier="3k",
        original_name="Aluminum Brushed",
        available=["1k", "2k"],
    )
    msg = str(err)
    assert "Available tiers:" in msg
    assert "1k" in msg and "2k" in msg
    # Original "Needs a re-bake" wording is replaced when alternatives
    # exist — bernhard's complaint was that line was non-actionable.
    assert "Needs a re-bake" not in msg


def test_material_not_staged_error_falls_back_to_rebake_when_no_alternatives() -> None:
    """When the material isn't staged at any tier, the message keeps
    its original 'Needs a re-bake' wording — that's still the only
    actionable hint for the maintainer path."""
    from mat_vis_client import MaterialNotStagedError

    err = MaterialNotStagedError(
        source="gpuopen",
        material_id="some-uuid",
        tier="1k",
        available=[],
    )
    msg = str(err)
    assert "Needs a re-bake" in msg
    assert "Available tiers:" not in msg


def test_material_not_staged_error_carries_available_attribute() -> None:
    """Programmatic consumers (pymat, build123d) can read ``.available``
    to render alternate-tier suggestions in their UI."""
    from mat_vis_client import MaterialNotStagedError

    err = MaterialNotStagedError(
        source="gpuopen",
        material_id="x",
        tier="3k",
        available=["1k", "2k"],
    )
    assert err.available == ["1k", "2k"]
