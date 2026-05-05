"""VisAsset — ergonomic bundle of (identity + lazy scalars + lazy textures + adapters).

Two-layer shape (mat-vis#93): :class:`VisAsset` is the ergonomic class that
binds the identity tuple ``(source, material_id, tier)`` to a client and a
lazy view of its scalars and textures. The underlying free functions —
:func:`mat_vis_client.adapters.to_threejs`,
:func:`mat_vis_client.adapters.to_gltf`,
:func:`mat_vis_client.adapters.export_mtlx` — remain the stable primitive
layer (the JS/Rust port boundary). :class:`VisAsset` adapter methods call
those primitives with identity-bound args.

Mirrors the ``requests.Session`` / ``requests.get()`` and
``subprocess.Popen`` / ``subprocess.run()`` two-layer pattern: class as
ergonomic surface, free functions as port-friendly primitives.

Identity is **immutable** — assigning to ``source``/``material_id``/``tier``
raises ``AttributeError``. Use :meth:`VisAsset.with_tier` to spawn a new
instance for a different tier. Equality and hashing are identity-only
(same triple, even across distinct client instances).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mat_vis_client.client import MatVisClient, MtlxSource

_FROZEN = ("_source", "_material_id", "_tier")


class VisAsset:
    """Bundle of (identity + lazy scalars + lazy textures + adapters).

    Two-layer shape: VisAsset is the ergonomic class; the underlying
    ``to_threejs`` / ``to_gltf`` / ``export_mtlx`` free functions remain
    the stable primitives. Adapter methods on this class call those
    primitives with identity-bound args.
    """

    __slots__ = (
        "_client",
        "_source",
        "_material_id",
        "_tier",
        "_scalars_cache",
        "_textures_cache",
        "_initialized",
    )

    def __init__(
        self,
        client: MatVisClient,
        source: str,
        material_id: str,
        tier: str,
    ) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_material_id", material_id)
        object.__setattr__(self, "_tier", tier)
        object.__setattr__(self, "_scalars_cache", None)
        object.__setattr__(self, "_textures_cache", None)
        object.__setattr__(self, "_initialized", True)

    @classmethod
    def from_client(
        cls,
        client: MatVisClient,
        source: str,
        material_id: str,
        tier: str = "1k",
    ) -> VisAsset:
        return cls(client, source, material_id, tier)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_initialized", False) and name in _FROZEN:
            raise AttributeError(f"{name} is immutable; use with_tier() / new VisAsset")
        object.__setattr__(self, name, value)

    @property
    def source(self) -> str:
        return self._source

    @property
    def material_id(self) -> str:
        return self._material_id

    @property
    def tier(self) -> str:
        return self._tier

    def with_tier(self, tier: str) -> VisAsset:
        """Return a new :class:`VisAsset` with the same source/material_id but a different tier."""
        return VisAsset(self._client, self._source, self._material_id, tier)

    @property
    def scalars(self) -> dict:
        """Lazy PBR scalars (cached after first access).

        Calls :meth:`MatVisClient._scalars_for` exactly once per instance.
        """
        if self._scalars_cache is None:
            object.__setattr__(
                self,
                "_scalars_cache",
                self._client._scalars_for(self._source, self._material_id),
            )
        return self._scalars_cache

    @property
    def textures(self) -> dict[str, bytes]:
        """Lazy channel -> PNG bytes mapping (cached after first access).

        Calls :meth:`MatVisClient.fetch_all_textures` exactly once per instance.
        """
        if self._textures_cache is None:
            object.__setattr__(
                self,
                "_textures_cache",
                self._client.fetch_all_textures(self._source, self._material_id, self._tier),
            )
        return self._textures_cache

    def to_threejs(self) -> dict:
        """Return a Three.js ``MeshPhysicalMaterial`` parameter dict.

        Wraps :func:`mat_vis_client.adapters.to_threejs` with this asset's
        identity-bound scalars and textures.
        """
        from mat_vis_client.adapters import to_threejs

        return to_threejs(self.scalars, self.textures)

    def to_gltf(self) -> dict:
        """Return a glTF 2.0 material dict.

        Wraps :func:`mat_vis_client.adapters.to_gltf` with this asset's
        identity-bound scalars and textures.
        """
        from mat_vis_client.adapters import to_gltf

        return to_gltf(self.scalars, self.textures)

    def to_mtlx(self) -> MtlxSource:
        """Return a fresh :class:`MtlxSource` for this asset's identity.

        Composition, not replacement: ``MtlxSource`` remains the public
        MaterialX façade. This is the recommended entry point.
        """
        from mat_vis_client.client import MtlxSource

        return MtlxSource(self._client, self._source, self._material_id, self._tier)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VisAsset) and (
            self._source,
            self._material_id,
            self._tier,
        ) == (other._source, other._material_id, other._tier)

    def __hash__(self) -> int:
        return hash((self._source, self._material_id, self._tier))

    def __repr__(self) -> str:
        return f"VisAsset({self._source!r}, {self._material_id!r}, tier={self._tier!r})"
