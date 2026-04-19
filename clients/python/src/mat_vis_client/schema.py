"""Single source of truth for mat-vis channels, tiers, and renderer mappings.

One registry (``CHANNELS``) drives every derived view — Three.js props,
glTF props, MaterialX props, USD preview names, and upstream filename
aliases. Adding a channel = one edit.

Consumers import the derived maps directly; they are built once at
import time and shared by identity so there is one authoritative copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Channel(StrEnum):
    """mat-vis canonical channel names."""

    COLOR = "color"
    NORMAL = "normal"
    ROUGHNESS = "roughness"
    METALNESS = "metalness"
    AO = "ao"
    DISPLACEMENT = "displacement"
    EMISSION = "emission"


class Tier(StrEnum):
    """Resolution tiers published by the baker."""

    T1K = "1k"
    T2K = "2k"
    T4K = "4k"


@dataclass(frozen=True)
class ChannelSpec:
    """Per-channel metadata for every renderer mat-vis targets.

    gltf_prop is ``None`` for metalness/roughness because glTF 2.0 packs
    those two channels into a single ``metallicRoughnessTexture`` — there
    is no standalone per-channel slot.
    """

    channel: Channel
    threejs_prop: str
    gltf_prop: str | None
    mtlx_prop: str
    usd_preview_prop: str
    usd_preview_type: str  # "color3" | "vector3" | "float"
    filename_aliases: tuple[str, ...] = field(default_factory=tuple)


CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        channel=Channel.COLOR,
        threejs_prop="map",
        gltf_prop="baseColorTexture",
        mtlx_prop="base_color",
        usd_preview_prop="diffuseColor",
        usd_preview_type="color3",
        filename_aliases=("basecolor", "base_color", "diffuse", "color"),
    ),
    ChannelSpec(
        channel=Channel.NORMAL,
        threejs_prop="normalMap",
        gltf_prop="normalTexture",
        mtlx_prop="normal",
        usd_preview_prop="normal",
        usd_preview_type="vector3",
        filename_aliases=("normal",),
    ),
    ChannelSpec(
        channel=Channel.ROUGHNESS,
        threejs_prop="roughnessMap",
        gltf_prop=None,  # packed into metallicRoughnessTexture
        mtlx_prop="specular_roughness",
        usd_preview_prop="roughness",
        usd_preview_type="float",
        filename_aliases=("roughness", "specular_roughness"),
    ),
    ChannelSpec(
        channel=Channel.METALNESS,
        threejs_prop="metalnessMap",
        gltf_prop=None,  # packed into metallicRoughnessTexture
        mtlx_prop="metalness",
        usd_preview_prop="metallic",
        usd_preview_type="float",
        filename_aliases=("metallic", "metalness"),
    ),
    ChannelSpec(
        channel=Channel.AO,
        threejs_prop="aoMap",
        gltf_prop="occlusionTexture",
        mtlx_prop="occlusion",
        usd_preview_prop="occlusion",
        usd_preview_type="float",
        filename_aliases=("occlusion", "ao", "ambientocclusion"),
    ),
    ChannelSpec(
        channel=Channel.DISPLACEMENT,
        threejs_prop="displacementMap",
        gltf_prop=None,
        mtlx_prop="displacement",
        usd_preview_prop="displacement",
        usd_preview_type="float",
        filename_aliases=("displacement", "height"),
    ),
    ChannelSpec(
        channel=Channel.EMISSION,
        threejs_prop="emissiveMap",
        gltf_prop="emissiveTexture",
        mtlx_prop="emission_color",
        usd_preview_prop="emissiveColor",
        usd_preview_type="color3",
        filename_aliases=("emission", "emissive"),
    ),
)


def _key(spec: ChannelSpec) -> str:
    """Channel registry key — accept plain strings in tests, Channel in prod."""
    return spec.channel.value if isinstance(spec.channel, Channel) else str(spec.channel)


def build_threejs_map(specs) -> dict[str, str]:
    return {_key(s): s.threejs_prop for s in specs}


def build_gltf_map(specs) -> dict[str, str]:
    return {_key(s): s.gltf_prop for s in specs if s.gltf_prop is not None}


def build_mtlx_map(specs) -> dict[str, str]:
    return {_key(s): s.mtlx_prop for s in specs}


def build_usd_preview_map(specs) -> dict[str, tuple[str, str]]:
    return {_key(s): (s.usd_preview_prop, s.usd_preview_type) for s in specs}


def build_filename_to_channel(specs) -> dict[str, str]:
    return {alias: _key(s) for s in specs for alias in s.filename_aliases}


CHANNELS_BY_NAME: dict[str, ChannelSpec] = {_key(s): s for s in CHANNELS}
THREEJS_MAP: dict[str, str] = build_threejs_map(CHANNELS)
GLTF_MAP: dict[str, str] = build_gltf_map(CHANNELS)
MTLX_MAP: dict[str, str] = build_mtlx_map(CHANNELS)
USD_PREVIEW_MAP: dict[str, tuple[str, str]] = build_usd_preview_map(CHANNELS)
FILENAME_TO_CHANNEL: dict[str, str] = build_filename_to_channel(CHANNELS)


__all__ = [
    "CHANNELS",
    "CHANNELS_BY_NAME",
    "Channel",
    "ChannelSpec",
    "FILENAME_TO_CHANNEL",
    "GLTF_MAP",
    "MTLX_MAP",
    "THREEJS_MAP",
    "Tier",
    "USD_PREVIEW_MAP",
    "build_filename_to_channel",
    "build_gltf_map",
    "build_mtlx_map",
    "build_threejs_map",
    "build_usd_preview_map",
]
