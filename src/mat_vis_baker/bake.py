"""Bake engine — validate flat PNGs, bake layered mtlx via MaterialX.

Most sources (ambientcg, polyhaven) ship pre-baked flat PNGs.
The bake stage validates them, generates thumbnails, and hashes.
gpuopen layered graphs require MaterialX baking (optional dependency).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

from mat_vis_baker.common import TIER_TO_PX, MaterialRecord, hash_textures

log = logging.getLogger("mat-vis-baker.bake")

THUMB_SIZE = 128


def _validate_and_resize_png(path: Path, target_px: int | None = None) -> bool:
    """Validate PNG. If target_px is smaller than source, resize in place."""
    try:
        with Image.open(path) as img:
            if img.format != "PNG":
                log.warning("%s: not PNG (got %s)", path, img.format)
                return False
            if target_px and (img.width > target_px or img.height > target_px):
                resized = img.copy()
                resized.thumbnail((target_px, target_px), Image.LANCZOS)
                resized.save(path, "PNG")
                log.debug(
                    "%s: resized %dx%d → %dx%d", path, img.width, img.height, target_px, target_px
                )
            return True
    except Exception:
        log.exception("%s: invalid image", path)
        return False


def _generate_thumbnail(src_path: Path, thumb_dir: Path, channel: str) -> Path:
    """Resize to 128x128 thumbnail. Returns path to thumbnail."""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / f"{channel}_thumb.png"
    with Image.open(src_path) as img:
        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        img.save(thumb_path, "PNG", optimize=True)
    return thumb_path


def _bake_mtlx(mtlx_path: Path, output_dir: Path, resolution_px: int) -> dict[str, Path]:
    """Bake a layered MaterialX graph to flat PNGs.

    Requires the [materialx] optional dependency.
    """
    try:
        import MaterialX as mx  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "MaterialX is required for baking layered gpuopen graphs. "
            "Install with: pip install mat-vis[materialx]"
        ) from exc

    # TODO: implement full MaterialX TextureBaker flow
    raise NotImplementedError(f"MaterialX baking not yet implemented: {mtlx_path}")


def bake_material(
    record: MaterialRecord,
    output_dir: Path,
    thumb_dir: Path | None = None,
    tier: str | None = None,
) -> MaterialRecord:
    """Validate or bake a single material's textures. Generate thumbnails + hashes."""
    if record.status == "failed":
        return record

    expected_px = TIER_TO_PX.get(tier) if tier else None

    if record.needs_mtlx_bake:
        mtlx_path = record.texture_paths.get("_mtlx")
        if not mtlx_path:
            log.error("%s: needs mtlx bake but no mtlx path", record.id)
            record.status = "failed"
            return record
        try:
            baked = _bake_mtlx(mtlx_path, output_dir / record.id, expected_px or 1024)
            record.texture_paths.update(baked)
            record.maps = sorted(baked.keys())
        except (ImportError, NotImplementedError):
            log.exception("%s: mtlx bake failed", record.id)
            record.status = "failed"
        return record

    # flat PNGs — validate each
    valid_maps: dict[str, Path] = {}
    for channel, path in record.texture_paths.items():
        if channel.startswith("_"):
            continue
        if _validate_and_resize_png(path, expected_px):
            valid_maps[channel] = path
        else:
            log.warning("%s/%s: invalid PNG, dropping channel", record.id, channel)

    if not valid_maps:
        log.error("%s: no valid textures after validation", record.id)
        record.status = "failed"
    else:
        record.texture_paths = {
            k: v for k, v in record.texture_paths.items() if k.startswith("_") or k in valid_maps
        }
        record.texture_paths.update(valid_maps)
        record.maps = sorted(valid_maps.keys())

        # Generate thumbnails
        if thumb_dir:
            mat_thumb_dir = thumb_dir / record.source / record.id
            for channel, path in valid_maps.items():
                try:
                    _generate_thumbnail(path, mat_thumb_dir, channel)
                except Exception:
                    log.warning("%s/%s: thumbnail generation failed", record.id, channel)

        # Hash textures for integrity verification
        hash_textures(record)

    return record


def bake_batch(
    records: list[MaterialRecord],
    output_dir: Path,
    tier: str | None = None,
    thumb_dir: Path | None = None,
) -> list[MaterialRecord]:
    """Validate/bake all records, catching per-material errors."""
    for rec in records:
        try:
            bake_material(rec, output_dir, thumb_dir, tier)
        except Exception:
            log.exception("%s: unexpected bake error", rec.id)
            rec.status = "failed"
    ok = sum(1 for r in records if r.status == "ok")
    log.info("bake: %d ok, %d failed / %d total", ok, len(records) - ok, len(records))
    return records
