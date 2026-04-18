"""Canonical taxonomy loaded from the packaged index-schema.json.

Source of truth: ``docs/specs/index-schema.json``. Synced to each package's
``_spec/`` subdirectory by ``scripts/sync-spec.py`` (pre-commit hook). This
module loads the packaged copy via ``importlib.resources`` so it works after
``pip install`` without repo access.

Downstream modules should import taxonomies from here — never hardcode.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files


@cache
def _schema() -> dict:
    """Load and cache the packaged index-schema.json."""
    data = files("mat_vis_baker._spec").joinpath("index-schema.json").read_text(encoding="utf-8")
    return json.loads(data)


@cache
def CATEGORIES() -> tuple[str, ...]:  # noqa: N802 — public, stable accessor
    """The ten canonical material categories."""
    return tuple(_schema()["items"]["properties"]["category"]["enum"])


@cache
def CHANNELS() -> tuple[str, ...]:  # noqa: N802
    """The seven canonical texture channels, in the order the baker writes them."""
    return tuple(_schema()["items"]["properties"]["maps"]["items"]["enum"])


@cache
def SOURCES() -> tuple[str, ...]:  # noqa: N802
    """The four upstream source identifiers."""
    return tuple(_schema()["items"]["properties"]["source"]["enum"])
