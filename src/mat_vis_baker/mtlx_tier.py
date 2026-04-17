"""Pack original upstream MaterialX files into a JSON map for release.

GPUOpen provides real layered shader graphs (procedural mixing, world-space
normals, etc.) that are unique per material and cannot be regenerated.
These are packed into a single JSON file per source:

    gpuopen-mtlx.json = {"material_id": "<xml>...", ...}

Flat generated MaterialX (UsdPreviewSurface wrappers) are NOT stored —
the client generates those on the fly via the adapters module since they're
just templates with substituted material IDs and channel lists.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("mat-vis-baker.mtlx-tier")


def pack_original_mtlx_json(
    mtlx_dir: Path,
    source: str,
    output_dir: Path,
) -> Path:
    """Pack original upstream .mtlx files into a single JSON map.

    Reads all .mtlx files from ``mtlx_dir/{source}/`` and produces
    a JSON file mapping material_id → XML string.

    Args:
        mtlx_dir: Root directory containing per-source subdirectories
                  (e.g. ``mtlx/gpuopen/MaterialName/material.mtlx``).
        source: Source name (e.g. ``gpuopen``).
        output_dir: Where to write the output JSON file.

    Returns:
        Path to the written JSON file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = mtlx_dir / source

    if not source_dir.is_dir():
        log.error("source directory does not exist: %s", source_dir)
        return output_dir / f"{source}-mtlx.json"

    mtlx_files = sorted(source_dir.rglob("*.mtlx"))
    if not mtlx_files:
        log.warning("no .mtlx files found in %s", source_dir)
        return output_dir / f"{source}-mtlx.json"

    log.info("packing %d .mtlx files from %s", len(mtlx_files), source_dir)

    materials: dict[str, str] = {}

    for mtlx_path in mtlx_files:
        # Derive material ID from parent dir (MaterialName/material.mtlx)
        # or from filename (MaterialName.mtlx)
        if mtlx_path.name == "material.mtlx":
            material_id = mtlx_path.parent.name
        else:
            material_id = mtlx_path.stem

        xml_text = mtlx_path.read_text(encoding="utf-8")
        materials[material_id] = xml_text

    out_path = output_dir / f"{source}-mtlx.json"
    out_path.write_text(json.dumps(materials, indent=2) + "\n")

    log.info(
        "wrote %s (%d materials, %.1f KB)",
        out_path,
        len(materials),
        out_path.stat().st_size / 1024,
    )
    return out_path
