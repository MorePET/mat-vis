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


# ── #207 part 2 / #210 part A: derive must emit + CAS-retry the manifest ──


def _make_412_error(msg: str = "412 Precondition Failed: revision moved") -> Exception:
    """Mirror of bake-side helper. Substring detection in the production
    code matches across hub error-class shifts."""
    return RuntimeError(msg)


class TestDeriveManifestEmission:
    """The derive path's catalog+manifest commit was added in #209.
    Mocked tests asserted the parent_commit kwarg is set, but never
    verified the manifest path is in the commit ops set or that the
    manifest body actually carries the new tier. Locking it down so a
    refactor that drops the bundled commit can't reach prod."""

    def test_commit_path_set_includes_manifest(self, tmp_path) -> None:
        api = TestDeriveSmallerTier()._setup_api(["mat_0", "mat_1"], ["color"])
        get, head = _patch_http_for_derive(png_bytes=_make_png(64), target_existing=set())

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", side_effect=head),
        ):
            derive_smaller_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="512",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
            )

        all_paths = {
            op.path_in_repo
            for c in api.create_commit.call_args_list
            for op in c.kwargs["operations"]
        }
        # Substrate-contract paths the derive run MUST emit.
        assert "release-manifest.json" in all_paths
        assert "polyhaven.json" in all_paths
        assert "polyhaven/512/.tier_complete" in all_paths

    def test_manifest_body_lists_new_tier_under_source(self, tmp_path) -> None:
        """The body of the manifest commit must carry
        sources.<src>.tiers.<target_tier> = {complete: true} — clients
        depend on this to discover derived tiers without re-baking."""
        api = TestDeriveSmallerTier()._setup_api(["mat_0"], ["color"])
        get, head = _patch_http_for_derive(png_bytes=_make_png(64), target_existing=set())

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", side_effect=head),
        ):
            derive_smaller_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="512",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
            )

        manifest_op = next(
            op
            for c in api.create_commit.call_args_list
            for op in c.kwargs["operations"]
            if op.path_in_repo == "release-manifest.json"
        )
        manifest = json.loads(manifest_op.path_or_fileobj)
        assert manifest["schema_version"] == 3
        assert manifest["sources"]["polyhaven"]["tiers"]["512"] == {"complete": True}


class TestDeriveCasRetryOnManifestCommit:
    """Mirror of bake-side TestCasRetryOnManifestCommit — proves the
    derive path's 412-retry loop actually retries, exhausts at the
    documented budget, and does NOT retry non-412 errors."""

    def _drive_derive_with_commit_side_effect(
        self, tmp_path, side_effect, *, expect_raises: bool = False
    ):
        """Returns (api, fetch_mfst, exc) — same shape as the bake helper."""
        api = TestDeriveSmallerTier()._setup_api(["mat_0"], ["color"])
        get, head = _patch_http_for_derive(png_bytes=_make_png(64), target_existing=set())

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", side_effect=head),
            patch(
                "mat_vis_baker.hf_bake_per_file._fetch_manifest_with_parent",
                return_value=({}, "deadbeef"),
            ) as fetch_mfst,
        ):
            api.create_commit.side_effect = side_effect
            api.create_commit.return_value = SimpleNamespace(oid="cafef00d")
            exc: Exception | None = None
            try:
                derive_smaller_tier(
                    source="polyhaven",
                    source_tier="1k",
                    target_tier="512",
                    release_tag="v0.0.0-test",
                    work_dir=tmp_path,
                    repo_id="gerchowl/mat-vis-tst",
                )
            except Exception as e:  # noqa: BLE001
                exc = e

            if expect_raises:
                assert exc is not None, "expected an exception to propagate"
            else:
                assert exc is None, f"unexpected exception: {exc!r}"
            return api, fetch_mfst, exc

    def test_412_retry_succeeds_on_second_attempt(self, tmp_path) -> None:
        """One 412 → retry → success. Asserts the loop fetched the
        manifest twice (initial + retry) and made two manifest-commit
        attempts."""
        attempts = {"n": 0}

        def side_effect(*args, **kwargs):
            ops = kwargs.get("operations") or []
            paths = {op.path_in_repo for op in ops}
            if "release-manifest.json" in paths:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise _make_412_error()
            return SimpleNamespace(oid="cafef00d")

        _api, fetch_mfst, _exc = self._drive_derive_with_commit_side_effect(
            tmp_path, side_effect=side_effect, expect_raises=False
        )

        assert attempts["n"] == 2, f"expected 2 manifest attempts, got {attempts['n']}"
        assert fetch_mfst.call_count == 2, fetch_mfst.call_count

    def test_412_retry_exhausts_after_max_retries(self, tmp_path) -> None:
        """Continuous 412 → 6 attempts then propagate."""

        def side_effect(*args, **kwargs):
            ops = kwargs.get("operations") or []
            paths = {op.path_in_repo for op in ops}
            if "release-manifest.json" in paths:
                raise _make_412_error()
            return SimpleNamespace(oid="cafef00d")

        _api, fetch_mfst, exc = self._drive_derive_with_commit_side_effect(
            tmp_path, side_effect=side_effect, expect_raises=True
        )

        assert fetch_mfst.call_count == 6, f"expected 6 retry attempts, got {fetch_mfst.call_count}"
        assert exc is not None and "412" in str(exc), exc

    def test_non_412_error_is_not_retried(self, tmp_path) -> None:
        """A 401 must propagate immediately — masking auth failures
        behind retry exhaustion would create false 'flaky CI' signals."""
        attempts = {"n": 0}

        def side_effect(*args, **kwargs):
            ops = kwargs.get("operations") or []
            paths = {op.path_in_repo for op in ops}
            if "release-manifest.json" in paths:
                attempts["n"] += 1
                raise RuntimeError("401 Unauthorized: token rejected")
            return SimpleNamespace(oid="cafef00d")

        _api, fetch_mfst, exc = self._drive_derive_with_commit_side_effect(
            tmp_path, side_effect=side_effect, expect_raises=True
        )

        assert attempts["n"] == 1, f"401 must not retry (got {attempts['n']} attempts)"
        assert fetch_mfst.call_count == 1, fetch_mfst.call_count
        assert exc is not None and "401" in str(exc), exc


# ── #228: bytes-aware batching (derive path) ─────────────────────────


def _texture_only_commits(api):
    """Strip the catalog+manifest commit and the sentinel commit from the
    derive call list — what's left is the texture-batch flushes."""
    return [
        c
        for c in api.create_commit.call_args_list
        if not any(
            op.path_in_repo
            in {
                "release-manifest.json",
                "polyhaven.json",
                "polyhaven/512/.tier_complete",
            }
            for op in c.kwargs["operations"]
        )
    ]


class TestDeriveBytesAwareBatching:
    """#228: derive driver flushes on first-of-N-or-bytes. Same shape
    of test as the bake-side coverage. The transform inflates payload
    bytes (resize → smaller PNG) so the test stubs the transform with a
    deterministic fixed-size payload to make the math predictable."""

    def _setup_api_with_n_materials(self, n: int) -> MagicMock:
        api = MagicMock()
        tree = [_fake_tree_entry(f"polyhaven/1k/mat_{i}/color.png") for i in range(n)]

        def _list(repo_id, repo_type, revision, path_in_repo, recursive=False, **kw):
            return [e for e in tree if e.path.startswith(path_in_repo.rstrip("/") + "/")]

        api.list_repo_tree.side_effect = _list
        api.create_commit.return_value = SimpleNamespace(oid="deadbeef" * 5)
        return api

    def test_count_ceiling_drives_flush_when_bytes_below_max(self, tmp_path):
        """Many tiny derived payloads → count drives the flush.
        batch_size=2 across 5 materials ⇒ 3 texture-batch commits."""
        api = self._setup_api_with_n_materials(5)
        small_png = _make_png(8)  # ~70-90 bytes

        def _get(url, *, token=None, timeout=120):
            if url.endswith(".json"):
                return json.dumps(
                    [{"id": f"mat_{i}", "available_tiers": ["1k"]} for i in range(5)]
                ).encode("utf-8")
            return small_png

        # Stub transform to return a fixed-size small payload.
        small_out = PNG_MAGIC + b"\x00" * 200

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=_get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", return_value=False),
            patch(
                "mat_vis_baker.hf_derive_per_file._resize_png",
                side_effect=lambda raw, target_px: small_out,
            ),
        ):
            result = derive_smaller_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="512",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
                batch_size=2,
                batch_max_bytes=10 * 1024 * 1024,
            )

        assert result["ok"] == 5
        texture_calls = _texture_only_commits(api)
        assert len(texture_calls) == 3, (
            f"expected 3 texture commits (count-driven 2+2+1); got {len(texture_calls)}"
        )

    def test_bytes_ceiling_drives_flush_when_count_below_max(self, tmp_path):
        """Few large derived payloads → bytes drives the flush.
        Stub transform returns ~5 MiB per channel; with a 6 MiB ceiling
        each material lands as its own commit despite batch_size=300."""
        api = self._setup_api_with_n_materials(3)
        small_in = _make_png(8)
        big_out = PNG_MAGIC + b"\x00" * (5 * 1024 * 1024)

        def _get(url, *, token=None, timeout=120):
            if url.endswith(".json"):
                return json.dumps(
                    [{"id": f"mat_{i}", "available_tiers": ["1k"]} for i in range(3)]
                ).encode("utf-8")
            return small_in

        with (
            patch("mat_vis_baker.hf_derive_per_file.HfApi", return_value=api),
            patch("mat_vis_baker.hf_derive_per_file._http_get", side_effect=_get),
            patch("mat_vis_baker.hf_derive_per_file._http_head_ok", return_value=False),
            patch(
                "mat_vis_baker.hf_derive_per_file._resize_png",
                side_effect=lambda raw, target_px: big_out,
            ),
        ):
            result = derive_smaller_tier(
                source="polyhaven",
                source_tier="1k",
                target_tier="512",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                repo_id="gerchowl/mat-vis-tst",
                batch_size=300,  # high — count must NOT bind
                batch_max_bytes=4 * 1024 * 1024,  # below one material's payload
            )

        assert result["ok"] == 3
        texture_calls = _texture_only_commits(api)
        assert len(texture_calls) == 3, (
            f"expected 3 texture commits (bytes-driven, one each); got {len(texture_calls)}"
        )

    def test_preflight_skip_still_works_under_bytes_aware_batching(self, tmp_path):
        """Preflight skip primitive intact: 5 materials, mat_0 + mat_1
        already on HF, batch_size=2 → only 3 derived → 2 texture commits."""
        api = self._setup_api_with_n_materials(5)
        png = _make_png(16)

        # Mark mat_0 + mat_1 as already present at target tier.
        existing_target_urls = {
            f"https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve/v0.0.0-test"
            f"/polyhaven/512/mat_{i}/color.png"
            for i in range(2)
        }

        def _get(url, *, token=None, timeout=120):
            if url.endswith(".json"):
                return json.dumps(
                    [{"id": f"mat_{i}", "available_tiers": ["1k"]} for i in range(5)]
                ).encode("utf-8")
            return png

        def _head(url, *, token=None, timeout=30):
            return url in existing_target_urls

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
                batch_size=2,
                batch_max_bytes=10 * 1024 * 1024,
            )

        assert result["ok"] == 3
        assert result["skipped_preflight"] == 2

        texture_calls = _texture_only_commits(api)
        # 3 unskipped → batch_size=2 → 2+1 → 2 texture commits.
        assert len(texture_calls) == 2, (
            f"expected 2 texture commits across 3 unskipped materials; got {len(texture_calls)}"
        )
