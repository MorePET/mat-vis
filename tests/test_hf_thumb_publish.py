"""Tests for ``mat_vis_baker.hf_thumb_publish`` (#402).

Pure-Python — every HfApi / HTTP call is mocked. No live network.

Covers the load-bearing properties of the thumb publish pipeline:

1. ``_guard_prod_target`` refuses prod repos without ``allow_prod``.
2. Pre-flight HEAD probes skip already-published thumbs.
3. ``_extend_maps_for_thumb`` adds 'thumb' to ``maps`` idempotently.
4. The ``.tier_complete`` sentinel is the LAST commit in the run.
5. ``dry_run=True`` makes no ``create_commit`` calls.
6. Materials with bad PNG bytes are skipped without crashing the run.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from mat_vis_baker.hf_thumb_publish import (
    _extend_maps_for_thumb,
    _list_local_thumbs,
    publish_thumb_tier,
)


def _make_png(path: Path, color: str = "red") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (32, 32), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()
    path.write_bytes(raw)
    return raw


def test_extend_maps_for_thumb_adds_thumb_idempotently():
    catalog = [
        {"id": "A", "maps": ["color", "normal"]},
        {"id": "B", "maps": ["color", "thumb"]},  # already has it
        {"id": "C"},  # no maps key
        {"id": "D", "maps": ["color"]},  # not in derived
    ]
    _extend_maps_for_thumb(catalog, derived_ids={"A", "B", "C"})
    assert catalog[0]["maps"] == ["color", "normal", "thumb"]
    assert catalog[1]["maps"] == ["color", "thumb"]  # unchanged
    assert catalog[2]["maps"] == ["thumb"]
    assert catalog[3]["maps"] == ["color"]  # untouched


def test_list_local_thumbs_sorted(tmp_path: Path):
    src = tmp_path / "ambientcg"
    _make_png(src / "Wood001" / "thumb.png")
    _make_png(src / "Metal007" / "thumb.png")
    _make_png(src / "Foo" / "other.png")  # ignored — wrong filename
    (src / "NotADir").touch()  # ignored — not a dir
    out = _list_local_thumbs(tmp_path, "ambientcg")
    assert [mid for mid, _ in out] == ["Metal007", "Wood001"]


def test_list_local_thumbs_empty_when_no_source_dir(tmp_path: Path):
    assert _list_local_thumbs(tmp_path, "missing") == []


def test_publish_dry_run_makes_no_create_commit_calls(tmp_path: Path):
    _make_png(tmp_path / "ambientcg" / "Mat1" / "thumb.png")
    fake_api = MagicMock()
    fake_api.create_commit = MagicMock()
    with (
        patch("mat_vis_baker.hf_thumb_publish.HfApi", return_value=fake_api),
        patch("mat_vis_baker.hf_thumb_publish._http_head_ok", return_value=False),
        patch(
            "mat_vis_baker.hf_thumb_publish._fetch_catalog",
            return_value=[{"id": "Mat1", "maps": ["color"]}],
        ),
    ):
        result = publish_thumb_tier(
            source="ambientcg",
            release_tag="v0.0.0-tst",
            thumbs_dir=tmp_path,
            repo_id="gerchowl/mat-vis-tst",
            dry_run=True,
        )
    assert fake_api.create_commit.call_count == 0
    assert result["ok"] == 1
    assert result["failed"] == 0


def test_publish_skips_already_present_via_head_probe(tmp_path: Path):
    _make_png(tmp_path / "ambientcg" / "Mat1" / "thumb.png")
    _make_png(tmp_path / "ambientcg" / "Mat2" / "thumb.png")
    fake_api = MagicMock()
    # Mat1 already on HF, Mat2 missing.

    def head_probe(url, *, token=None):
        return "Mat1" in url

    with (
        patch("mat_vis_baker.hf_thumb_publish.HfApi", return_value=fake_api),
        patch("mat_vis_baker.hf_thumb_publish._http_head_ok", side_effect=head_probe),
        patch("mat_vis_baker.hf_thumb_publish._fetch_catalog", return_value=[]),
        patch("mat_vis_baker.hf_thumb_publish._create_commit_with_backoff") as mock_commit,
    ):
        mock_commit.return_value.oid = "abc123"
        result = publish_thumb_tier(
            source="ambientcg",
            release_tag="v0.0.0-tst",
            thumbs_dir=tmp_path,
            repo_id="gerchowl/mat-vis-tst",
        )
    assert result["ok"] == 1  # Mat2
    assert result["skipped_preflight"] == 1  # Mat1
    # Two commits total: one batch (Mat2) + sentinel. No catalog
    # commit because _fetch_catalog returned [].
    assert mock_commit.call_count == 2


def test_publish_sentinel_is_last_commit(tmp_path: Path):
    _make_png(tmp_path / "ambientcg" / "Mat1" / "thumb.png")
    fake_api = MagicMock()
    fake_api.repo_info.return_value = type("X", (), {"sha": "parent"})()
    with (
        patch("mat_vis_baker.hf_thumb_publish.HfApi", return_value=fake_api),
        patch("mat_vis_baker.hf_thumb_publish._http_head_ok", return_value=False),
        patch(
            "mat_vis_baker.hf_thumb_publish._fetch_catalog",
            return_value=[{"id": "Mat1", "maps": ["color"]}],
        ),
        patch(
            "mat_vis_baker.hf_thumb_publish._fetch_manifest_with_parent",
            return_value=({}, "parent"),
        ),
        patch("mat_vis_baker.hf_thumb_publish._create_commit_with_backoff") as mock_commit,
    ):
        mock_commit.return_value.oid = "x"
        publish_thumb_tier(
            source="ambientcg",
            release_tag="v0.0.0-tst",
            thumbs_dir=tmp_path,
            repo_id="gerchowl/mat-vis-tst",
        )
    # 3 commits: per-file batch, catalog+manifest, sentinel.
    assert mock_commit.call_count == 3
    last_call = mock_commit.call_args_list[-1]
    ops = last_call.kwargs["operations"]
    assert len(ops) == 1
    assert ops[0].path_in_repo == "ambientcg/thumb/.tier_complete"


def test_publish_skips_status_failed_materials(tmp_path: Path):
    """#428: materials marked status=failed in the catalog must not get a
    published thumb — the renderer ships a default white sphere for them."""
    for mid in ("Good_A", "Bad_B", "Good_C"):
        _make_png(tmp_path / "gpuopen" / mid / "thumb.png")
    catalog = [
        {"id": "Good_A", "status": "ok", "maps": ["color"]},
        {"id": "Bad_B", "status": "failed"},
        {"id": "Good_C", "status": "ok", "maps": ["color"]},
    ]
    fake_api = MagicMock()
    with (
        patch("mat_vis_baker.hf_thumb_publish.HfApi", return_value=fake_api),
        patch("mat_vis_baker.hf_thumb_publish._http_head_ok", return_value=False),
        patch("mat_vis_baker.hf_thumb_publish._fetch_catalog", return_value=catalog),
        patch(
            "mat_vis_baker.hf_thumb_publish._fetch_manifest_with_parent",
            return_value=({}, "parent"),
        ),
        patch("mat_vis_baker.hf_thumb_publish._create_commit_with_backoff") as mock_commit,
    ):
        mock_commit.return_value.oid = "x"
        result = publish_thumb_tier(
            source="gpuopen",
            release_tag="v0.0.0-tst",
            thumbs_dir=tmp_path,
            repo_id="gerchowl/mat-vis-tst",
        )
    committed = [
        op.path_in_repo
        for call in mock_commit.call_args_list
        for op in call.kwargs.get("operations", [])
    ]
    assert any(p == "gpuopen/thumb/Good_A/thumb.png" for p in committed)
    assert any(p == "gpuopen/thumb/Good_C/thumb.png" for p in committed)
    assert not any("Bad_B" in p for p in committed), "status=failed thumb must not be published"
    assert result["ok"] == 2
    assert result["skipped_failed"] == 1


def test_publish_skips_bad_png_bytes(tmp_path: Path):
    # Write a non-PNG file as thumb.png — magic-byte verify must reject.
    bad = tmp_path / "ambientcg" / "Bad" / "thumb.png"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"this is not a PNG")
    _make_png(tmp_path / "ambientcg" / "Good" / "thumb.png")
    fake_api = MagicMock()
    with (
        patch("mat_vis_baker.hf_thumb_publish.HfApi", return_value=fake_api),
        patch("mat_vis_baker.hf_thumb_publish._http_head_ok", return_value=False),
        patch("mat_vis_baker.hf_thumb_publish._fetch_catalog", return_value=[]),
        patch("mat_vis_baker.hf_thumb_publish._create_commit_with_backoff") as mock_commit,
    ):
        mock_commit.return_value.oid = "y"
        result = publish_thumb_tier(
            source="ambientcg",
            release_tag="v0.0.0-tst",
            thumbs_dir=tmp_path,
            repo_id="gerchowl/mat-vis-tst",
        )
    assert result["ok"] == 1  # Good
    assert result["failed"] == 1  # Bad


def test_publish_guards_prod_repo_without_allow_prod(tmp_path: Path):
    _make_png(tmp_path / "ambientcg" / "Mat1" / "thumb.png")
    with pytest.raises(ValueError, match="allow.prod"):
        publish_thumb_tier(
            source="ambientcg",
            release_tag="v0.0.0",
            thumbs_dir=tmp_path,
            repo_id="gerchowl/mat-vis",  # prod, no allow_prod
        )


def test_publish_returns_error_when_no_local_thumbs(tmp_path: Path):
    result = publish_thumb_tier(
        source="ambientcg",
        release_tag="v0.0.0-tst",
        thumbs_dir=tmp_path,
        repo_id="gerchowl/mat-vis-tst",
    )
    assert "error" in result
    assert result["ok"] == 0
