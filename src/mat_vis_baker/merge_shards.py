"""Merge shard artifacts back into one unsharded tar + rowmap (#134).

Every shard produced one ``<source>-<tier>.shard-N-of-K.tar`` +
one ``<source>-<tier>.shard-N-of-K-rowmap.json`` (and for bake
shards: one partial catalog ``…shard-N-of-K.catalog.json``). This
module reassembles them:

1. List the dataset tree at ``release_tag`` and discover all shard
   artifacts for ``(source, tier)``.
2. Validate every ``shard_total`` agrees and every ``shard_index``
   in ``[0, K)`` is present (no gaps, no duplicates).
3. For each shard, HTTP-range-read every channel from its tar URL
   (using the shard rowmap's offset/length) and add it to a fresh
   ``TarWriter``. Offsets are recomputed from the shard rowmap —
   the shard tar bytes themselves aren't re-parsed.
4. For bake-shard partial catalogs (when present), union by
   material id into the final ``<source>.json``.
5. One atomic HF commit carries the merged tar + rowmap
   (+ catalog) and deletes the shard artifacts in the same commit.

Derive and bake outputs live in different paths inside the repo —
KTX2 derives nest under ``ktx2/``; PNG derives and bakes are flat.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import requests

from mat_vis_baker.hf_push import push_to_hf
from mat_vis_baker.tar_writer import TarWriter

log = logging.getLogger("mat-vis-baker.merge_shards")

HF_RESOLVE = "https://huggingface.co/datasets"
DEFAULT_REPO_ID = "gerchowl/mat-vis"


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _list_tree(repo_id: str, revision: str, token: str | None) -> list[dict]:
    """List the dataset tree (recursive) for ``revision``."""
    url = f"https://huggingface.co/api/datasets/{repo_id}/tree/{revision}?recursive=true"
    r = requests.get(url, headers=_auth_headers(token), timeout=60)
    r.raise_for_status()
    return r.json()


def _shard_name_regex(source: str, tier: str, is_ktx2: bool) -> re.Pattern[str]:
    """Match ``[ktx2/]<source>-<tier>.shard-N-of-K.tar`` with N, K as groups."""
    prefix = r"ktx2/" if is_ktx2 else ""
    return re.compile(
        rf"^{prefix}{re.escape(source)}-{re.escape(tier)}"
        r"\.shard-(\d+)-of-(\d+)\.tar$"
    )


def _fetch_json(url: str, token: str | None) -> dict:
    r = requests.get(url, headers=_auth_headers(token), timeout=60)
    r.raise_for_status()
    return r.json()


def _range_read(
    session: requests.Session, url: str, offset: int, length: int, token: str | None
) -> bytes:
    headers = {"Range": f"bytes={offset}-{offset + length - 1}", **_auth_headers(token)}
    r = session.get(url, headers=headers, timeout=120)
    r.raise_for_status()
    if len(r.content) != length:
        raise RuntimeError(f"range read short: asked {length} bytes, got {len(r.content)} @ {url}")
    return r.content


def _discover_shards(
    tree: list[dict], source: str, tier: str, is_ktx2: bool
) -> list[tuple[int, int, str]]:
    """Return sorted list of ``(shard_index, shard_total, path)``."""
    pat = _shard_name_regex(source, tier, is_ktx2)
    hits: list[tuple[int, int, str]] = []
    for entry in tree:
        if entry.get("type") != "file":
            continue
        m = pat.match(entry["path"])
        if m:
            hits.append((int(m.group(1)), int(m.group(2)), entry["path"]))
    hits.sort()
    return hits


def _validate_shards(shards: list[tuple[int, int, str]]) -> int:
    """Return shard_total if the shard set is complete, else raise."""
    if not shards:
        raise RuntimeError("no shard tars found — nothing to merge")
    totals = {k for _, k, _ in shards}
    if len(totals) != 1:
        raise RuntimeError(f"inconsistent shard_total values: {sorted(totals)}")
    total = totals.pop()
    indices = sorted(i for i, _, _ in shards)
    expected = list(range(total))
    if indices != expected:
        missing = set(expected) - set(indices)
        extra = set(indices) - set(expected)
        raise RuntimeError(
            f"shard set incomplete: expected {expected}, got {indices}. "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    return total


def _merge_catalogs(partial_catalogs: list[list[dict]]) -> list[dict]:
    """Union partial catalogs by ``id`` — shard-A's entry wins if shard-B
    happens to also emit it (shouldn't happen with material-level
    sharding, but we guard anyway)."""
    by_id: dict[str, dict] = {}
    for entries in partial_catalogs:
        for e in entries:
            by_id.setdefault(e["id"], e)
    return sorted(by_id.values(), key=lambda e: e["id"])


def merge_shards(
    source: str,
    tier: str,
    release_tag: str,
    work_dir: Path,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    hf_token: str | None = None,
    dry_run: bool = False,
    keep_shards: bool = False,
) -> dict:
    """Reassemble shard artifacts for ``(source, tier)`` into one tar.

    ``tier`` may be a PNG tier (``"512"``, ``"1k"``) or a KTX2 tier
    name (``"ktx2-1k"``). The KTX2 path lives under ``ktx2/`` in the
    repo, which is inferred from the tier prefix.
    """
    t0 = time.monotonic()
    is_ktx2 = tier.startswith("ktx2-")
    subdir = "ktx2/" if is_ktx2 else ""

    work_dir.mkdir(parents=True, exist_ok=True)
    out_tar_name = f"{source}-{tier}.tar"
    out_rowmap_name = f"{source}-{tier}-rowmap.json"
    out_tar_in_repo = f"{subdir}{out_tar_name}"
    out_rowmap_in_repo = f"{subdir}{out_rowmap_name}"
    # Merged files must be staged locally under the same relative path
    # that they land in the repo — HF push takes (local, repo_path) pairs.
    out_local_parent = work_dir / subdir.rstrip("/") if is_ktx2 else work_dir
    out_local_parent.mkdir(parents=True, exist_ok=True)
    out_tar_path = out_local_parent / out_tar_name
    out_rowmap_path = out_local_parent / out_rowmap_name

    log.info(
        "=== merge-shards %s %s @ %s (ktx2=%s) ===",
        source,
        tier,
        release_tag,
        is_ktx2,
    )

    tree = _list_tree(repo_id, release_tag, hf_token)
    shards = _discover_shards(tree, source, tier, is_ktx2)
    shard_total = _validate_shards(shards)
    log.info("found %d shards (complete set)", shard_total)

    session = requests.Session()
    resolve_base = f"{HF_RESOLVE}/{repo_id}/resolve/{release_tag}"
    partial_catalog_data: list[list[dict]] = []
    shard_artifacts_to_delete: list[str] = []
    total_channels_seen = 0

    with TarWriter(out_tar_path) as tw:
        for idx, total, tar_repo_path in shards:
            # Rowmap lives at the same path with `.tar` swapped for
            # `-rowmap.json`. rsplit guards against any future naming
            # scheme that happens to contain `.tar` mid-path.
            rowmap_repo_path = tar_repo_path.rsplit(".tar", 1)[0] + "-rowmap.json"
            tar_url = f"{resolve_base}/{tar_repo_path}"
            rowmap_url = f"{resolve_base}/{rowmap_repo_path}"
            rowmap = _fetch_json(rowmap_url, hf_token)
            materials = rowmap.get("materials", {})
            n_channels = sum(len(v) for v in materials.values())
            log.info(
                "shard %d/%d: %d materials, %d channels",
                idx,
                total,
                len(materials),
                n_channels,
            )
            for mid, channels in materials.items():
                for ch, spec in channels.items():
                    data = _range_read(
                        session, tar_url, int(spec["offset"]), int(spec["length"]), hf_token
                    )
                    tw.add_channel(mid, ch, data)
                    total_channels_seen += 1

            shard_artifacts_to_delete.append(tar_repo_path)
            shard_artifacts_to_delete.append(rowmap_repo_path)

            # Partial catalog (bake shards only — derive shards don't write one).
            partial_catalog_repo_path = f"{source}-{tier}.shard-{idx}-of-{total}.catalog.json"
            if any(e.get("path") == partial_catalog_repo_path for e in tree):
                cat = _fetch_json(f"{resolve_base}/{partial_catalog_repo_path}", hf_token)
                if isinstance(cat, list):
                    partial_catalog_data.append(cat)
                shard_artifacts_to_delete.append(partial_catalog_repo_path)

        merged_materials = tw.finalize()

    rowmap = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": tier,
        "tar_file": out_tar_in_repo,
        "materials": merged_materials,
    }
    out_rowmap_path.write_text(json.dumps(rowmap, indent=2) + "\n")

    files_to_push: list[tuple[Path, str]] = [
        (out_tar_path, out_tar_in_repo),
        (out_rowmap_path, out_rowmap_in_repo),
    ]
    catalog_repo_path: str | None = None
    if partial_catalog_data:
        merged_catalog = _merge_catalogs(partial_catalog_data)
        catalog_name = f"{source}.json"
        catalog_path = work_dir / catalog_name
        catalog_path.write_text(json.dumps(merged_catalog, indent=2, ensure_ascii=False) + "\n")
        files_to_push.append((catalog_path, catalog_name))
        catalog_repo_path = catalog_name
        log.info("merged catalog: %d unique materials", len(merged_catalog))

    delete_paths: list[str] = [] if keep_shards else shard_artifacts_to_delete

    log.info(
        "PERF merge-shards: %.1fs, %d channels merged, tar=%.1f MB, deleting %d shard artifacts",
        time.monotonic() - t0,
        total_channels_seen,
        out_tar_path.stat().st_size / 1e6,
        len(delete_paths),
    )

    if dry_run:
        log.info(
            "dry-run: would push %d files, delete %d shard artifacts",
            len(files_to_push),
            len(delete_paths),
        )
        return {
            "dry_run": True,
            "channels": total_channels_seen,
            "tar_bytes": out_tar_path.stat().st_size,
            "shards": shard_total,
        }

    sha = push_to_hf(
        repo_id=repo_id,
        files=files_to_push,
        revision=release_tag,
        commit_message=(
            f"feat(data): {release_tag} — merge {shard_total} shards → {source} {tier}"
        ),
        token=hf_token,
        delete_paths=delete_paths,
    )
    return {
        "commit": sha,
        "channels": total_channels_seen,
        "tar_bytes": out_tar_path.stat().st_size,
        "shards": shard_total,
        "catalog": catalog_repo_path,
    }
