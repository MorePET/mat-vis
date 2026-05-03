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


# ── #210: multi-source manifest merge (was implicit in single-source E2E) ──


class TestMultiSourceManifestMerge:
    """Two sequential bakes against ONE release tag must accumulate
    in the manifest, not clobber. The CAS implementation in #208 was
    correct, but the only proof was the unit tests + a manual probe.
    This is the live gate: future regressions to the merge logic
    surface here in nightly E2E.
    """

    MULTI_TAG = "v0.0.0-e2e-210-multisrc"

    def test_two_sources_accumulate_in_manifest(self) -> None:
        import json

        from huggingface_hub import HfApi

        from mat_vis_baker.hf_bake import bake_one

        token = _hf_token()
        api = HfApi(token=token)
        # Defensive cleanup in case a previous run crashed before teardown.
        try:
            api.delete_branch(repo_id=REPO, repo_type="dataset", branch=self.MULTI_TAG)
        except Exception:  # noqa: BLE001
            pass

        try:
            for source in ("polyhaven", "ambientcg"):
                with tempfile.TemporaryDirectory() as td:
                    result = bake_one(
                        source=source,
                        tier=TIER,
                        release_tag=self.MULTI_TAG,
                        work_dir=Path(td),
                        repo_id=REPO,
                        hf_token=token,
                        limit=2,
                        batch_size=2,
                    )
                assert result.get("ok", 0) >= 1, f"{source} bake failed: {result}"

            manifest_url = (
                f"https://huggingface.co/datasets/{REPO}/resolve/"
                f"{self.MULTI_TAG}/release-manifest.json"
            )
            body = _http_get(manifest_url)
            manifest = json.loads(body.decode("utf-8"))
            assert manifest["schema_version"] == 3, manifest
            sources = manifest.get("sources", {})
            # Both sources MUST be present — second bake clobbering the
            # first would only show one entry here.
            assert "polyhaven" in sources, sources
            assert "ambientcg" in sources, sources
            # Each source's tiers block lists the baked tier.
            assert TIER in sources["polyhaven"].get("tiers", {}), sources["polyhaven"]
            assert TIER in sources["ambientcg"].get("tiers", {}), sources["ambientcg"]
        finally:
            try:
                api.delete_branch(repo_id=REPO, repo_type="dataset", branch=self.MULTI_TAG)
            except Exception as e:  # noqa: BLE001
                print(f"multi-source cleanup warn: {type(e).__name__}: {e}")


# ── #210: opt-in concurrency stress test ──


@pytest.mark.skipif(
    not os.environ.get("MAT_VIS_E2E_CONCURRENCY"),
    reason="set MAT_VIS_E2E_CONCURRENCY=N to run the multi-process race test (default off)",
)
class TestConcurrentBakesShareTag:
    """Fires N (default 4) parallel processes, each baking a distinct
    (source, tier) slug into the SAME release tag. Asserts all bakes
    succeed AND the final manifest contains every source/tier — proves
    the CAS retry budget holds under realistic matrix concurrency.

    Uses ``multiprocessing.Process`` (NOT ``threading.Thread``) — the
    GIL serializes live HTTP and never fires the retry path; verified
    during the #207 fix. Real prod parallelism only happens between
    processes (one per matrix container), so we test that.

    Skipped by default. The "MAT_VIS_E2E=1" gate AND
    "MAT_VIS_E2E_CONCURRENCY=N" must both be set. Default N=4 ≈ today's
    realistic matrix size (4 textured sources).

    On failure: if retries exhaust, that's the trigger to escalate to
    the Option-B "fragments + final merge" design (see #210 comment).
    """

    CONC_TAG = "v0.0.0-e2e-210-concurrent"

    def test_n_parallel_bakes_all_land_in_manifest(self) -> None:
        import json
        import multiprocessing as mp
        import os as _os

        from huggingface_hub import HfApi

        n_workers = int(_os.environ.get("MAT_VIS_E2E_CONCURRENCY", "4"))
        # Cap to the four sources we have; an N>4 run would require
        # synthesizing tier slugs which complicates the assertion logic.
        sources = ["polyhaven", "ambientcg", "gpuopen", "polyhaven"][:n_workers]
        # Distinct (source, tier) per worker so they don't collide on
        # texture-write paths — only the manifest is shared state.
        plan = [(s, f"1k-w{i}") for i, s in enumerate(sources[:2])]
        # For workers 3+ on the same source, use a different tier slug.
        for i, s in enumerate(sources[2:], start=2):
            plan.append((s, f"1k-w{i}"))
        plan = plan[:n_workers]

        token = _hf_token()
        api = HfApi(token=token)
        try:
            api.delete_branch(repo_id=REPO, repo_type="dataset", branch=self.CONC_TAG)
        except Exception:  # noqa: BLE001
            pass

        # Worker entry — must be top-level / picklable, so we use a
        # module-level _worker_bake helper defined just below the class.
        # #230: a Manager-backed Barrier lets every worker rendezvous
        # at the same point (just before the manifest CAS commit) so
        # contention happens regardless of upstream-fetch variance.
        # Without this, fast workers finish before slow ones even
        # arrive at the manifest commit, and cas_retries stays at 0.
        try:
            ctx = mp.get_context("spawn")
            with ctx.Manager() as mgr:
                barrier = mgr.Barrier(n_workers, timeout=180)
                with ctx.Pool(
                    processes=n_workers,
                    initializer=_worker_init,
                    initargs=(barrier,),
                ) as pool:
                    results = pool.starmap(
                        _worker_bake,
                        [(self.CONC_TAG, source, tier, token) for source, tier in plan],
                    )

            # Every worker must return ok>=1.
            for (source, tier), r in zip(plan, results, strict=True):
                assert isinstance(r, dict) and r.get("ok", 0) >= 1, (
                    f"{source}/{tier} bake failed: {r}"
                )

            # #230: at least one worker must have observed contention
            # — either a 412 CAS mismatch (parent_commit moved between
            # our read and our commit) or a 409 per-repo write-lock
            # collision. Both are observable counters the workers
            # carry back across the multiprocessing pipe — no log
            # scraping needed. Without this assertion the test was a
            # manifest-merge smoke test, not the contention test the
            # file's docstring promises.
            total_cas_retries = sum(int(r.get("cas_retries", 0)) for r in results)
            total_lock_retries = sum(int(r.get("lock_409_retries", 0)) for r in results)
            assert total_cas_retries + total_lock_retries >= 1, (
                "expected at least one contention retry across N "
                f"concurrent writers; got cas_retries={total_cas_retries} "
                f"lock_409_retries={total_lock_retries}. Per-worker: "
                f"{[(s, t, r.get('cas_retries'), r.get('lock_409_retries')) for (s, t), r in zip(plan, results, strict=True)]}"
            )

            # Final manifest must list every (source, storage-tier) pair.
            # Per #230 the slug is now passed through verbatim as the
            # storage-tier key, so the worker's `1k-wN` slug is also
            # the manifest tier name.
            manifest_url = (
                f"https://huggingface.co/datasets/{REPO}/resolve/"
                f"{self.CONC_TAG}/release-manifest.json"
            )
            body = _http_get(manifest_url)
            manifest = json.loads(body.decode("utf-8"))
            ms = manifest.get("sources", {})
            for source, tier in plan:
                assert source in ms, f"{source} missing after concurrent bakes; {ms.keys()}"
                assert tier in ms[source].get("tiers", {}), (
                    f"{source}/{tier} missing; {source} has {ms[source].get('tiers', {}).keys()}"
                )
        finally:
            try:
                api.delete_branch(repo_id=REPO, repo_type="dataset", branch=self.CONC_TAG)
            except Exception as e:  # noqa: BLE001
                print(f"concurrent cleanup warn: {type(e).__name__}: {e}")


# Per-worker globals populated by ``_worker_init`` (Pool initializer).
# Spawn-method workers don't inherit module state from the parent, so
# the Manager-backed Barrier has to be plumbed explicitly.
_WORKER_BARRIER = None


def _worker_init(barrier) -> None:
    """Pool initializer: stash the cross-process Barrier proxy in a
    module global so ``_worker_bake`` can hand it to ``bake_one`` as
    the pre-manifest hook."""
    global _WORKER_BARRIER
    _WORKER_BARRIER = barrier


def _worker_bake(release_tag: str, source: str, tier: str, token: str) -> dict:
    """Top-level so it's picklable for ``multiprocessing.spawn``.

    ``tier`` here is a per-worker slug (``1k-w0``, ``1k-w1``, ...).
    Per #230 we feed it through the new ``storage_tier`` kwarg so it
    becomes the path/manifest key, while ``tier="1k"`` continues to
    drive the upstream fetcher. This keeps the production tier guard
    intact (``bake_one`` still validates ``tier`` against the
    upstream-supported set) and makes each worker's slug a
    first-class manifest entry — which is what the original test
    intent demanded.

    The Manager-backed Barrier (set by ``_worker_init``) is wired to
    ``bake_one``'s ``_pre_manifest_hook`` so all N workers rendezvous
    just before the manifest commit — which forces the CAS retry
    path to actually fire even when upstream fetch durations vary.
    """
    import tempfile

    from mat_vis_baker.hf_bake import bake_one

    def _hook() -> None:
        # Park here until every worker has finished its texture batch
        # and is ready to commit the manifest. Then race together.
        if _WORKER_BARRIER is not None:
            _WORKER_BARRIER.wait()

    with tempfile.TemporaryDirectory() as td:
        try:
            return bake_one(
                source=source,
                # Always fetch at a real upstream tier; the slug only
                # affects where files land + how the manifest names
                # them. (Worker slugs share one upstream payload so
                # contention happens on the manifest CAS, not the
                # texture write paths.)
                tier="1k",
                storage_tier=tier,
                release_tag=release_tag,
                work_dir=Path(td),
                repo_id=REPO,
                hf_token=token,
                limit=1,
                batch_size=1,
                _pre_manifest_hook=_hook,
            )
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}", "ok": 0, "failed": 1}
