"""``Match`` — the unified return type for ``search()`` and ``index()`` (#359).

A dict-subclass: every existing ``entry["mat_vis"]["pbr"][...]`` access keeps
working, ``isinstance(m, dict)`` stays True. The class adds:

- A human-friendly ``__str__`` (one-line summary, not the raw dict dump).
- Stable identity properties: ``id``, ``source``, ``ref`` (the ``"source/id"``
  string handle for ``client.asset(ref)``).
- Namespace pointers: ``mat_vis``, ``pbr``, ``physical``, ``attribution``,
  ``dates``, ``upstream``, ``maps``, ``tiers``.

The properties are deliberately a *small* surface — only identity and
namespace pointers. No PBR field accessors (``m.roughness``, ``m.metalness``,
etc.) because those would duplicate the substrate shape and bind ``Match``
to PBR field churn (#316 + #340 added 8 fields in two months). Use
``m.pbr["roughness"]`` for the actual scalars; the substrate dict shape is
the source of truth.
"""

from __future__ import annotations

from typing import Any


class Match(dict):
    """Substrate Layer-1 entry with smart presentation + identity props.

    Inherits from ``dict``; all existing key-access patterns work. Adds
    convenience properties for the *common* identity + namespace cases.
    """

    # ── identity ─────────────────────────────────────────────────

    @property
    def id(self) -> str:
        return self.get("id", "")

    @property
    def source(self) -> str:
        return self.get("source", "")

    @property
    def ref(self) -> str:
        """Globally-unique fetch handle: ``"<source>/<id>"``.

        Pass directly to :meth:`MatVisClient.asset` — accepts this string
        form alongside the ``Match`` itself and explicit kwargs.
        """
        return f"{self.source}/{self.id}"

    # ── tier coverage ────────────────────────────────────────────

    @property
    def tiers(self) -> list[str]:
        """The tiers this material is staged at (``available_tiers``)."""
        v = self.get("available_tiers")
        return list(v) if v else []

    # ── namespace pointers ───────────────────────────────────────
    #
    # These return the live nested dicts; mutation is the caller's
    # problem (same as the existing ``entry["mat_vis"][...]`` contract).
    # Each returns ``{}`` when the namespace is absent so chained access
    # doesn't crash on partial entries (e.g. failed records).

    @property
    def mat_vis(self) -> dict[str, Any]:
        return self.get("mat_vis") or {}

    @property
    def pbr(self) -> dict[str, Any]:
        return self.mat_vis.get("pbr") or {}

    @property
    def physical(self) -> dict[str, Any]:
        return self.mat_vis.get("physical") or {}

    @property
    def attribution(self) -> dict[str, Any]:
        return self.mat_vis.get("attribution") or {}

    @property
    def dates(self) -> dict[str, Any]:
        return self.mat_vis.get("dates") or {}

    @property
    def upstream(self) -> dict[str, Any] | None:
        """Verbatim Layer-2 mirror (ADR-0011) — None when absent.

        Unlike the other pointers (which return ``{}`` for missing
        namespaces), ``upstream`` returns ``None`` because the *absence*
        of an upstream block is itself meaningful: pre-Phase-C entries
        and stripped views (default for :meth:`MatVisClient.search` /
        :meth:`MatVisClient.index`) just don't have it.
        """
        v = self.get("upstream")
        return v if isinstance(v, dict) else None

    @property
    def maps(self) -> list[str]:
        v = self.get("maps")
        return list(v) if v else []

    # ── presentation ─────────────────────────────────────────────

    def __str__(self) -> str:
        """One-line human summary — ``ref + category + scalars + tiers``.

        Example: ``ambientcg/Brass001  metal  r=0.10 m=1.00  tiers=[1k,2k]``

        Falls back gracefully when fields are missing (failed records,
        partial entries).
        """
        cat = self.mat_vis.get("category") or "?"
        pbr = self.pbr
        r = pbr.get("roughness")
        m = pbr.get("metalness")

        bits = [self.ref, cat]
        if r is not None or m is not None:
            r_str = f"r={r:.2f}" if r is not None else "r=?"
            m_str = f"m={m:.2f}" if m is not None else "m=?"
            bits.append(f"{r_str} {m_str}")
        if self.tiers:
            bits.append(f"tiers=[{','.join(self.tiers)}]")
        return "  ".join(bits)

    def __repr__(self) -> str:
        # Distinct from dict's repr so REPL output stays tight.
        return f"Match({self.ref!r})"
