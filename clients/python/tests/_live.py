"""Shared live-test config for the Python reference client suite.

Hosts the single source of truth for:

* the ``@live`` skip marker (set ``MAT_VIS_LIVE_TESTS=1`` to run);
* the ``LIVE_TAG`` default (override via ``MAT_VIS_LIVE_TAG``).

Until #274 these lived as duplicates in both ``test_client.py`` files,
and the nested copy drifted to ``v2026.04.1`` after the v2026.04.2
bump. Centralising here makes "which tag does this hit" a one-line
answer. The matching ``live_client`` fixture lives in ``conftest.py``
so pytest can auto-inject it without an explicit import.

Underscore prefix marks this as internal to the suite; pytest will
not collect it as a test module.
"""

from __future__ import annotations

import os

import pytest

# Skip-by-default marker for tests that hit the real HF dataset.
live = pytest.mark.skipif(
    os.environ.get("MAT_VIS_LIVE_TESTS") != "1",
    reason=(
        "set MAT_VIS_LIVE_TESTS=1 to run live tests against the prod HF dataset. "
        "Disabled by default until prod is rebaked under the per-file substrate "
        "(#186 / ADR-0012); the current prod tags are tar-substrate and the "
        "v0.6 client has dropped tar support."
    ),
)


# Default release tag for live tests. The top-level test_client.py
# was the canonical source pre-#274 (the nested copy missed the
# v2026.04.2 bump). Override at the shell with MAT_VIS_LIVE_TAG.
LIVE_TAG = os.environ.get("MAT_VIS_LIVE_TAG", "v2026.04.2")
