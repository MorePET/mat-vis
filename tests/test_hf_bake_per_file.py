"""Per-file substrate baker (ADR-0012 / #182).

Covers the three load-bearing behaviours of ``bake_one_per_file``:

1. Every (source, tier, material, channel) baked texture lands as an
   individual HF file at ``<source>/<tier>/<mid>/<channel>.{png,ktx2}``.
   No tar, no rowmap.
2. A pre-flight tree scan of the target revision skips materials
   whose files are already committed — resumable-by-default across
   crashes / SIGTERMs / rate-limit stalls.
3. Commits happen in batches (default N=50 materials); each commit is
   a durable checkpoint. A `.tier_complete` sentinel file lands as the
   final commit per tier so clients can detect tier-level atomicity
   (restoring ADR-0007's invariant on top of the new substrate).

Pure-Python tests — HfApi is mocked; no HF calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# Import target — will initially fail (RED) until the module exists.
bake_module_available = False
try:
    from mat_vis_baker.hf_bake_per_file import bake_one_per_file  # type: ignore

    bake_module_available = True
except ImportError:
    bake_one_per_file = None  # type: ignore


pytestmark = pytest.mark.skipif(
    not bake_module_available,
    reason="RED phase — mat_vis_baker.hf_bake_per_file not yet implemented",
)


def _fake_record(mid: str, channels: dict[str, bytes], work_dir: Path):
    """Build a minimal ``MaterialRecord`` stub that has channel bytes
    on disk under ``work_dir / textures / <mid> / <channel>.png``."""
    from mat_vis_baker.common import (
        AttributionBlock,
        MatVisBlock,
        MaterialRecord,
    )

    d = work_dir / "textures" / mid
    d.mkdir(parents=True, exist_ok=True)
    paths = {}
    for ch, data in channels.items():
        p = d / f"{ch}.png"
        p.write_bytes(data)
        paths[ch] = p

    return MaterialRecord(
        id=mid,
        source="polyhaven",
        mat_vis=MatVisBlock(
            name=mid,
            category="other",
            upstream_id=mid,
            attribution=AttributionBlock(license_spdx="CC0-1.0"),
        ),
        texture_paths=paths,
        maps=list(channels.keys()),
        status="ok",
    )


class TestBakeOnePerFile:
    def test_writes_one_hf_file_per_channel(self, tmp_path):
        """Three materials × two channels = six CommitOperationAdd
        entries + one catalog JSON + one .tier_complete sentinel."""
        PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
        fake_records = [
            _fake_record(
                f"mat_{i}",
                {"color": PNG_MAGIC + b"\x00" * 100, "normal": PNG_MAGIC + b"\x00" * 80},
                tmp_path,
            )
            for i in range(3)
        ]

        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
        ):

            def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
                end = None if limit is None else offset + limit
                return fake_records[offset:end]

            fetcher.return_value = _sliced
            api = api_cls.return_value
            api.list_repo_tree.return_value = []  # nothing committed yet

            result = bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
            )

        # Union of all adds across all create_commit calls must contain
        # the 6 texture paths + the catalog + the tier-complete sentinel.
        all_adds: list[str] = []
        for call in api.create_commit.call_args_list:
            for op in call.kwargs.get("operations", call.args[-1] if call.args else []):
                all_adds.append(op.path_in_repo)

        expected_textures = {
            f"polyhaven/1k/mat_{i}/{ch}.png" for i in range(3) for ch in ("color", "normal")
        }
        assert expected_textures <= set(all_adds), (
            f"missing texture files. present={sorted(all_adds)}"
        )
        assert "polyhaven.json" in all_adds
        assert "polyhaven/1k/.tier_complete" in all_adds
        assert result["ok"] == 3
        assert result["failed"] == 0

    def test_preflight_skips_already_committed_materials(self, tmp_path):
        """If two of three materials already live on HF, baker only
        processes the missing one — by-design resume."""
        fake_records = [
            _fake_record(f"mat_{i}", {"color": b"PNG\x00" * 20}, tmp_path) for i in range(3)
        ]

        # Fake tree: mat_0 + mat_1 already have color.png committed.
        from huggingface_hub.hf_api import RepoFile

        existing_files = [
            RepoFile(path=f"polyhaven/1k/mat_{i}/color.png", size=80, oid="x") for i in range(2)
        ]

        bake_calls: list[str] = []

        def track_bake(rec, *a, **k):
            bake_calls.append(rec.id)
            return rec

        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=track_bake),
        ):

            def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
                end = None if limit is None else offset + limit
                return fake_records[offset:end]

            fetcher.return_value = _sliced
            api = api_cls.return_value
            api.list_repo_tree.return_value = existing_files

            bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
            )

        # bake_material called only for mat_2 (the missing one).
        assert bake_calls == ["mat_2"], (
            f"preflight should have skipped mat_0, mat_1; ran {bake_calls}"
        )

    def test_batch_commits_checkpoint_progress(self, tmp_path):
        """batch_size=2 across 5 materials → 3 batch commits +
        catalog commit + sentinel commit. Each batch is durable."""
        fake_records = [
            _fake_record(f"mat_{i}", {"color": b"PNG\x00" * 10}, tmp_path) for i in range(5)
        ]

        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
        ):

            def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
                end = None if limit is None else offset + limit
                return fake_records[offset:end]

            fetcher.return_value = _sliced
            api = api_cls.return_value
            api.list_repo_tree.return_value = []

            bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
                batch_size=2,
            )

        # Expect: 3 texture-batch commits (2+2+1) + 1 catalog commit
        # + 1 sentinel commit = 5 total.
        assert api.create_commit.call_count == 5, (
            f"expected 5 commits (3 batches + catalog + sentinel); "
            f"got {api.create_commit.call_count}"
        )

    def test_prod_target_requires_allow_prod(self, tmp_path):
        """Safety rail: non-*-tst targets require opt-in, same as the
        Dagger-level guard in #178 — enforced at the baker entry too."""
        with pytest.raises(ValueError, match="allow_prod"):
            bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v2026.05.0",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis",  # non-tst — refuse
            )

    def test_empty_fetcher_result_returns_no_materials_error(self, tmp_path):
        """Fetcher with nothing to bake — return early with an explicit
        error code rather than committing an empty sentinel."""
        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
        ):
            fetcher.return_value = lambda *a, **kw: []
            api = api_cls.return_value
            api.list_repo_tree.return_value = []

            result = bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
            )
        assert result.get("error")
        assert result.get("ok", 0) == 0


# ── Substrate-contract: every bake completion writes the same set ────


def _bake_with_records(records, tmp_path, **bake_kwargs):
    """Drive ``bake_one_per_file`` with a fixed record list. Returns
    the mocked HfApi instance so callers can introspect commit history."""
    with (
        patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
        patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
        patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
    ):

        def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
            end = None if limit is None else offset + limit
            return records[offset:end]

        fetcher.return_value = _sliced
        api = api_cls.return_value
        api.list_repo_tree.return_value = []
        bake_one_per_file(
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.0-test",
            work_dir=tmp_path,
            hf_token="t",
            repo_id="gerchowl/mat-vis-tst",
            **bake_kwargs,
        )
        return api


def _commit_path_set(api):
    """Set of every ``path_in_repo`` ever committed by the mocked API."""
    out = set()
    for call in api.create_commit.call_args_list:
        ops = call.kwargs.get("operations") or (call.args[-1] if call.args else [])
        for op in ops:
            out.add(op.path_in_repo)
    return out


class TestManifestEmission:
    """#207 / #210 Part A — assert the static contract paths each bake
    must produce. The bug that motivated #210 was the bake never writing
    ``release-manifest.json``; mocked tests passed because they only
    asserted the textures + catalog + sentinel paths. Tightening the
    contract surface here means a future regression that drops the
    manifest emission fails immediately."""

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80

    def test_commit_path_set_includes_manifest_catalog_sentinel(self, tmp_path):
        records = [_fake_record(f"mat_{i}", {"color": self.PNG}, tmp_path) for i in range(2)]
        api = _bake_with_records(records, tmp_path, batch_size=2)
        paths = _commit_path_set(api)
        # Required substrate-contract paths — every per-file bake emits
        # this set, no matter how many materials / batches.
        assert "release-manifest.json" in paths, paths
        assert "polyhaven.json" in paths, paths
        assert "polyhaven/1k/.tier_complete" in paths, paths

    def test_manifest_commit_bundled_with_catalog(self, tmp_path):
        """The manifest update lands in the SAME commit as the catalog
        — atomicity for clients (#207). A regression that splits them
        into separate commits would leave a window where catalog +
        manifest disagree."""
        records = [_fake_record(f"mat_{i}", {"color": self.PNG}, tmp_path) for i in range(2)]
        api = _bake_with_records(records, tmp_path, batch_size=2)
        # Find the commit containing release-manifest.json.
        manifest_commits = [
            c
            for c in api.create_commit.call_args_list
            if any(op.path_in_repo == "release-manifest.json" for op in c.kwargs["operations"])
        ]
        assert len(manifest_commits) == 1, "manifest must land in exactly one commit"
        ops_in_manifest_commit = {
            op.path_in_repo for op in manifest_commits[0].kwargs["operations"]
        }
        assert "polyhaven.json" in ops_in_manifest_commit, ops_in_manifest_commit

    def test_manifest_commit_carries_parent_commit_for_cas(self, tmp_path):
        """Per #208's CAS retry: the manifest commit MUST set
        ``parent_commit`` so HF returns 412 on a concurrent writer's
        clobber. A regression that drops the kwarg would silently
        re-introduce the multi-source race."""
        records = [_fake_record(f"mat_{i}", {"color": self.PNG}, tmp_path) for i in range(2)]
        api = _bake_with_records(records, tmp_path, batch_size=2)
        manifest_call = next(
            c
            for c in api.create_commit.call_args_list
            if any(op.path_in_repo == "release-manifest.json" for op in c.kwargs["operations"])
        )
        assert "parent_commit" in manifest_call.kwargs, (
            "manifest commit must opt into HF's parent_commit lock for CAS retry"
        )

    def test_sentinel_is_strictly_last_commit(self, tmp_path):
        """The .tier_complete sentinel is the final commit per tier so
        clients can probe one file to check atomicity. Manifest commit
        must come BEFORE the sentinel, not after — otherwise readers
        that arrive between manifest-commit and sentinel-commit see a
        catalog claiming complete tiers without the sentinel marker."""
        records = [_fake_record("mat_0", {"color": self.PNG}, tmp_path)]
        api = _bake_with_records(records, tmp_path, batch_size=1)
        last_call = api.create_commit.call_args_list[-1]
        last_paths = {op.path_in_repo for op in last_call.kwargs["operations"]}
        assert last_paths == {"polyhaven/1k/.tier_complete"}, last_paths


# ── CAS retry on the manifest commit (#208) ──────────────────────────


def _make_412_error(msg: str = "412 Precondition Failed: revision moved") -> Exception:
    """A generic exception that matches the substring detection in
    ``hf_bake_per_file.bake_one_per_file``'s retry loop. We don't import
    huggingface_hub's specific error class — the production code does
    substring matching on the message because HF's error type changed
    between hub releases."""
    return RuntimeError(msg)


class TestCasRetryOnManifestCommit:
    """The 412-retry loop in ``bake_one_per_file``'s catalog+manifest
    commit was previously untested. The tests here exercise the three
    distinct code paths: a single 412 → success on retry, exhaustion of
    the retry budget, and a non-412 error that must NOT be retried."""

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80

    def _drive_bake_with_commit_side_effect(
        self, tmp_path, side_effect, *, expect_raises: bool = False
    ):
        """Helper: drive ``bake_one_per_file`` with a custom
        ``api.create_commit.side_effect``. Pre-seeds an empty manifest
        on the revision so the read leg of the CAS loop has a known
        return value. Returns ``(api, fetch_mfst, exc)`` where ``exc``
        is the raised exception (or ``None`` on success)."""
        from types import SimpleNamespace

        records = [_fake_record("mat_0", {"color": self.PNG}, tmp_path)]
        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch("mat_vis_baker.hf_bake_per_file.bake_material", side_effect=lambda r, *a, **k: r),
            patch(
                "mat_vis_baker.hf_bake_per_file._fetch_manifest_with_parent",
                return_value=({}, "deadbeef"),
            ) as fetch_mfst,
        ):

            def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
                end = None if limit is None else offset + limit
                return records[offset:end]

            fetcher.return_value = _sliced
            api = api_cls.return_value
            api.list_repo_tree.return_value = []
            api.create_commit.side_effect = side_effect
            api.create_commit.return_value = SimpleNamespace(oid="cafef00d")

            exc: Exception | None = None
            try:
                bake_one_per_file(
                    source="polyhaven",
                    tier="1k",
                    release_tag="v0.0.0-test",
                    work_dir=tmp_path,
                    hf_token="t",
                    repo_id="gerchowl/mat-vis-tst",
                    batch_size=1,
                )
            except Exception as e:  # noqa: BLE001
                exc = e

            if expect_raises:
                assert exc is not None, "expected an exception to propagate"
            else:
                assert exc is None, f"unexpected exception: {exc!r}"
            return api, fetch_mfst, exc

    def test_412_retry_succeeds_on_second_attempt(self, tmp_path):
        """One 412 → fetch fresh parent SHA → retry → success."""
        from types import SimpleNamespace

        attempts = {"n": 0}

        def side_effect(*args, **kwargs):
            ops = kwargs.get("operations") or []
            paths = {op.path_in_repo for op in ops}
            if "release-manifest.json" in paths:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise _make_412_error()
            return SimpleNamespace(oid="cafef00d")

        api, fetch_mfst, _ = self._drive_bake_with_commit_side_effect(
            tmp_path, side_effect=side_effect, expect_raises=False
        )

        # Manifest commit attempted twice — once 412, once success.
        assert attempts["n"] == 2, f"expected 2 manifest attempts, got {attempts['n']}"
        # Fresh manifest re-fetched on the retry path (initial + retry).
        assert fetch_mfst.call_count == 2, fetch_mfst.call_count

    def test_412_retry_exhausts_after_max_retries(self, tmp_path):
        """Continuous 412 → loop exhausts → raises after max_retries.
        The implementation hardcodes max_retries=6 in the bake path."""

        def side_effect(*args, **kwargs):
            ops = kwargs.get("operations") or []
            paths = {op.path_in_repo for op in ops}
            if "release-manifest.json" in paths:
                raise _make_412_error()
            from types import SimpleNamespace

            return SimpleNamespace(oid="cafef00d")

        api, fetch_mfst, exc = self._drive_bake_with_commit_side_effect(
            tmp_path, side_effect=side_effect, expect_raises=True
        )

        # CAS loop fired exactly max_retries (=6) times before bubbling
        # the 412 out. Each attempt re-fetches the manifest first, so
        # fetch_count == attempt_count == 6.
        assert fetch_mfst.call_count == 6, (
            f"expected exactly 6 retry attempts, got {fetch_mfst.call_count}"
        )
        # The exception that escapes is the 412 from the last attempt.
        assert exc is not None and "412" in str(exc), exc

    def test_non_412_error_is_not_retried(self, tmp_path):
        """An auth / network error must NOT trigger CAS retry — that
        would mask real failures behind exhausted-retries fatigue."""

        attempts = {"n": 0}

        def side_effect(*args, **kwargs):
            ops = kwargs.get("operations") or []
            paths = {op.path_in_repo for op in ops}
            if "release-manifest.json" in paths:
                attempts["n"] += 1
                raise RuntimeError("401 Unauthorized: token rejected")
            from types import SimpleNamespace

            return SimpleNamespace(oid="cafef00d")

        api, fetch_mfst, exc = self._drive_bake_with_commit_side_effect(
            tmp_path, side_effect=side_effect, expect_raises=True
        )

        # Exactly one manifest attempt — the 401 propagated out without
        # a retry. fetch_manifest fired once (the pre-attempt read),
        # NOT a second time (no retry leg).
        assert attempts["n"] == 1, f"401 must not retry (got {attempts['n']} attempts)"
        assert fetch_mfst.call_count == 1, fetch_mfst.call_count
        assert exc is not None and "401" in str(exc), exc


# ── #228: bytes-aware batching (first-of-N-or-bytes) ─────────────────


PNG_HDR = b"\x89PNG\r\n\x1a\n"


def _texture_commit_calls(api):
    """Texture-batch commits only — strip the catalog + sentinel calls.

    Per the substrate contract the trailing two commits are
    catalog+manifest then sentinel, so anything before that is a
    texture batch flush."""
    return [
        c
        for c in api.create_commit.call_args_list
        if not any(
            op.path_in_repo
            in {
                "release-manifest.json",
                "polyhaven.json",
                "polyhaven/1k/.tier_complete",
            }
            for op in c.kwargs["operations"]
        )
    ]


class TestBytesAwareBatching:
    """#228: flush trips on first-of-N-or-bytes — whichever bound hits
    first. Locks down both halves of the OR so a regression that drops
    either ceiling fails fast."""

    def test_count_ceiling_flushes_when_bytes_below_max(self, tmp_path):
        """Many tiny materials → bytes is far below ceiling → count
        ceiling (batch_size) drives the flush. With batch_size=2 across
        5 materials we expect 3 texture-batch commits (2 + 2 + 1)."""
        # Each material is tiny (~108 bytes per channel) — well below
        # any reasonable bytes ceiling.
        small = PNG_HDR + b"\x00" * 100
        fake_records = [_fake_record(f"mat_{i}", {"color": small}, tmp_path) for i in range(5)]

        api = _bake_with_records(
            fake_records,
            tmp_path,
            batch_size=2,
            batch_max_bytes=10 * 1024 * 1024,  # 10 MiB — far above any single material
        )

        texture_calls = _texture_commit_calls(api)
        assert len(texture_calls) == 3, (
            f"expected 3 texture-batch commits (2+2+1, count-driven); got {len(texture_calls)}"
        )

    def test_bytes_ceiling_flushes_when_count_below_max(self, tmp_path):
        """Few large materials → count is below ceiling → bytes ceiling
        (batch_max_bytes) drives the flush. Each material's payload
        already exceeds the ceiling, so every append immediately
        crosses it and flushes — one material per commit despite
        ``batch_size`` being far above 1."""
        # Each material is "large": ~5 MiB per channel; the ceiling
        # at 4 MiB is below a single material's payload so the very
        # first append crosses the bound and triggers the flush.
        big_payload = PNG_HDR + b"\x00" * (5 * 1024 * 1024)
        fake_records = [
            _fake_record(f"mat_{i}", {"color": big_payload}, tmp_path) for i in range(3)
        ]

        api = _bake_with_records(
            fake_records,
            tmp_path,
            batch_size=300,  # high — count must NOT be the binding constraint
            batch_max_bytes=4 * 1024 * 1024,  # below one material's payload
        )

        texture_calls = _texture_commit_calls(api)
        # Each material's 5 MiB payload exceeds 4 MiB on the very
        # first append → 3 materials = 3 texture commits.
        assert len(texture_calls) == 3, (
            f"expected 3 texture commits (bytes-driven, one material each); "
            f"got {len(texture_calls)}"
        )

    def test_preflight_skip_still_works_under_bytes_aware_batching(self, tmp_path):
        """The preflight skip primitive must keep working: of N=5
        materials with M=2 already-committed, only 3 should reach
        the bake/commit path. Bytes ceiling high so the count ceiling
        bounds batches predictably."""
        fake_records = [
            _fake_record(f"mat_{i}", {"color": PNG_HDR + b"\x00" * 200}, tmp_path) for i in range(5)
        ]

        # Pretend mat_0 + mat_1 already on HF.
        from huggingface_hub.hf_api import RepoFile

        existing_files = [
            RepoFile(path=f"polyhaven/1k/mat_{i}/color.png", size=80, oid="x") for i in range(2)
        ]

        bake_calls: list[str] = []

        def track_bake(rec, *a, **k):
            bake_calls.append(rec.id)
            return rec

        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch(
                "mat_vis_baker.hf_bake_per_file.bake_material",
                side_effect=track_bake,
            ),
        ):

            def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
                end = None if limit is None else offset + limit
                return fake_records[offset:end]

            fetcher.return_value = _sliced
            api = api_cls.return_value
            api.list_repo_tree.return_value = existing_files

            result = bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
                batch_size=2,  # 3 unskipped → 2 then 1 → 2 texture commits
                batch_max_bytes=10 * 1024 * 1024,
            )

        # Only the 3 unskipped materials reached bake_material.
        assert bake_calls == ["mat_2", "mat_3", "mat_4"], bake_calls
        assert result["ok"] == 3
        assert result["skipped_preflight"] == 2

        texture_calls = _texture_commit_calls(api)
        assert len(texture_calls) == 2, (
            f"expected 2 texture commits across 3 unskipped materials "
            f"(batch_size=2); got {len(texture_calls)}"
        )


# ── #292: upstream-MTLX pack-and-push ────────────────────────────────


def _catalog_commit_call(api):
    """Find the create_commit call that carries release-manifest.json
    (the catalog + manifest commit, where the per-source ``mtlx`` field
    and ``<source>-mtlx.json`` blob land)."""
    for c in api.create_commit.call_args_list:
        for op in c.kwargs["operations"]:
            if op.path_in_repo == "release-manifest.json":
                return c
    raise AssertionError("no catalog+manifest commit found")


class TestMtlxPackAndPush:
    """#292: bake_one_per_file packs upstream .mtlx files into
    ``<source>-mtlx.json`` and ships it in the same atomic commit as
    the catalog + manifest, with ``sources.<src>.mtlx`` stamped on
    the manifest. Without this wire-up, ``client.mtlx(...).original``
    returns None for every gpuopen material in v2026.04.0/.1/.2."""

    def test_packs_and_pushes_mtlx_when_fetcher_writes_files(self, tmp_path):
        """Fetcher writes .mtlx files into ``mtlx_dir/<source>/...`` (the
        layout both gpuopen and polyhaven use). The bake must (a) pack
        them into ``<source>-mtlx.json``, (b) include the JSON blob in
        the catalog commit, and (c) stamp ``sources.<src>.mtlx`` on the
        release manifest."""
        PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
        fake_records = [
            _fake_record(f"mat_{i}", {"color": PNG_MAGIC + b"\x00" * 32}, tmp_path)
            for i in range(2)
        ]

        def _writing_fetch(tier, textures_dir, *, limit=None, offset=0, mtlx_dir=None, **kw):
            # Mirror the gpuopen fetch contract: write upstream .mtlx
            # files into ``mtlx_dir/<source>/<mid>/material.mtlx`` so
            # ``pack_original_mtlx_json`` can pick them up.
            end = None if limit is None else offset + limit
            slice_ = fake_records[offset:end]
            if mtlx_dir is not None:
                for rec in slice_:
                    p = Path(mtlx_dir) / "polyhaven" / rec.id / "material.mtlx"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(f"<materialx>{rec.id}</materialx>")
            return slice_

        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch(
                "mat_vis_baker.hf_bake_per_file.bake_material",
                side_effect=lambda r, *a, **k: r,
            ),
        ):
            fetcher.return_value = _writing_fetch
            api = api_cls.return_value
            api.list_repo_tree.return_value = []
            # Empty manifest on first read → merge starts from {}.
            api.hf_hub_download.side_effect = FileNotFoundError("no manifest yet")

            bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
            )

        # (a) The packed JSON must exist on disk.
        packed_path = tmp_path / "polyhaven-mtlx.json"
        assert packed_path.exists(), "expected polyhaven-mtlx.json packed in work_dir"
        import json as _json

        packed = _json.loads(packed_path.read_text())
        assert set(packed.keys()) == {"mat_0", "mat_1"}
        assert packed["mat_0"] == "<materialx>mat_0</materialx>"

        # (b) The catalog+manifest commit must include the mtlx blob.
        catalog_call = _catalog_commit_call(api)
        commit_paths = {op.path_in_repo for op in catalog_call.kwargs["operations"]}
        assert "polyhaven-mtlx.json" in commit_paths, (
            f"polyhaven-mtlx.json missing from catalog commit; got {sorted(commit_paths)}"
        )
        # And it should be bundled with the catalog + manifest, atomically.
        assert "polyhaven.json" in commit_paths
        assert "release-manifest.json" in commit_paths

        # (c) The committed manifest must stamp sources.polyhaven.mtlx.
        manifest_op = next(
            op
            for op in catalog_call.kwargs["operations"]
            if op.path_in_repo == "release-manifest.json"
        )
        manifest_payload = _json.loads(Path(manifest_op.path_or_fileobj).read_text())
        assert manifest_payload["sources"]["polyhaven"]["mtlx"] == "polyhaven-mtlx.json"

    def test_no_mtlx_files_means_no_blob_no_manifest_field(self, tmp_path):
        """ambientcg-shaped sources don't ship upstream .mtlx. The bake
        must NOT publish an empty ``<source>-mtlx.json`` and must NOT
        stamp ``sources.<src>.mtlx`` on the manifest — both would
        mislead clients into thinking originals are available."""
        PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
        fake_records = [
            _fake_record(f"mat_{i}", {"color": PNG_MAGIC + b"\x00" * 32}, tmp_path)
            for i in range(2)
        ]

        with (
            patch("mat_vis_baker.hf_bake_per_file._get_fetcher") as fetcher,
            patch("mat_vis_baker.hf_bake_per_file.HfApi") as api_cls,
            patch(
                "mat_vis_baker.hf_bake_per_file.bake_material",
                side_effect=lambda r, *a, **k: r,
            ),
        ):

            def _sliced(tier, textures_dir, *, limit=None, offset=0, **kw):
                end = None if limit is None else offset + limit
                return fake_records[offset:end]

            fetcher.return_value = _sliced
            api = api_cls.return_value
            api.list_repo_tree.return_value = []
            api.hf_hub_download.side_effect = FileNotFoundError("no manifest yet")

            bake_one_per_file(
                source="polyhaven",
                tier="1k",
                release_tag="v0.0.0-test",
                work_dir=tmp_path,
                hf_token="t",
                repo_id="gerchowl/mat-vis-tst",
            )

        catalog_call = _catalog_commit_call(api)
        commit_paths = {op.path_in_repo for op in catalog_call.kwargs["operations"]}
        assert "polyhaven-mtlx.json" not in commit_paths

        import json as _json

        manifest_op = next(
            op
            for op in catalog_call.kwargs["operations"]
            if op.path_in_repo == "release-manifest.json"
        )
        manifest_payload = _json.loads(Path(manifest_op.path_or_fileobj).read_text())
        assert "mtlx" not in manifest_payload["sources"]["polyhaven"]


class TestMergeManifestMtlxField:
    """Unit-level: ``_merge_manifest_for_source`` accepts an optional
    ``mtlx_filename`` kwarg and stamps ``sources.<src>.mtlx`` only when
    it's set. Locks the field name down so a future rename can't drift
    out of sync with ``MatVisClient._fetch_mtlx_original_map``."""

    def test_mtlx_filename_stamped_when_provided(self):
        from mat_vis_baker.hf_bake_per_file import _merge_manifest_for_source

        merged = _merge_manifest_for_source(
            {}, "gpuopen", "1k", "v0.0.0-test", mtlx_filename="gpuopen-mtlx.json"
        )
        assert merged["sources"]["gpuopen"]["mtlx"] == "gpuopen-mtlx.json"
        assert merged["sources"]["gpuopen"]["catalog"] == "gpuopen.json"

    def test_mtlx_field_absent_when_not_provided(self):
        from mat_vis_baker.hf_bake_per_file import _merge_manifest_for_source

        merged = _merge_manifest_for_source({}, "ambientcg", "1k", "v0.0.0-test")
        assert "mtlx" not in merged["sources"]["ambientcg"]

    def test_mtlx_field_preserved_across_subsequent_tier_merges(self):
        """If a later derive (KTX2 transcode) re-merges without the
        kwarg, the previously-stamped mtlx field must NOT be wiped —
        otherwise the catalog and the mtlx file fall out of sync after
        a derive run."""
        from mat_vis_baker.hf_bake_per_file import _merge_manifest_for_source

        m = _merge_manifest_for_source(
            {}, "gpuopen", "1k", "v0.0.0-test", mtlx_filename="gpuopen-mtlx.json"
        )
        m = _merge_manifest_for_source(m, "gpuopen", "ktx2-1k", "v0.0.0-test")
        assert m["sources"]["gpuopen"]["mtlx"] == "gpuopen-mtlx.json"
        assert "ktx2-1k" in m["sources"]["gpuopen"]["tiers"]
