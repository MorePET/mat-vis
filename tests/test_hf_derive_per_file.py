"""Tests for ``mat_vis_baker.hf_derive_per_file`` (#204 / ADR-0012).

Pure-Python — every HfApi / HTTP call is mocked. No live network.

Covers the load-bearing properties of the per-file derive pipeline:

1. ``_guard_prod_target`` refuses prod repos without ``--allow-prod``.
2. The catalog's ``available_tiers`` is updated for every derived id.
3. Pre-flight HEAD probes on the target tier skip already-derived
   materials (no source GET, no resize work).
4. The ``.tier_complete`` sentinel is the *last* commit in the run.
5. ``dry_run=True`` makes no ``create_commit`` calls.
6. Magic-byte verification rejects non-PNG source bytes and
   non-KTX2 transformed bytes.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from mat_vis_baker.hf_derive_per_file import (
    _extend_available_tiers,
    _resize_png,
    derive_ktx2_tier,
    derive_smaller_tier,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
KTX2_MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"


def _make_png(size: int = 32) -> bytes:
    """Real PNG bytes — PIL needs to actually decode them."""
    img = Image.new("RGB", (size, size), color=(120, 80, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _fake_tree_entry(path: str) -> SimpleNamespace:
    return SimpleNamespace(path=path)


# ── _extend_available_tiers ──────────────────────────────────


class TestExtendAvailableTiers:
    def test_appends_target_tier_to_matched_entries(self) -> None:
        catalog = [
            {"id": "mat_a", "available_tiers": ["1k"]},
            {"id": "mat_b", "available_tiers": ["1k"]},
            {"id": "mat_c", "available_tiers": ["1k"]},
        ]
        out = _extend_available_tiers(catalog, {"mat_a", "mat_b"}, "512")
        assert out[0]["available_tiers"] == ["1k", "512"]
        assert out[1]["available_tiers"] == ["1k", "512"]
        assert out[2]["available_tiers"] == ["1k"]

    def test_idempotent_when_tier_already_listed(self) -> None:
        catalog = [{"id": "mat_a", "available_tiers": ["1k", "512"]}]
        out = _extend_available_tiers(catalog, {"mat_a"}, "512")
        # No duplicate appended.
        assert out[0]["available_tiers"] == ["1k", "512"]

    def test_handles_missing_available_tiers_field(self) -> None:
        catalog = [{"id": "mat_a"}]  # no available_tiers key
        out = _extend_available_tiers(catalog, {"mat_a"}, "512")
        assert out[0]["available_tiers"] == ["512"]


# ── prod guard ──────────────────────────────────────────────


class TestProdGuard:
    def test_refuses_prod_repo_without_allow_prod(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="allow_prod"):
            derive_smaller_tier(
                source="polyhaven",
                source_tier="4k",
                target_tier="1k",
                release_tag="v2026.05.0",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis",  # prod — refuse
            )

    def test_refuses_prod_repo_for_ktx2_too(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="allow_prod"):
            derive_ktx2_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="ktx2-1k",
                release_tag="v2026.05.0",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis",
            )


# ── upscale guard ───────────────────────────────────────────


class TestUpscaleGuard:
    def test_refuses_upscale(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="upscaling not supported"):
            derive_smaller_tier(
                source="polyhaven",
                source_tier="512",  # 512 px
                target_tier="1k",  # 1024 px — would invent pixels
                release_tag="v0.0.0",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
            )


# ── derive_smaller_tier integration (mocked) ────────────────


def _patch_http_for_derive(*, png_bytes: bytes, target_existing: set[str]):
    """Build patches for the HTTP helpers used by the driver.

    - GETs: catalog fetch returns a stub catalog; everything else
      returns ``png_bytes``.
    - HEADs: True iff URL is in ``target_existing``.
    """

    def _get(url, *, token=None, timeout=120):  # noqa: ARG001
        if url.endswith(".json"):
            return json.dumps(
                [{"id": f"mat_{i}", "available_tiers": ["1k"]} for i in range(3)]
            ).encode("utf-8")
        return png_bytes

    def _head(url, *, token=None, timeout=30):  # noqa: ARG001
        return url in target_existing

    return _get, _head


class TestDeriveSmallerTier:
    def _setup_api(self, mids: list[str], channels: list[str]) -> MagicMock:
        api = MagicMock()
        # Tree of source tier: one entry per (mid, channel).
        tree = []
        for m in mids:
            for ch in channels:
                tree.append(_fake_tree_entry(f"polyhaven/1k/{m}/{ch}.png"))

        def _list(repo_id, repo_type, revision, path_in_repo, recursive=False, **kw):
            return [e for e in tree if e.path.startswith(path_in_repo.rstrip("/") + "/")]

        api.list_repo_tree.side_effect = _list
        commit = SimpleNamespace(oid="deadbeef" * 5, commit_oid="deadbeef" * 5)
        api.create_commit.return_value = commit
        return api

    def test_happy_path_resize_writes_per_file_then_catalog_then_sentinel(self, tmp_path) -> None:
        png = _make_png(64)
        api = self._setup_api(["mat_0", "mat_1"], ["color", "normal"])
        get, head = _patch_http_for_derive(png_bytes=png, target_existing=set())

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", side_effect=head),
        ):
            result = derive_smaller_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="512",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
            )

        assert result["ok"] == 2
        assert result["failed"] == 0

        # Verify commit ordering: textures → catalog → sentinel (last).
        calls = api.create_commit.call_args_list
        # Last commit MUST be the sentinel.
        last_ops = calls[-1].kwargs["operations"]
        assert len(last_ops) == 1
        assert last_ops[0].path_in_repo == "polyhaven/512/.tier_complete"

        # Catalog commit is the second-to-last; it now also bundles the
        # release-manifest update (#207-style atomicity, parent_commit
        # CAS-retry).
        catalog_ops = calls[-2].kwargs["operations"]
        catalog_paths = {op.path_in_repo for op in catalog_ops}
        assert catalog_paths == {"polyhaven.json", "release-manifest.json"}, catalog_paths
        assert "parent_commit" in calls[-2].kwargs

        # And the catalog body has the new tier appended.
        catalog_bytes = next(
            op.path_or_fileobj for op in catalog_ops if op.path_in_repo == "polyhaven.json"
        )
        catalog = json.loads(catalog_bytes)
        for entry in catalog:
            if entry["id"] in {"mat_0", "mat_1"}:
                assert "512" in entry["available_tiers"], entry

        # Manifest body lists the derived tier under the source.
        manifest_bytes = next(
            op.path_or_fileobj for op in catalog_ops if op.path_in_repo == "release-manifest.json"
        )
        manifest = json.loads(manifest_bytes)
        assert manifest["sources"]["polyhaven"]["tiers"]["512"] == {"complete": True}

        # Texture batch commits should land target-tier paths.
        texture_paths = [op.path_in_repo for c in calls[:-2] for op in c.kwargs["operations"]]
        for m in ("mat_0", "mat_1"):
            for ch in ("color", "normal"):
                assert f"polyhaven/512/{m}/{ch}.png" in texture_paths

    def test_preflight_skip_short_circuits_already_derived(self, tmp_path) -> None:
        """A material whose target files all already exist (HEAD ok)
        must NOT be re-resized — no source GET for its channels."""
        png = _make_png(64)
        api = self._setup_api(["mat_0", "mat_1"], ["color"])

        # Pretend mat_0's target file is already on HF.
        existing = {
            "https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve/v0.0.0-test"
            "/polyhaven/512/mat_0/color.png"
        }

        get_calls: list[str] = []

        def _get(url, *, token=None, timeout=120):
            get_calls.append(url)
            if url.endswith(".json"):
                return json.dumps(
                    [{"id": f"mat_{i}", "available_tiers": ["1k"]} for i in range(2)]
                ).encode("utf-8")
            return png

        def _head(url, *, token=None, timeout=30):
            return url in existing

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=_get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", side_effect=_head),
        ):
            result = derive_smaller_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="512",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
            )

        # mat_0 skipped, mat_1 derived.
        assert result["skipped_preflight"] == 1
        assert result["ok"] == 1

        # No source GET on the skipped material's channel.
        skipped_url = (
            "https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve/v0.0.0-test"
            "/polyhaven/1k/mat_0/color.png"
        )
        assert skipped_url not in get_calls, (
            f"preflight should have skipped mat_0; saw GET {skipped_url}"
        )

    def test_dry_run_makes_no_commit_calls(self, tmp_path) -> None:
        png = _make_png(64)
        api = self._setup_api(["mat_0"], ["color"])
        get, head = _patch_http_for_derive(png_bytes=png, target_existing=set())

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", side_effect=head),
        ):
            result = derive_smaller_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="512",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
                dry_run=True,
            )

        assert result["ok"] == 1
        api.create_commit.assert_not_called()

    def test_rejects_non_png_source_bytes(self, tmp_path) -> None:
        """A material whose source GET returns garbage (HTML 404 served
        as 200, etc.) must fail magic-byte verification, not crash PIL
        with an opaque error or commit garbage downstream."""
        api = self._setup_api(["mat_0"], ["color"])

        def _get(url, *, token=None, timeout=120):
            if url.endswith(".json"):
                return b"[]"
            return b"<html>404 not found</html>"  # NOT a PNG

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=_get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", return_value=False),
        ):
            result = derive_smaller_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="512",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
            )

        # No ops produced → material counted as failed.
        assert result["ok"] == 0
        assert result["failed"] == 1


# ── derive_ktx2_tier ────────────────────────────────────────


class TestDeriveKtx2Tier:
    def _setup_api(self) -> MagicMock:
        api = MagicMock()
        tree = [
            _fake_tree_entry("polyhaven/1k/mat_0/color.png"),
            _fake_tree_entry("polyhaven/1k/mat_0/normal.png"),
        ]

        def _list(repo_id, repo_type, revision, path_in_repo, recursive=False, **kw):
            return [e for e in tree if e.path.startswith(path_in_repo.rstrip("/") + "/")]

        api.list_repo_tree.side_effect = _list
        api.create_commit.return_value = SimpleNamespace(oid="cafebabe" * 5)
        return api

    def test_ktx2_transcode_writes_ktx2_files_and_sentinel_last(self, tmp_path) -> None:
        png = _make_png(64)
        api = self._setup_api()

        def _get(url, *, token=None, timeout=120):
            if url.endswith(".json"):
                return b"[]"
            return png

        # Stub out toktx — return KTX2-magic bytes from the transform.
        def _fake_transcode(raw):  # noqa: ARG001
            return KTX2_MAGIC + b"\x00\x01\x02\x03"

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=_get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", return_value=False),
            patch(
                "mat_vis_baker.hf_derive_per_file._ktx2_transcode",
                side_effect=_fake_transcode,
            ),
        ):
            result = derive_ktx2_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="ktx2-1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
            )

        assert result["ok"] == 1
        # Last commit is the sentinel.
        last_ops = api.create_commit.call_args_list[-1].kwargs["operations"]
        assert last_ops[0].path_in_repo == "polyhaven/ktx2-1k/.tier_complete"

        # All texture commits use .ktx2 extension.
        all_paths = [
            op.path_in_repo
            for c in api.create_commit.call_args_list
            for op in c.kwargs["operations"]
        ]
        ktx_paths = [p for p in all_paths if p.startswith("polyhaven/ktx2-1k/mat_0/")]
        assert {"polyhaven/ktx2-1k/mat_0/color.ktx2", "polyhaven/ktx2-1k/mat_0/normal.ktx2"} == set(
            ktx_paths
        )

    def test_rejects_transform_output_lacking_ktx2_magic(self, tmp_path) -> None:
        """A toktx that silently returns a PNG (or empty bytes) must
        not get committed under a .ktx2 path."""
        png = _make_png(64)
        api = self._setup_api()

        def _get(url, *, token=None, timeout=120):
            if url.endswith(".json"):
                return b"[]"
            return png

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=_get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", return_value=False),
            patch(
                "mat_vis_baker.hf_derive_per_file._ktx2_transcode",
                # Return PNG-magic bytes (definitely not KTX2). Magic-byte
                # gate must reject this before commit.
                side_effect=lambda raw: PNG_MAGIC + b"\x00",
            ),
        ):
            result = derive_ktx2_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="ktx2-1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
            )

        assert result["ok"] == 0
        assert result["failed"] == 1


# ── transform unit ──────────────────────────────────────────


class TestResizeTransform:
    def test_lanczos_resize_produces_valid_png_at_target_size(self) -> None:
        """The resize transform must produce a PNG of the requested
        size — guards against a refactor that swaps LANCZOS for a
        broken filter or forgets the .save(format='PNG') call."""
        src = _make_png(256)
        out = _resize_png(src, 64)
        assert out.startswith(PNG_MAGIC)
        img = Image.open(io.BytesIO(out))
        assert img.size == (64, 64)
