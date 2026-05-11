"""CI helper: assert no baked thumb matches a known-failure fingerprint.

Usage:
    uv run python bake/preview/utils/check_thumbs.py <thumbs_dir>
    # → exits 0 if all clean
    # → exits 1 with a list of failing thumbs otherwise

A baked thumb that pixel-matches one of the blank fingerprints means
the bake silently produced a default-shaded sphere instead of the
material's actual PBR look — i.e. a bake regression that wouldn't
surface from per-file sanity checks alone.

Comparison uses mean per-pixel L2 distance over the 256x256 grid;
threshold is conservative (≤2 in 0-255 scale ≈ visually identical)
because SwiftShader is byte-deterministic across runs.

Two fingerprints checked (see bake/preview/utils/bake_blanks.py):

- blank_default.png       — Three.js MeshPhysicalMaterial defaults
                            (white, metalness=0, roughness=1). Catches:
                            renderer ran with empty material spec.
- blank_default_grey.png  — pymat _PBR_DEFAULTS leak fingerprint
                            (#CCCCCC, m=0, r=0.5). Catches: substrate
                            catalog was stale and pymat's render floor
                            leaked through (mat-vis#285 / mat-vis#376
                            regression signal).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[3]
ASSETS = REPO_ROOT / "bake" / "preview" / "assets"
FINGERPRINTS = ["blank_default.png", "blank_default_grey.png"]
DISTANCE_THRESHOLD = 2.0  # mean per-pixel L2 in 0-255 scale


def _load_rgb(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)


def _mean_pixel_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Mean L2 distance per pixel over the channel axis. Robust to
    minor compression artifacts; sensitive to material-content changes."""
    if a.shape != b.shape:
        return float("inf")
    diff = a - b
    return float(np.sqrt((diff * diff).sum(axis=-1)).mean())


def check(thumbs_dir: Path) -> int:
    fps: dict[str, np.ndarray] = {}
    for name in FINGERPRINTS:
        p = ASSETS / name
        if not p.exists():
            print(f"FATAL: missing fingerprint {p.relative_to(REPO_ROOT)}")
            print("  run `uv run python bake/preview/utils/bake_blanks.py` to regenerate")
            return 2
        fps[name] = _load_rgb(p)

    matches: list[tuple[Path, str, float]] = []
    checked = 0
    for thumb in sorted(thumbs_dir.rglob("*.png")):
        # Don't check the fingerprints against themselves
        if thumb.parent == ASSETS and thumb.name in FINGERPRINTS:
            continue
        thumb_arr = _load_rgb(thumb)
        checked += 1
        for fp_name, fp_arr in fps.items():
            d = _mean_pixel_distance(thumb_arr, fp_arr)
            if d <= DISTANCE_THRESHOLD:
                matches.append((thumb, fp_name, d))

    if not matches:
        print(f"OK — {checked} thumbs checked, none match fingerprints")
        return 0

    print(f"FAIL — {len(matches)} thumb(s) match a known-failure fingerprint:")
    for thumb, fp_name, d in matches:
        rel = thumb.relative_to(thumbs_dir.parent if thumbs_dir.parent.exists() else thumbs_dir)
        print(f"  {rel}  ←  {fp_name}  (distance={d:.2f})")
    return 1


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    thumbs_dir = Path(sys.argv[1]).resolve()
    if not thumbs_dir.is_dir():
        print(f"FATAL: {thumbs_dir} is not a directory", file=sys.stderr)
        return 2
    return check(thumbs_dir)


if __name__ == "__main__":
    sys.exit(main())
