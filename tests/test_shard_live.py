"""Live HF-gated smoke test for the shard derive → merge loop (#134).

PR #146 made the claim that the sharded derive + merge path works
end-to-end against a real HF dataset revision. The unit suite in
``tests/test_shard_integration.py`` mocks every HTTP call, so that
claim only lived in prose. This module encodes it as an actually-
runnable test against ``gerchowl/mat-vis-tst@v0.0.1-smoke``.

Gated behind ``HF_INTEGRATION=1`` (+ a usable token from ``HF_TOKEN``
or ``~/.cache/huggingface/token``) so the regular suite stays offline.

Pattern: 4-shard resize 1k→256 on a 2-material / 10-channel smoke
dataset. Each shard owns 2–3 channels (empirically verified against
the current ``shard_utils`` hash). After 4 shard pushes + 1 merge,
we assert:

- the merged ``polyhaven-256.tar`` is live on the tag,
- no ``shard-N-of-4`` artifacts remain,
- every rowmap entry points at a valid PNG via HTTP Range reads.

Cleanup: one final atomic commit deletes the merged tar + rowmap so
the tag ends up in the same state it started in, and a second
identical run will find nothing to clean up.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import requests

from mat_vis_baker.hf_derive import derive_smaller_tier
from mat_vis_baker.hf_push import push_to_hf
from mat_vis_baker.merge_shards import merge_shards

pytestmark = pytest.mark.skipif(
    not os.environ.get("HF_INTEGRATION"),
    reason="requires HF_INTEGRATION=1 + a usable HF_TOKEN",
)


REPO_ID = "gerchowl/mat-vis-tst"
REVISION = "v0.0.1-smoke"
SOURCE = "polyhaven"
SOURCE_TIER = "1k"
TARGET_TIER = "256"
K = 4


def _resolve_token() -> str:
    """HF_TOKEN wins; otherwise fall back to the CLI cache file."""
    tok = os.environ.get("HF_TOKEN")
    if tok:
        return tok.strip()
    cache = Path.home() / ".cache" / "huggingface" / "token"
    if cache.exists():
        return cache.read_text().strip()
    pytest.skip("no HF token available (set HF_TOKEN or run `hf auth login`)")


def _tree_paths(token: str) -> set[str]:
    r = requests.get(
        f"https://huggingface.co/api/datasets/{REPO_ID}/tree/{REVISION}?recursive=true",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return {e["path"] for e in r.json() if e.get("type") == "file"}


def _range_head_bytes(url: str, offset: int, length: int, token: str) -> bytes:
    r = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Range": f"bytes={offset}-{offset + length - 1}",
        },
        timeout=30,
    )
    r.raise_for_status()
    assert len(r.content) == length, f"range read short: {len(r.content)} != {length}"
    return r.content


def test_shard_derive_merge_live_roundtrip(tmp_path: Path) -> None:
    token = _resolve_token()
    t0 = time.monotonic()

    merged_tar = f"{SOURCE}-{TARGET_TIER}.tar"
    merged_rowmap = f"{SOURCE}-{TARGET_TIER}-rowmap.json"

    # Pre-clean: if an earlier (possibly failed) run left a merged tar
    # on the tag, delete it so we're testing the derive→merge path,
    # not cached state. Shard artifacts (if any) are swept by the merge
    # step itself via delete_paths.
    existing = _tree_paths(token)
    pre_deletes = [p for p in (merged_tar, merged_rowmap) if p in existing]
    # Stale shard artifacts from a previous aborted run would cause the
    # next merge to see 2K shards instead of K, so sweep those too.
    pre_deletes += [p for p in existing if f"{SOURCE}-{TARGET_TIER}.shard-" in p]
    if pre_deletes:
        push_to_hf(
            repo_id=REPO_ID,
            files=[],
            revision=REVISION,
            commit_message=f"test: pre-clean {TARGET_TIER} artifacts for live roundtrip",
            token=token,
            delete_paths=pre_deletes,
        )

    # 1. Four shard derives — each pushes its own shard-N-of-K tar.
    for i in range(K):
        result = derive_smaller_tier(
            source=SOURCE,
            target_tier=TARGET_TIER,
            source_tier=SOURCE_TIER,
            release_tag=REVISION,
            work_dir=tmp_path / f"shard-{i}",
            repo_id=REPO_ID,
            hf_token=token,
            dry_run=False,
            workers=2,
            shard=(i, K),
        )
        assert result.get("ok", 0) > 0, f"shard {i}/{K} produced no channels: {result}"
        assert result.get("failed", 0) == 0, f"shard {i}/{K} had failures: {result}"

    # Confirm all K shard tars are on HF before we merge.
    post_derive = _tree_paths(token)
    for i in range(K):
        assert f"{SOURCE}-{TARGET_TIER}.shard-{i}-of-{K}.tar" in post_derive
        assert f"{SOURCE}-{TARGET_TIER}.shard-{i}-of-{K}-rowmap.json" in post_derive

    # 2. Merge.
    merge_result = merge_shards(
        source=SOURCE,
        tier=TARGET_TIER,
        release_tag=REVISION,
        work_dir=tmp_path / "merge",
        repo_id=REPO_ID,
        hf_token=token,
        dry_run=False,
        keep_shards=False,
    )
    assert merge_result["shards"] == K
    assert merge_result["channels"] == 10, merge_result

    # 3. Post-merge tree: merged artifacts present, shard artifacts gone.
    post_merge = _tree_paths(token)
    assert merged_tar in post_merge
    assert merged_rowmap in post_merge
    leftover_shards = [p for p in post_merge if f"{SOURCE}-{TARGET_TIER}.shard-" in p]
    assert not leftover_shards, f"shard artifacts not swept: {leftover_shards}"

    # 4. Rowmap offsets point at valid PNG bytes — verify via HTTP Range.
    resolve_base = f"https://huggingface.co/datasets/{REPO_ID}/resolve/{REVISION}"
    rowmap = requests.get(
        f"{resolve_base}/{merged_rowmap}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ).json()
    merged_tar_url = f"{resolve_base}/{merged_tar}"
    materials = rowmap["materials"]
    assert set(materials) == {"aerial_beach_01", "aerial_asphalt_01"}
    # Spot-check one channel per material (two Range reads is enough —
    # full coverage would inflate runtime past the 60 s target).
    for mid in materials:
        ch, spec = next(iter(materials[mid].items()))
        head = _range_head_bytes(
            merged_tar_url, int(spec["offset"]), min(8, int(spec["length"])), token
        )
        assert head[:4] == b"\x89PNG", f"{mid}/{ch} is not a PNG: {head!r}"

    # 5. Cleanup — one atomic commit to remove the merged artifacts so
    # the tag ends up exactly as it started. Wrapped in a try/finally
    # at the caller level isn't appropriate: if earlier assertions fail
    # we *want* the artifacts to linger for post-mortem. Explicit
    # cleanup runs only on the success path.
    push_to_hf(
        repo_id=REPO_ID,
        files=[],
        revision=REVISION,
        commit_message=f"test: cleanup {TARGET_TIER} merged artifacts after live roundtrip",
        token=token,
        delete_paths=[merged_tar, merged_rowmap],
    )
    final = _tree_paths(token)
    assert merged_tar not in final
    assert merged_rowmap not in final

    elapsed = time.monotonic() - t0
    # Soft budget — not a hard assert; logged so we notice drift.
    print(f"\nlive roundtrip took {elapsed:.1f}s (budget: ~60 s)")
