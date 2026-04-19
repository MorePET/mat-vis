"""Tests for mat_vis_client.schema — single source of truth for channels.

Enforces that adapter / client maps are *derived* from the registry
rather than hand-maintained duplicates. Any new channel added to the
registry must automatically propagate to every consumer.
"""

from __future__ import annotations


def test_schema_module_exists():
    """schema.py exposes Channel enum, ChannelSpec, and CHANNELS registry."""
    from mat_vis_client import schema

    assert hasattr(schema, "Channel")
    assert hasattr(schema, "ChannelSpec")
    assert hasattr(schema, "CHANNELS")
    assert len(schema.CHANNELS) >= 7  # color, normal, rough, metal, ao, disp, emission


def test_channel_enum_values():
    from mat_vis_client.schema import Channel

    assert Channel.COLOR.value == "color"
    assert Channel.NORMAL.value == "normal"
    assert Channel.ROUGHNESS.value == "roughness"
    assert Channel.METALNESS.value == "metalness"
    assert Channel.AO.value == "ao"
    assert Channel.DISPLACEMENT.value == "displacement"
    assert Channel.EMISSION.value == "emission"


def test_tier_enum_values():
    from mat_vis_client.schema import Tier

    assert Tier.T1K.value == "1k"
    assert Tier.T2K.value == "2k"
    assert Tier.T4K.value == "4k"


def test_channel_spec_has_all_renderer_props():
    """Every ChannelSpec carries props for every supported renderer."""
    from mat_vis_client.schema import CHANNELS

    for spec in CHANNELS:
        assert spec.threejs_prop  # required
        assert spec.mtlx_prop
        assert spec.usd_preview_prop
        assert spec.usd_preview_type in ("color3", "vector3", "float")
        # gltf_prop may be None (metalness/roughness pack into one tex)
        assert isinstance(spec.filename_aliases, tuple)


def test_derived_threejs_map_matches_registry():
    from mat_vis_client.schema import CHANNELS, THREEJS_MAP

    assert set(THREEJS_MAP) == {s.channel.value for s in CHANNELS}
    for s in CHANNELS:
        assert THREEJS_MAP[s.channel.value] == s.threejs_prop


def test_derived_gltf_map_skips_none_props():
    from mat_vis_client.schema import CHANNELS, GLTF_MAP

    for s in CHANNELS:
        if s.gltf_prop is None:
            assert s.channel.value not in GLTF_MAP
        else:
            assert GLTF_MAP[s.channel.value] == s.gltf_prop


def test_derived_usd_preview_map_carries_types():
    from mat_vis_client.schema import CHANNELS, USD_PREVIEW_MAP

    for s in CHANNELS:
        assert USD_PREVIEW_MAP[s.channel.value] == (s.usd_preview_prop, s.usd_preview_type)


def test_filename_to_channel_built_from_aliases():
    from mat_vis_client.schema import CHANNELS, FILENAME_TO_CHANNEL

    for s in CHANNELS:
        for alias in s.filename_aliases:
            assert FILENAME_TO_CHANNEL[alias] == s.channel.value


def test_filename_aliases_cover_known_upstream_names():
    """Regression: old _FILENAME_TO_CHANNEL hardcoded these mappings."""
    from mat_vis_client.schema import FILENAME_TO_CHANNEL

    expected = {
        "basecolor": "color",
        "base_color": "color",
        "diffuse": "color",
        "normal": "normal",
        "roughness": "roughness",
        "specular_roughness": "roughness",
        "metallic": "metalness",
        "metalness": "metalness",
        "occlusion": "ao",
        "ao": "ao",
        "ambientocclusion": "ao",
        "displacement": "displacement",
        "height": "displacement",
        "emission": "emission",
        "emissive": "emission",
    }
    for alias, channel in expected.items():
        assert FILENAME_TO_CHANNEL[alias] == channel, f"{alias!r} expected to map to {channel!r}"


def test_adapters_reference_schema_maps_by_identity():
    """No duplicate maps in adapters — they must be the registry objects.

    If someone re-declares _THREEJS_TEX_MAP / _GLTF_TEX_MAP / _USD_PREVIEW_TEX_MAP
    locally instead of importing from schema, this test fails. This is the
    DRY invariant: one edit in schema = every adapter updated.
    """
    from mat_vis_client import adapters
    from mat_vis_client import schema

    assert adapters._THREEJS_TEX_MAP is schema.THREEJS_MAP
    assert adapters._GLTF_TEX_MAP is schema.GLTF_MAP
    assert adapters._USD_PREVIEW_TEX_MAP is schema.USD_PREVIEW_MAP
    # MTLX_MAP is exported by schema but unused by adapters (MaterialX is
    # built via USD_PREVIEW_MAP); the schema export is for future consumers.
    assert hasattr(schema, "MTLX_MAP")


def test_client_filename_map_is_schema_derived():
    """client.py's _FILENAME_TO_CHANNEL must be the schema-derived one."""
    from mat_vis_client import client
    from mat_vis_client import schema

    assert client._FILENAME_TO_CHANNEL is schema.FILENAME_TO_CHANNEL


def test_registry_is_the_single_source_for_channels():
    """Adding a channel to schema.CHANNELS propagates to all derived maps.

    This is the core DRY invariant enforced by the registry pattern.
    Simulates adding a hypothetical channel and verifies all views
    pick it up through the derivation functions.
    """
    from mat_vis_client.schema import (
        ChannelSpec,
        build_threejs_map,
        build_gltf_map,
        build_usd_preview_map,
        build_mtlx_map,
        build_filename_to_channel,
    )

    # Fake channel added to an ad-hoc registry
    fake_specs = [
        ChannelSpec(
            channel="transmission",  # type: ignore[arg-type]
            threejs_prop="transmissionMap",
            gltf_prop="transmissionTexture",
            mtlx_prop="transmission",
            usd_preview_prop="transmission",
            usd_preview_type="float",
            filename_aliases=("transmission",),
        )
    ]

    tj = build_threejs_map(fake_specs)
    gl = build_gltf_map(fake_specs)
    usd = build_usd_preview_map(fake_specs)
    mx = build_mtlx_map(fake_specs)
    fn = build_filename_to_channel(fake_specs)

    assert tj["transmission"] == "transmissionMap"
    assert gl["transmission"] == "transmissionTexture"
    assert usd["transmission"] == ("transmission", "float")
    assert mx["transmission"] == "transmission"
    assert fn["transmission"] == "transmission"
