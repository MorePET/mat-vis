"""Shared pytest fixtures for the Python reference client test suite.

Hosts the ``live_client`` fixture that points a fresh ``MatVisClient``
at the prod HF revision with a temp cache. The ``@live`` skip marker
and ``LIVE_TAG`` default are imported from ``tests._live`` (kept in a
plain module so the symbols can be referenced at class-decorator scope
without relying on conftest reimport tricks).

Pre-#274 both this dir's ``test_client.py`` and the now-deleted
top-level copy carried their own ``live_client`` fixtures and
``LIVE_TAG`` defaults; the nested one had drifted to ``v2026.04.1``
after the prod bump.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mat_vis_client import MatVisClient

from tests._live import LIVE_TAG


@pytest.fixture
def live_client():
    """Client pointed at the prod HF revision with a temp cache."""
    with tempfile.TemporaryDirectory() as tmp:
        yield MatVisClient(tag=LIVE_TAG, cache_dir=Path(tmp))
