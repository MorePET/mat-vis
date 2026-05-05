"""VisAsset — re-export module for the ergonomic asset wrapper.

The :class:`VisAsset` class is defined in :mod:`mat_vis_client.client` so the
single-file standalone client can mirror it without splitting across modules
(see ``tests/test_standalone_drift.py``). This module exists as the public
import path documented in the spec for mat-vis#93 — keeps user-facing
imports tidy while letting the standalone parity test pass.
"""

from __future__ import annotations

from mat_vis_client.client import VisAsset

__all__ = ["VisAsset"]
