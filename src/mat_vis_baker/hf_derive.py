"""Derive smaller / transcoded tiers from an existing HF tar (ADR-0007).

Two pipelines live here — both atomic-commit to the same release:

- ``derive_smaller_tier(..., target_tier="512")`` — reads an existing
  PNG tar at a source tier (typically 1k), resizes each channel to
  the target resolution, writes a new ``<source>-<target>.tar`` +
  rowmap. Used to bake 128/256/512 without re-fetching upstream.
- ``derive_ktx2_tier(..., source_tier="1k")`` — same input tar,
  transcodes each PNG → KTX2 via the ``toktx`` binary, writes a
  ``ktx2/<source>-<source_tier>.tar``. Runner must have
  KTX-Software installed (``toktx`` on PATH).

Both follow the same merge/push pattern as ``hf_bake.bake_one``:
the catalog's ``available_tiers`` field is extended in place, the
manifest's ``sources[<src>].tiers`` gains a new key, everything
lands in one ``create_commit``.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from huggingface_hub import hf_hub_download
from PIL import Image

from mat_vis_baker.common import TIER_TO_PX
from mat_vis_baker.hf_push import push_to_hf
from mat_vis_baker.manifest import merge_remote_manifest
from mat_vis_baker.tar_writer import TarWriter

log = logging.getLogger("mat-vis-baker.hf_derive")

DEFAULT_REPO_ID = "gerchowl/mat-vis"


def _download_source_artifacts(
    *, repo_id: str, release_tag: str, source: str, source_tier: str, hf_token: str | None
) -> tuple[Path, dict]:
    """Return (tar path on disk, rowmap dict)."""
    tar_path = hf_hub_download(
        repo_id=repo_id,
        repo_type="dataset",
        revision=release_tag,
        filename=f"{source}-{source_tier}.tar",
        token=hf_token,
    )
    rowmap_path = hf_hub_download(
        repo_id=repo_id,
        repo_type="dataset",
        revision=release_tag,
        filename=f"{source}-{source_tier}-rowmap.json",
        token=hf_token,
    )
    return Path(tar_path), json.loads(Path(rowmap_path).read_text())


def _slice_channel(tar_bytes: bytes, spec: dict) -> bytes:
    lo = int(spec["offset"])
    length = int(spec["length"])
    return tar_bytes[lo : lo + length]


def _patch_catalog_tiers(
    catalog: list[dict], material_ids: set[str], added_tier: str
) -> list[dict]:
    """Add ``added_tier`` to ``available_tiers`` for every entry whose id
    is in ``material_ids``. Returns a new list; does not mutate input."""
    out = []
    for entry in catalog:
        new_entry = dict(entry)
        if new_entry.get("id") in material_ids:
            tiers = sorted(set(new_entry.get("available_tiers") or []) | {added_tier})
            new_entry["available_tiers"] = tiers
        out.append(new_entry)
    return out


def derive_smaller_tier(
    source: str,
    target_tier: str,
    source_tier: str,
    release_tag: str,
    work_dir: Path,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    hf_token: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Resize every channel from ``<source>-<source_tier>.tar`` to
    ``target_tier`` resolution; pack into a new tar; atomic-commit."""
    if target_tier not in TIER_TO_PX:
        raise ValueError(f"unknown target tier {target_tier!r}")
    if source_tier not in TIER_TO_PX:
        raise ValueError(f"unknown source tier {source_tier!r}")
    target_px = TIER_TO_PX[target_tier]
    if TIER_TO_PX[source_tier] < target_px:
        raise ValueError(
            f"source tier {source_tier!r} ({TIER_TO_PX[source_tier]}px) is smaller "
            f"than target {target_tier!r} ({target_px}px) — upscaling not supported"
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    out_tar_name = f"{source}-{target_tier}.tar"
    out_rowmap_name = f"{source}-{target_tier}-rowmap.json"
    out_tar_path = work_dir / out_tar_name
    out_rowmap_path = work_dir / out_rowmap_name
    catalog_path = work_dir / f"{source}.json"
    manifest_path = work_dir / "release-manifest.json"

    t0 = time.monotonic()
    log.info(
        "=== hf-derive %s %s → %s @ %s ===",
        source,
        source_tier,
        target_tier,
        release_tag,
    )

    src_tar_path, src_rowmap = _download_source_artifacts(
        repo_id=repo_id,
        release_tag=release_tag,
        source=source,
        source_tier=source_tier,
        hf_token=hf_token,
    )
    tar_bytes = src_tar_path.read_bytes()
    materials = src_rowmap.get("materials", {})
    log.info("loaded source: %d materials, tar=%.1f MB", len(materials), len(tar_bytes) / 1e6)

    n_ok = 0
    n_failed = 0
    with TarWriter(out_tar_path) as tw:
        for mid, channels in materials.items():
            for ch, spec in channels.items():
                try:
                    raw = _slice_channel(tar_bytes, spec)
                    img = Image.open(io.BytesIO(raw))
                    img.load()
                    resized = img.resize((target_px, target_px), Image.LANCZOS)
                    buf = io.BytesIO()
                    resized.save(buf, format="PNG", optimize=False)
                    tw.add_channel(mid, ch, buf.getvalue())
                    n_ok += 1
                except Exception as e:  # pragma: no cover
                    log.warning("%s/%s: resize failed: %s", mid, ch, e)
                    n_failed += 1
        new_materials = tw.finalize()

    if n_ok == 0:
        return {"error": "no channels resized", "ok": 0, "failed": n_failed}

    rowmap = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": target_tier,
        "tar_file": out_tar_name,
        "materials": new_materials,
    }
    out_rowmap_path.write_text(json.dumps(rowmap, indent=2) + "\n")

    # Patch catalog: add target_tier to `available_tiers` for every
    # material we actually produced bytes for.
    from mat_vis_baker.manifest import _download_json

    remote_catalog = (
        _download_json(
            repo_id=repo_id, revision=release_tag, path=f"{source}.json", hf_token=hf_token
        )
        or []
    )
    produced = set(new_materials.keys())
    patched_catalog = _patch_catalog_tiers(remote_catalog, produced, target_tier)
    catalog_path.write_text(json.dumps(patched_catalog, indent=2, ensure_ascii=False) + "\n")

    # Merge manifest: add the new tier under this source's tiers map.
    manifest = merge_remote_manifest(
        repo_id=repo_id,
        revision=release_tag,
        release_tag=release_tag,
        patch={
            "sources": {
                source: {
                    "catalog": f"{source}.json",
                    "materials_count": len(patched_catalog),
                    "tiers": {
                        target_tier: {"tar": out_tar_name, "rowmap": out_rowmap_name},
                    },
                }
            }
        },
        hf_token=hf_token,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    log.info(
        "PERF derive: %.1fs, %d ok / %d failed, tar=%.1f MB",
        time.monotonic() - t0,
        n_ok,
        n_failed,
        out_tar_path.stat().st_size / 1e6,
    )

    if dry_run:
        return {"dry_run": True, "ok": n_ok, "failed": n_failed}

    sha = push_to_hf(
        repo_id=repo_id,
        files=[
            (manifest_path, "release-manifest.json"),
            (catalog_path, f"{source}.json"),
            (out_tar_path, out_tar_name),
            (out_rowmap_path, out_rowmap_name),
        ],
        revision=release_tag,
        commit_message=f"feat(data): {release_tag} — derive {source} {target_tier} from {source_tier}",
        token=hf_token,
    )
    return {
        "commit": sha,
        "ok": n_ok,
        "failed": n_failed,
        "tar_bytes": out_tar_path.stat().st_size,
    }


def derive_ktx2_tier(
    source: str,
    source_tier: str,
    release_tag: str,
    work_dir: Path,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    hf_token: str | None = None,
    dry_run: bool = False,
    target_tier: str | None = None,
) -> dict:
    """Transcode every channel from a PNG tar → KTX2; pack into
    ``ktx2/<source>-<target_tier>.tar``; atomic-commit. ``target_tier``
    defaults to ``ktx2-<source_tier>``."""
    target_tier = target_tier or f"ktx2-{source_tier}"

    # Verify toktx is available.
    try:
        subprocess.run(["toktx", "--version"], capture_output=True, check=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise RuntimeError(
            "toktx not on PATH — install KTX-Software "
            "(https://github.com/KhronosGroup/KTX-Software/releases)"
        ) from e

    work_dir.mkdir(parents=True, exist_ok=True)
    out_subdir_name = "ktx2"
    out_tar_name = f"{source}-{target_tier}.tar"
    out_tar_in_repo = f"{out_subdir_name}/{out_tar_name}"
    out_rowmap_name = f"{source}-{target_tier}-rowmap.json"
    out_rowmap_in_repo = f"{out_subdir_name}/{out_rowmap_name}"
    (work_dir / out_subdir_name).mkdir(exist_ok=True)
    out_tar_path = work_dir / out_subdir_name / out_tar_name
    out_rowmap_path = work_dir / out_subdir_name / out_rowmap_name
    catalog_path = work_dir / f"{source}.json"
    manifest_path = work_dir / "release-manifest.json"

    t0 = time.monotonic()
    log.info(
        "=== hf-derive-ktx2 %s %s → %s @ %s ===",
        source,
        source_tier,
        target_tier,
        release_tag,
    )

    src_tar_path, src_rowmap = _download_source_artifacts(
        repo_id=repo_id,
        release_tag=release_tag,
        source=source,
        source_tier=source_tier,
        hf_token=hf_token,
    )
    tar_bytes = src_tar_path.read_bytes()
    materials = src_rowmap.get("materials", {})

    n_ok = 0
    n_failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with TarWriter(out_tar_path) as tw:
            for mid, channels in materials.items():
                for ch, spec in channels.items():
                    try:
                        raw = _slice_channel(tar_bytes, spec)
                        png_in = tmp_dir / "in.png"
                        ktx_out = tmp_dir / "out.ktx2"
                        png_in.write_bytes(raw)
                        if ktx_out.exists():
                            ktx_out.unlink()
                        # UASTC is the standard for lossy-but-good
                        # texture compression; --genmipmap for
                        # texture-sampling quality.
                        subprocess.run(
                            [
                                "toktx",
                                "--encode",
                                "uastc",
                                "--genmipmap",
                                "--t2",
                                str(ktx_out),
                                str(png_in),
                            ],
                            check=True,
                            capture_output=True,
                        )
                        tw.add_channel(mid, ch, ktx_out.read_bytes())
                        n_ok += 1
                    except Exception as e:  # pragma: no cover
                        log.warning("%s/%s: ktx2 transcode failed: %s", mid, ch, e)
                        n_failed += 1
            new_materials = tw.finalize()

    if n_ok == 0:
        return {"error": "no channels transcoded", "ok": 0, "failed": n_failed}

    rowmap = {
        "version": 1,
        "release_tag": release_tag,
        "source": source,
        "tier": target_tier,
        "tar_file": out_tar_in_repo,
        "materials": new_materials,
    }
    out_rowmap_path.write_text(json.dumps(rowmap, indent=2) + "\n")

    from mat_vis_baker.manifest import _download_json

    remote_catalog = (
        _download_json(
            repo_id=repo_id, revision=release_tag, path=f"{source}.json", hf_token=hf_token
        )
        or []
    )
    produced = set(new_materials.keys())
    patched_catalog = _patch_catalog_tiers(remote_catalog, produced, target_tier)
    catalog_path.write_text(json.dumps(patched_catalog, indent=2, ensure_ascii=False) + "\n")

    manifest = merge_remote_manifest(
        repo_id=repo_id,
        revision=release_tag,
        release_tag=release_tag,
        patch={
            "sources": {
                source: {
                    "catalog": f"{source}.json",
                    "materials_count": len(patched_catalog),
                    "tiers": {
                        target_tier: {
                            "tar": out_tar_in_repo,
                            "rowmap": out_rowmap_in_repo,
                        },
                    },
                }
            }
        },
        hf_token=hf_token,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    log.info(
        "PERF ktx2: %.1fs, %d ok / %d failed, tar=%.1f MB",
        time.monotonic() - t0,
        n_ok,
        n_failed,
        out_tar_path.stat().st_size / 1e6,
    )

    if dry_run:
        return {"dry_run": True, "ok": n_ok, "failed": n_failed}

    sha = push_to_hf(
        repo_id=repo_id,
        files=[
            (manifest_path, "release-manifest.json"),
            (catalog_path, f"{source}.json"),
            (out_tar_path, out_tar_in_repo),
            (out_rowmap_path, out_rowmap_in_repo),
        ],
        revision=release_tag,
        commit_message=f"feat(data): {release_tag} — derive {source} {target_tier} from {source_tier}",
        token=hf_token,
    )
    return {
        "commit": sha,
        "ok": n_ok,
        "failed": n_failed,
        "tar_bytes": out_tar_path.stat().st_size,
    }
