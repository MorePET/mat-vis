"""End-to-end shard invariants (#134).

Exercises the full hf-derive → merge-shards loop without touching HF:
mocks the HTTP layer so each shard's derive reads bytes from a
pre-built source tar and writes shard artifacts to disk, then
``merge_shards`` reads those shard tars back and reassembles.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from mat_vis_baker.hf_derive import derive_smaller_tier
from mat_vis_baker.merge_shards import (
    _discover_shards,
    _merge_catalogs,
    _validate_shards,
    merge_shards,
)
from mat_vis_baker.tar_writer import TarWriter


def _png(size: int, color: tuple[int, int, int] = (200, 100, 50)) -> bytes:
    img = Image.new("RGB", (size, size), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_src(tmp_path: Path, n_materials: int = 16) -> tuple[Path, dict]:
    tar = tmp_path / "polyhaven-1k.tar"
    with TarWriter(tar) as tw:
        for i in range(n_materials):
            mid = f"mat_{i:03d}"
            for ch in ("color", "normal", "roughness"):
                tw.add_channel(mid, ch, _png(1024, ((i * 17) % 255, 100, 50)))
        materials = tw.finalize()
    rowmap = {
        "version": 1,
        "release_tag": "v0.0.0-test",
        "source": "polyhaven",
        "tier": "1k",
        "tar_file": "polyhaven-1k.tar",
        "materials": materials,
    }
    return tar, rowmap


def _mock_range(src_bytes: bytes):
    def fake(*, session, tar_url, spec, token):
        lo, length = int(spec["offset"]), int(spec["length"])
        return src_bytes[lo : lo + length]

    return fake


def test_shard_coverage_union_equals_unsharded(tmp_path: Path) -> None:
    """Running all K shards of a derive produces disjoint rowmaps whose
    union equals the unsharded derive's rowmap (material+channel level)."""
    src_tar, src_rowmap = _build_src(tmp_path, n_materials=16)
    src_bytes = src_tar.read_bytes()
    work_root = tmp_path / "work"

    # Unsharded reference run.
    with (
        patch("mat_vis_baker.hf_derive._pin_commit", return_value="deadbeef"),
        patch("mat_vis_baker.hf_derive._fetch_rowmap", return_value=src_rowmap),
        patch("mat_vis_baker.hf_derive._range_read", side_effect=_mock_range(src_bytes)),
    ):
        derive_smaller_tier(
            source="polyhaven",
            target_tier="512",
            source_tier="1k",
            release_tag="v0.0.0-test",
            work_dir=work_root / "ref",
            hf_token="t",
            dry_run=True,
            workers=2,
        )
    ref_rowmap = json.loads((work_root / "ref" / "polyhaven-512-rowmap.json").read_text())
    ref_pairs = {(mid, ch) for mid, channels in ref_rowmap["materials"].items() for ch in channels}

    # All K shards.
    K = 4
    per_shard: list[set[tuple[str, str]]] = []
    for i in range(K):
        with (
            patch("mat_vis_baker.hf_derive._pin_commit", return_value="deadbeef"),
            patch("mat_vis_baker.hf_derive._fetch_rowmap", return_value=src_rowmap),
            patch("mat_vis_baker.hf_derive._range_read", side_effect=_mock_range(src_bytes)),
        ):
            derive_smaller_tier(
                source="polyhaven",
                target_tier="512",
                source_tier="1k",
                release_tag="v0.0.0-test",
                work_dir=work_root / f"shard-{i}",
                hf_token="t",
                dry_run=True,
                workers=2,
                shard=(i, K),
            )
        suffix = f".shard-{i}-of-{K}"
        rm_path = work_root / f"shard-{i}" / f"polyhaven-512{suffix}-rowmap.json"
        rm = json.loads(rm_path.read_text())
        pairs = {(mid, ch) for mid, channels in rm["materials"].items() for ch in channels}
        per_shard.append(pairs)

    # Disjoint.
    for i in range(K):
        for j in range(i + 1, K):
            assert not per_shard[i] & per_shard[j], f"shards {i}, {j} overlap"
    # Union equals reference.
    union: set[tuple[str, str]] = set()
    for s in per_shard:
        union |= s
    assert union == ref_pairs


def test_empty_shard_is_noop_not_error(tmp_path: Path) -> None:
    """When shard_total is large enough that some shards own zero
    channels, those shards must return cleanly (no terminal-gate
    crash). Uses a 1-material source with shard_total=8; most shards
    are guaranteed to be empty."""
    src_tar, src_rowmap = _build_src(tmp_path, n_materials=1)
    src_bytes = src_tar.read_bytes()

    hit_empty = False
    for i in range(8):
        dest = tmp_path / f"s{i}"
        with (
            patch("mat_vis_baker.hf_derive._pin_commit", return_value="deadbeef"),
            patch("mat_vis_baker.hf_derive._fetch_rowmap", return_value=src_rowmap),
            patch("mat_vis_baker.hf_derive._range_read", side_effect=_mock_range(src_bytes)),
        ):
            result = derive_smaller_tier(
                source="polyhaven",
                target_tier="512",
                source_tier="1k",
                release_tag="v0.0.0-test",
                work_dir=dest,
                hf_token="t",
                dry_run=True,
                workers=1,
                shard=(i, 8),
            )
        # A shard with no work returns ok=0 with "no channels resized"
        # — not a crash.
        if result.get("ok", 0) == 0:
            hit_empty = True
            assert "error" in result
    assert hit_empty, "expected at least one empty shard in this setup"


def test_shard_deterministic_bytes(tmp_path: Path) -> None:
    """Same input + same shard index → identical output bytes (re-runnable)."""
    src_tar, src_rowmap = _build_src(tmp_path, n_materials=8)
    src_bytes = src_tar.read_bytes()

    def run(dest: Path) -> bytes:
        with (
            patch("mat_vis_baker.hf_derive._pin_commit", return_value="deadbeef"),
            patch("mat_vis_baker.hf_derive._fetch_rowmap", return_value=src_rowmap),
            patch("mat_vis_baker.hf_derive._range_read", side_effect=_mock_range(src_bytes)),
        ):
            derive_smaller_tier(
                source="polyhaven",
                target_tier="512",
                source_tier="1k",
                release_tag="v0.0.0-test",
                work_dir=dest,
                hf_token="t",
                dry_run=True,
                workers=1,  # serial so tar member order is deterministic
                shard=(1, 4),
            )
        return (dest / "polyhaven-512.shard-1-of-4.tar").read_bytes()

    a = run(tmp_path / "a")
    b = run(tmp_path / "b")
    assert a == b


def test_validate_shards_happy_path() -> None:
    shards = [
        (0, 3, "x-1k.shard-0-of-3.tar"),
        (1, 3, "x-1k.shard-1-of-3.tar"),
        (2, 3, "x-1k.shard-2-of-3.tar"),
    ]
    assert _validate_shards(shards) == 3


def test_validate_shards_missing() -> None:
    shards = [(0, 3, "a"), (2, 3, "c")]
    import pytest

    with pytest.raises(RuntimeError, match="missing=\\[1\\]"):
        _validate_shards(shards)


def test_validate_shards_inconsistent_total() -> None:
    shards = [(0, 4, "a"), (1, 3, "b")]
    import pytest

    with pytest.raises(RuntimeError, match="inconsistent shard_total"):
        _validate_shards(shards)


def test_validate_shards_empty() -> None:
    import pytest

    with pytest.raises(RuntimeError, match="nothing to merge"):
        _validate_shards([])


def test_discover_shards_png_vs_ktx2() -> None:
    tree = [
        {"type": "file", "path": "polyhaven-1k.shard-0-of-2.tar"},
        {"type": "file", "path": "polyhaven-1k.shard-1-of-2.tar"},
        {"type": "file", "path": "ktx2/polyhaven-ktx2-1k.shard-0-of-2.tar"},
        {"type": "file", "path": "ktx2/polyhaven-ktx2-1k.shard-1-of-2.tar"},
        {"type": "file", "path": "polyhaven-1k.tar"},  # not a shard
        {"type": "directory", "path": "ktx2"},
    ]
    png = _discover_shards(tree, "polyhaven", "1k", is_ktx2=False)
    assert [p[2] for p in png] == [
        "polyhaven-1k.shard-0-of-2.tar",
        "polyhaven-1k.shard-1-of-2.tar",
    ]
    ktx2 = _discover_shards(tree, "polyhaven", "ktx2-1k", is_ktx2=True)
    assert [p[2] for p in ktx2] == [
        "ktx2/polyhaven-ktx2-1k.shard-0-of-2.tar",
        "ktx2/polyhaven-ktx2-1k.shard-1-of-2.tar",
    ]


def test_merge_catalogs_unions_by_id() -> None:
    a = [{"id": "m1", "source": "x"}, {"id": "m2", "source": "x"}]
    b = [{"id": "m3", "source": "x"}]
    merged = _merge_catalogs([a, b])
    assert [e["id"] for e in merged] == ["m1", "m2", "m3"]


def test_merge_catalogs_dedupes() -> None:
    a = [{"id": "m1", "source": "x", "v": 1}]
    b = [{"id": "m1", "source": "x", "v": 2}]  # same id
    merged = _merge_catalogs([a, b])
    assert len(merged) == 1
    # First-seen wins (a).
    assert merged[0]["v"] == 1


def test_merge_end_to_end_via_shard_derive(tmp_path: Path) -> None:
    """Drive two shard derives into local files, then invoke merge_shards
    with every HF call mocked out. Verifies the merged tar has every
    channel the unsharded derive would produce and the rowmap offsets
    point at valid PNG bytes."""
    src_tar, src_rowmap = _build_src(tmp_path, n_materials=8)
    src_bytes = src_tar.read_bytes()
    work = tmp_path / "work"
    K = 2

    for i in range(K):
        with (
            patch("mat_vis_baker.hf_derive._pin_commit", return_value="deadbeef"),
            patch("mat_vis_baker.hf_derive._fetch_rowmap", return_value=src_rowmap),
            patch("mat_vis_baker.hf_derive._range_read", side_effect=_mock_range(src_bytes)),
        ):
            derive_smaller_tier(
                source="polyhaven",
                target_tier="512",
                source_tier="1k",
                release_tag="v0.0.0-test",
                work_dir=work / f"shard-{i}",
                hf_token="t",
                dry_run=True,
                workers=1,
                shard=(i, K),
            )

    shard_bytes: dict[str, bytes] = {}
    shard_rowmaps: dict[str, dict] = {}
    for i in range(K):
        tar = work / f"shard-{i}" / f"polyhaven-512.shard-{i}-of-{K}.tar"
        rm = work / f"shard-{i}" / f"polyhaven-512.shard-{i}-of-{K}-rowmap.json"
        shard_bytes[f"polyhaven-512.shard-{i}-of-{K}.tar"] = tar.read_bytes()
        shard_rowmaps[f"polyhaven-512.shard-{i}-of-{K}-rowmap.json"] = json.loads(rm.read_text())

    fake_tree = [{"type": "file", "path": name} for name in list(shard_bytes) + list(shard_rowmaps)]

    def fake_fetch_json(url, token):
        for name, data in shard_rowmaps.items():
            if url.endswith(name):
                return data
        raise AssertionError(f"unexpected fetch: {url}")

    def fake_range_read(session, url, offset, length, token):
        for name, data in shard_bytes.items():
            if url.endswith(name):
                return data[offset : offset + length]
        raise AssertionError(f"unexpected range-read: {url}")

    pushed: dict = {}

    def fake_push_to_hf(*, repo_id, files, revision, commit_message, token, delete_paths=None):
        pushed["files"] = [(p.name, repo_path) for p, repo_path in files]
        pushed["delete_paths"] = list(delete_paths or [])
        return "mergedsha000"

    with (
        patch("mat_vis_baker.merge_shards._list_tree", return_value=fake_tree),
        patch("mat_vis_baker.merge_shards._fetch_json", side_effect=fake_fetch_json),
        patch("mat_vis_baker.merge_shards._range_read", side_effect=fake_range_read),
        patch("mat_vis_baker.merge_shards.push_to_hf", side_effect=fake_push_to_hf),
    ):
        result = merge_shards(
            source="polyhaven",
            tier="512",
            release_tag="v0.0.0-test",
            work_dir=work / "merge",
            hf_token="t",
        )

    assert result["shards"] == K
    assert result["channels"] == 8 * 3  # 8 mats × 3 channels
    # All shard artifacts marked for deletion.
    expected_deletes = set(shard_bytes) | set(shard_rowmaps)
    assert set(pushed["delete_paths"]) == expected_deletes
    # Merged tar + rowmap pushed.
    pushed_repo_paths = {p for _, p in pushed["files"]}
    assert "polyhaven-512.tar" in pushed_repo_paths
    assert "polyhaven-512-rowmap.json" in pushed_repo_paths

    merged_rowmap = json.loads((work / "merge" / "polyhaven-512-rowmap.json").read_text())
    assert set(merged_rowmap["materials"].keys()) == {f"mat_{i:03d}" for i in range(8)}
    # Every merged channel's offset/length points at a valid PNG header.
    merged_tar_bytes = (work / "merge" / "polyhaven-512.tar").read_bytes()
    for mid, channels in merged_rowmap["materials"].items():
        for ch, spec in channels.items():
            payload = merged_tar_bytes[spec["offset"] : spec["offset"] + spec["length"]]
            assert payload[:4] == b"\x89PNG", f"{mid}/{ch} has no PNG header"
