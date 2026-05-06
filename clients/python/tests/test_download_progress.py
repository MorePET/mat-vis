"""Download-progress logging on cache miss (#287).

bernhard-42 reports that ``show()`` over several uncached materials
pauses for many seconds with no feedback (each baked-material fetch
~0.8-1s). Library users (build123d, Jupyter, etc.) need a hook to know
the client is actually doing work, not hung.

Contract: ``fetch_texture`` emits ``log.info("Downloading ...")`` at the
network boundary — once per real GET — and stays silent on cache hits.
No tqdm dependency; pure stdlib logging so consumers wire up their own
handlers / progress UIs.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mat_vis_client import MatVisClient


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

MOCK_INDEX = [
    {
        "id": "Rock064",
        "source": "ambientcg",
        "mat_vis": {"name": "Rock064", "category": "stone"},
        "available_tiers": ["1k"],
        "maps": ["color"],
    }
]

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"data" + b"\xaeB`\x82"


@pytest.fixture
def tmp_cache():
    tmp = Path(tempfile.mkdtemp(prefix="mat-vis-test-progress-"))
    yield tmp
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def _prime(client: MatVisClient) -> None:
    client._manifest = MOCK_MANIFEST
    client._indexes = {"ambientcg": MOCK_INDEX}
    client._tier_complete[("ambientcg", "1k")] = True


def test_fetch_texture_logs_download_on_cache_miss(tmp_cache, caplog):
    """A network fetch must emit log.info('Downloading ...') with material id."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    _prime(c)

    def fake_get(url, headers=None, return_final_url=False):
        return (TINY_PNG, url) if return_final_url else TINY_PNG

    with caplog.at_level(logging.INFO, logger="mat-vis-client"):
        with patch("mat_vis_client.client._get", side_effect=fake_get):
            c.fetch_texture("ambientcg", "Rock064", "color", tier="1k")

    download_msgs = [r.getMessage() for r in caplog.records if "Downloading" in r.getMessage()]
    assert download_msgs, (
        f"expected a 'Downloading ...' info log on cache miss, "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )
    msg = download_msgs[0]
    assert "Rock064" in msg, f"download log must include material id, got: {msg!r}"
    assert "color" in msg, f"download log must include channel, got: {msg!r}"
    assert "1k" in msg, f"download log must include tier, got: {msg!r}"


def test_fetch_texture_silent_on_cache_hit(tmp_cache, caplog):
    """A cache hit must NOT emit a 'Downloading ...' log."""
    c = MatVisClient(cache_dir=tmp_cache, tag="v2026.04.0")
    _prime(c)

    # Pre-seed cache so fetch_texture short-circuits before _get.
    cache_path = c._cache_scope / "ambientcg" / "1k" / "Rock064" / "color.png"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(TINY_PNG)

    with caplog.at_level(logging.INFO, logger="mat-vis-client"):
        # _get must never be called on a cache hit; if the implementation
        # regresses and hits the network, the patch makes it loud.
        with patch(
            "mat_vis_client.client._get",
            side_effect=AssertionError("cache hit must not touch network"),
        ):
            data = c.fetch_texture("ambientcg", "Rock064", "color", tier="1k")

    assert data == TINY_PNG
    download_msgs = [r.getMessage() for r in caplog.records if "Downloading" in r.getMessage()]
    assert not download_msgs, f"cache hit must be silent, got 'Downloading' logs: {download_msgs}"
