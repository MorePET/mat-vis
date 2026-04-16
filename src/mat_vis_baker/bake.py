"""Bake engine — validate flat ONGs, bake layered mtlx via MaterialX.

Most sources (ambientcg, polyhaven) ship pre-baked flat ONGs.
The bake stage validates them. gpuopen layered graphs require
MaterialX baking (optional dependency).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

from mat_vis_baker.common import TIER_TO_PX, MaterialRecord

log = logging.getLogger("mat-vis-baker.bake")


def _validate_png(path: Path, expected_px: int | None = None) -> bool:
    """Open with Pillow, verify valid PNG. Optionally check dimensions."""
    try:
        with Image.open(path) as img:
            if img.format != "PNG":
                log.warning("%s: not PNG (got %s)", path, img.format)
                return False
            if expected_px and (img.width != expected_px or img.height != expected_px):
                log.warning(
                    "%s: expected %dx%d, got %dx%d",
                    path,
                    expected_px,
                    expected_px,
                    img.width,
                    img.height,
                )
                # non-square or wrong size is a warning, not a failure
            return True
    except Exception:
        log.exception("%s: invalid image", path)
        return False


def _bake_mtlx(mtlx_path: Path, output_dir: Path, resolution_px: int) -> dict[str, Path]:
    """Bake a layered MaterialX graph to flat ONGs.

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
    # For now, raise so gpuopen layered materials are skipped cleanly
    raise NotImplementedError(f"MaterialX baking not yet implemented: {mtlx_path}")


def bake_material(
    record: MaterialRecord, output_dir: Path, tier: str | None = None
) -> MaterialRecord:
    """Validate or bake a single material's textures."""
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

    # flat ONGs — validate each
    valid_maps: dict[str, Path] = {}
    for channel, path in record.texture_paths.items():
        if _validate_png(path, expected_px):
            valid_maps[channel] = path
        else:
            log.warning("%s/%s: invalid PNG, dropping channel", record.id, channel)

    if not valid_maps:
        log.error("%s: no valid textures after validation", record.id)
        record.status = "failed"
    else:
        record.texture_paths = valid_maps
        record.maps = sorted(valid_maps.keys())

    return record


def bake_batch(
    records: list[MaterialRecord], output_dir: Path, tier: str | None = None
) -> list[MaterialRecord]:
    """Validate/bake all records, catching per-material errors."""
    for rec in records:
        try:
            bake_material(rec, output_dir, tier)
        except Exception:
            log.exception("%s: unexpected bake error", rec.id)
            rec.status = "failed"
    ok = sum(1 for r in records if r.status == "ok")
    log.info("bake: %d ok, %d failed / %d total", ok, len(records) - ok, len(records))
    return records
