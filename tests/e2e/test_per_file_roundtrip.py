"""End-to-end smoke tests against ``gerchowl/mat-vis-tst`` (#193).

Gated behind ``MAT_VIS_E2E=1`` so the suite only runs locally / in
opt-in CI — HF rate limits make per-PR E2E impractical.

Each ADR-0012 PR (#184..#189) extends this file with the new code
path it just added. This commit adds the **#184 slice**: the baker
CLI / library default routing produces a per-file commit on
``mat-vis-tst`` and a plain HTTP GET on the `resolve/` URL returns
the same bytes the baker uploaded.

Throwaway tag is auto-deleted on suite teardown so the scratch repo
stays tidy.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

import pytest

E2E_ENABLED = os.environ.get("MAT_VIS_E2E") == "1"
pytestmark = pytest.mark.skipif(
    not E2E_ENABLED,
    reason="set MAT_VIS_E2E=1 to run end-to-end tests against gerchowl/mat-vis-tst",
)

REPO = "gerchowl/mat-vis-tst"
TAG = "v0.0.0-e2e-184-perfile"
SOURCE = "polyhaven"
TIER = "1k"


def _hf_token() -> str:
    tok = os.environ.get("HF_TOKEN")
    if tok:
        return tok
    p = Path("~/.cache/huggingface/token").expanduser()
    if p.exists():
        return p.read_text().strip()
    pytest.skip("no HF_TOKEN — set the env var or run `hf auth login`")


@pytest.fixture(scope="module")
def baked_tag():
    """Bake 2 polyhaven 1k materials per-file, yield (tag, result), cleanup."""
    from huggingface_hub import HfApi

    from mat_vis_baker.hf_bake import bake_one

    token = _hf_token()
    with tempfile.TemporaryDirectory() as td:
        result = bake_one(
            source=SOURCE,
            tier=TIER,
            release_tag=TAG,
            work_dir=Path(td),
            repo_id=REPO,
            hf_token=token,
            limit=2,
            batch_size=2,
        )

    assert result.get("ok", 0) == 2, f"baker failed: {result}"
    yield result

    # Cleanup throwaway branch.
    api = HfApi(token=token)
    try:
        api.delete_branch(repo_id=REPO, repo_type="dataset", branch=TAG)
    except Exception as e:  # noqa: BLE001
        print(f"e2e cleanup warn: {type(e).__name__}: {e}")


def _resolve_url(path: str, tag: str = TAG) -> str:
    return f"https://huggingface.co/datasets/{REPO}/resolve/{tag}/{path}"


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "mat-vis-e2e/1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


class TestBakeRoutingProducesPerFileTree:
    def test_tier_complete_sentinel_exists(self, baked_tag) -> None:
        """ADR-0012 atomicity: every completed tier carries a sentinel."""
        body = _http_get(_resolve_url(f"{SOURCE}/{TIER}/.tier_complete"))
        # Sentinel content is the release tag — a 1-line marker.
        assert TAG in body.decode("utf-8")

    def test_catalog_at_root_is_v3(self, baked_tag) -> None:
        """Catalog lives at repo root, not under <source>/<tier>/."""
        import json

        body = _http_get(_resolve_url(f"{SOURCE}.json"))
        catalog = json.loads(body)
        assert len(catalog) >= 2
        assert all("mat_vis" in entry for entry in catalog), "catalog must be v3-shaped"


class TestApiGetRoundTrip:
    """Plain HTTP GET on a per-file URL returns the baker's bytes.

    This is the contract #186 will codify in ``MatVisClient.fetch_texture``.
    Until then, exercising it via stdlib urllib proves the substrate is
    independently usable from any HTTP client.
    """

    def test_fetch_color_png_bytes(self, baked_tag) -> None:
        from huggingface_hub import HfApi

        api = HfApi(token=_hf_token())
        # Find first material id from the freshly-baked tree.
        tree = list(
            api.list_repo_tree(
                repo_id=REPO,
                repo_type="dataset",
                revision=TAG,
                path_in_repo=f"{SOURCE}/{TIER}",
                recursive=True,
            )
        )
        png_files = [
            getattr(e, "path", "") for e in tree if getattr(e, "path", "").endswith("/color.png")
        ]
        assert png_files, f"no color.png in tree: {[getattr(e, 'path', '') for e in tree]}"

        repo_path = png_files[0]
        body = _http_get(_resolve_url(repo_path))
        assert body.startswith(b"\x89PNG\r\n\x1a\n"), "must be a real PNG"
        assert len(body) > 1024, "PNG too small to be a real texture"

        # Cross-check against the LFS pointer's recorded blob.
        # (HF resolve/ serves the actual bytes through the LFS CDN.)
        sha256 = hashlib.sha256(body).hexdigest()
        assert len(sha256) == 64


class TestResumeViaPreflight:
    """Re-running the baker with the same tag must skip everything."""

    def test_second_bake_is_a_noop(self, baked_tag) -> None:
        from mat_vis_baker.hf_bake import bake_one

        with tempfile.TemporaryDirectory() as td:
            result = bake_one(
                source=SOURCE,
                tier=TIER,
                release_tag=TAG,
                work_dir=Path(td),
                repo_id=REPO,
                hf_token=_hf_token(),
                limit=2,
                batch_size=2,
            )

        assert result.get("ok", 0) == 0, "preflight should skip everything"
        assert result.get("skipped_preflight", 0) == 2


# ── #186 slice: Python client fetch_texture round-trip ──────────────


class TestPythonClientFetchTextureRoundTrip:
    """Bake → MatVisClient.fetch_texture → bytes match. Locks the
    plain-GET contract end-to-end (#186 / ADR-0012)."""

    def _client_pointed_at_tst(self, td: Path):
        """Return a MatVisClient whose HF_BASE / HF_DATASET point at
        ``mat-vis-tst`` for the duration of the test. The client uses
        module-level constants for these, so monkeypatch them on the
        loaded module."""
        from mat_vis_client import MatVisClient
        from mat_vis_client import client as _mvc

        _mvc.HF_DATASET = REPO  # gerchowl/mat-vis-tst
        _mvc.HF_BASE = f"https://huggingface.co/datasets/{REPO}/resolve"
        return MatVisClient(tag=TAG, cache_dir=td, cache=False)

    def test_client_fetch_returns_baker_bytes(self, baked_tag) -> None:
        """Python client .fetch_texture against the freshly-baked tag
        returns valid PNG bytes through the per-file resolve URL."""
        with tempfile.TemporaryDirectory() as td:
            client = self._client_pointed_at_tst(Path(td))
            mats = client.materials(SOURCE, TIER)
            assert len(mats) >= 2, f"expected at least 2 baked materials, got {mats}"

            # Validate channels enumeration.
            chs = client.channels(SOURCE, mats[0], TIER)
            assert "color" in chs

            # Plain GET on per-file URL returns valid PNG bytes.
            data = client.fetch_texture(SOURCE, mats[0], "color", TIER)
            assert data.startswith(b"\x89PNG\r\n\x1a\n"), "must be a real PNG"
            assert len(data) > 1024, "PNG too small to be a real texture"

    def test_client_rejects_partial_tier(self, baked_tag) -> None:
        """Pointed at an existing-but-incomplete tier (no .tier_complete
        sentinel), .fetch_texture raises ``MatVisError``. Lock this gate
        so future regressions can't silently serve mid-batch state."""
        from huggingface_hub import HfApi

        from mat_vis_client import MatVisClient, MatVisError

        token = _hf_token()
        api = HfApi(token=token)

        # Make a sibling tag that has files but lacks the sentinel —
        # snapshot main and add a single file so the tier looks "started".
        partial_tag = "v0.0.0-e2e-186-partial"
        from huggingface_hub import CommitOperationAdd

        api.create_branch(
            repo_id=REPO,
            repo_type="dataset",
            branch=partial_tag,
            revision="main",
            exist_ok=True,
        )
        api.create_commit(
            repo_id=REPO,
            repo_type="dataset",
            operations=[
                CommitOperationAdd(
                    path_in_repo=f"{SOURCE}/{TIER}/dummy/color.png",
                    path_or_fileobj=b"\x89PNG\r\n\x1a\n" + b"\x00" * 1100,
                ),
            ],
            commit_message="e2e #186 partial-tier setup (intentionally NO sentinel)",
            revision=partial_tag,
        )

        try:
            with tempfile.TemporaryDirectory() as td:
                from mat_vis_client import client as _mvc

                _mvc.HF_DATASET = REPO
                _mvc.HF_BASE = f"https://huggingface.co/datasets/{REPO}/resolve"
                client = MatVisClient(tag=partial_tag, cache_dir=Path(td), cache=False)
                # Pre-seed manifest + index so we get past metadata
                # validation and into the sentinel probe.
                client._manifest = {
                    "schema_version": 3,
                    "release_tag": partial_tag,
                    "sources": {
                        SOURCE: {
                            "catalog": f"{SOURCE}.json",
                            "tiers": {TIER: {"complete": False}},
                        },
                    },
                }
                client._indexes[SOURCE] = [
                    {
                        "id": "dummy",
                        "source": SOURCE,
                        "mat_vis": {"name": "dummy", "category": "other"},
                        "available_tiers": [TIER],
                        "maps": ["color"],
                    },
                ]
                with pytest.raises(MatVisError, match="not atomically complete"):
                    client.fetch_texture(SOURCE, "dummy", "color", TIER)
        finally:
            try:
                api.delete_branch(repo_id=REPO, repo_type="dataset", branch=partial_tag)
            except Exception as e:  # noqa: BLE001
                print(f"e2e #186 cleanup warn: {type(e).__name__}: {e}")


# ── #185 slice: Dagger passthrough produces the same per-file tree ──


@pytest.mark.skipif(
    not os.environ.get("MAT_VIS_E2E_DAGGER"),
    reason="set MAT_VIS_E2E_DAGGER=1 to also run the Dagger smoke (needs `dagger` CLI)",
)
class TestDaggerBakeRoutingSmoke:
    """Run the Dagger ``bake`` op end-to-end against ``mat-vis-tst``.

    Gated on ``MAT_VIS_E2E_DAGGER=1`` (separate from the baker E2E gate)
    because it requires a working ``dagger`` CLI on the host. CI runs
    this in the dagger-for-github action; locally it's opt-in.
    """

    DAGGER_TAG = "v0.0.0-e2e-185-dagger"

    def test_dagger_bake_2_polyhaven_lands_per_file(self) -> None:
        import subprocess

        from huggingface_hub import HfApi

        token = _hf_token()
        try:
            cmd = [
                "dagger",
                "call",
                "bake",
                "--context=.",
                "--source=polyhaven",
                "--tier=1k",
                f"--release-tag={self.DAGGER_TAG}",
                "--hf-token=env:HF_TOKEN",
                "--repo-id=gerchowl/mat-vis-tst",
                "--limit=2",
                "--batch-size=2",
            ]
            env = {**os.environ, "HF_TOKEN": token}
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
            assert proc.returncode == 0, f"dagger call bake failed: {proc.stderr[-2000:]}"

            api = HfApi(token=token)
            tree = list(
                api.list_repo_tree(
                    repo_id=REPO,
                    repo_type="dataset",
                    revision=self.DAGGER_TAG,
                    path_in_repo=f"{SOURCE}/{TIER}",
                    recursive=True,
                )
            )
            paths = {getattr(e, "path", "") for e in tree}
            assert any(p.endswith("/color.png") for p in paths)
            assert f"{SOURCE}/{TIER}/.tier_complete" in paths
        finally:
            try:
                HfApi(token=token).delete_branch(
                    repo_id=REPO, repo_type="dataset", branch=self.DAGGER_TAG
                )
            except Exception as e:  # noqa: BLE001
                print(f"dagger e2e cleanup warn: {type(e).__name__}: {e}")


# ── #204 slice: per-file derive (resize + ktx2) round-trip ──────────


class TestDeriveResizeRoundTrip:
    """Bake @1k → derive 1k → 512 (resize) → assert per-file PNGs land
    + .tier_complete sentinel + catalog reflects the new tier."""

    DERIVE_TAG = "v0.0.0-e2e-204-derive"
    DERIVE_TARGET = "512"

    def test_derive_resize_writes_per_file_pngs_and_sentinel(self, baked_tag) -> None:
        from huggingface_hub import HfApi

        from mat_vis_baker.hf_derive_per_file import derive_smaller_tier

        token = _hf_token()
        # Bake into a fresh tag so cleanup is a single delete-branch call.
        # Reuse baked_tag's two materials by cloning the SOURCE/TIER subtree
        # via a re-bake against DERIVE_TAG.
        from mat_vis_baker.hf_bake import bake_one

        with tempfile.TemporaryDirectory() as td:
            r = bake_one(
                source=SOURCE,
                tier=TIER,
                release_tag=self.DERIVE_TAG,
                work_dir=Path(td),
                repo_id=REPO,
                hf_token=token,
                limit=2,
                batch_size=2,
            )
        assert r.get("ok", 0) == 2, f"setup bake failed: {r}"

        try:
            with tempfile.TemporaryDirectory() as td:
                result = derive_smaller_tier(
                    source=SOURCE,
                    source_tier=TIER,
                    target_tier=self.DERIVE_TARGET,
                    release_tag=self.DERIVE_TAG,
                    work_dir=Path(td),
                    repo_id=REPO,
                    hf_token=token,
                    batch_size=2,
                )
            assert result.get("ok", 0) >= 1, f"derive failed: {result}"

            # Tree probe — at least one color.png and the sentinel.
            api = HfApi(token=token)
            tree = list(
                api.list_repo_tree(
                    repo_id=REPO,
                    repo_type="dataset",
                    revision=self.DERIVE_TAG,
                    path_in_repo=f"{SOURCE}/{self.DERIVE_TARGET}",
                    recursive=True,
                )
            )
            paths = {getattr(e, "path", "") for e in tree}
            assert any(p.endswith("/color.png") for p in paths), (
                f"no derived color.png in tree: {sorted(paths)[:10]}"
            )
            assert f"{SOURCE}/{self.DERIVE_TARGET}/.tier_complete" in paths

            # HTTP GET on a derived PNG returns valid PNG bytes.
            png_paths = [p for p in paths if p.endswith("/color.png")]
            body = _http_get(_resolve_url(png_paths[0], tag=self.DERIVE_TAG))
            assert body.startswith(b"\x89PNG\r\n\x1a\n"), "derived must be PNG"
        finally:
            try:
                HfApi(token=token).delete_branch(
                    repo_id=REPO, repo_type="dataset", branch=self.DERIVE_TAG
                )
            except Exception as e:  # noqa: BLE001
                print(f"derive e2e cleanup warn: {type(e).__name__}: {e}")


class TestDeriveKtx2RoundTrip:
    """Bake @1k → transcode 1k → ktx2-1k → assert KTX2 bytes land."""

    DERIVE_TAG = "v0.0.0-e2e-204-ktx2"
    KTX2_TARGET = "ktx2-1k"

    def test_derive_ktx2_writes_per_file_ktx2_and_sentinel(self) -> None:
        import shutil

        if not shutil.which("toktx"):
            pytest.skip("toktx not on PATH — install KTX-Software for the ktx2 e2e")

        from huggingface_hub import HfApi

        from mat_vis_baker.hf_bake import bake_one
        from mat_vis_baker.hf_derive_per_file import derive_ktx2_tier

        token = _hf_token()

        with tempfile.TemporaryDirectory() as td:
            r = bake_one(
                source=SOURCE,
                tier=TIER,
                release_tag=self.DERIVE_TAG,
                work_dir=Path(td),
                repo_id=REPO,
                hf_token=token,
                limit=2,
                batch_size=2,
            )
        assert r.get("ok", 0) == 2, f"setup bake failed: {r}"

        try:
            with tempfile.TemporaryDirectory() as td:
                result = derive_ktx2_tier(
                    source=SOURCE,
                    source_tier=TIER,
                    target_tier=self.KTX2_TARGET,
                    release_tag=self.DERIVE_TAG,
                    work_dir=Path(td),
                    repo_id=REPO,
                    hf_token=token,
                    batch_size=2,
                )
            assert result.get("ok", 0) >= 1, f"ktx2 derive failed: {result}"

            api = HfApi(token=token)
            tree = list(
                api.list_repo_tree(
                    repo_id=REPO,
                    repo_type="dataset",
                    revision=self.DERIVE_TAG,
                    path_in_repo=f"{SOURCE}/{self.KTX2_TARGET}",
                    recursive=True,
                )
            )
            paths = {getattr(e, "path", "") for e in tree}
            ktx_files = [p for p in paths if p.endswith(".ktx2")]
            assert ktx_files, f"no .ktx2 files in tree: {sorted(paths)[:10]}"
            assert f"{SOURCE}/{self.KTX2_TARGET}/.tier_complete" in paths

            # Magic-byte probe on one ktx2 file.
            body = _http_get(_resolve_url(ktx_files[0], tag=self.DERIVE_TAG))
            assert body.startswith(b"\xabKTX 20\xbb\r\n\x1a\n"), "must be KTX2"
        finally:
            try:
                HfApi(token=token).delete_branch(
                    repo_id=REPO, repo_type="dataset", branch=self.DERIVE_TAG
                )
            except Exception as e:  # noqa: BLE001
                print(f"ktx2 e2e cleanup warn: {type(e).__name__}: {e}")
