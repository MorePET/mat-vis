#!/usr/bin/env python
"""Phase 3 proof: HF substrate is client-readable end-to-end.

Pure-Python, zero-deps-except-PIL verification. Stands in for the
full client refactor — it exercises the exact read path a v0.6.0
client will follow:

    1. Fetch release-manifest.json (schema_version=2)
    2. For each (source, tier) present, fetch the rowmap
    3. Pick a material × channel, range-read [offset, offset+length)
       from the tar, verify PNG magic + decode via PIL.

Run against the scoped Phase 3 proof revision on HF:

    .venv/bin/python scripts/proof_hf_fetch.py v2026.04.1-rc1

Exits nonzero on any mismatch.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request

from PIL import Image

REPO = "gerchowl/mat-vis"
BASE = f"https://huggingface.co/datasets/{REPO}/resolve"


def _get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main(revision: str) -> int:
    manifest = json.loads(_get(f"{BASE}/{revision}/release-manifest.json"))
    assert manifest["schema_version"] == 2, (
        f"expected schema_version=2, got {manifest['schema_version']}"
    )
    assert manifest["release_tag"] == revision, (
        f"release_tag mismatch: {manifest['release_tag']} vs {revision}"
    )
    print(f"manifest ok: {len(manifest['sources'])} sources")

    any_tar_checked = False
    for source, entry in sorted(manifest["sources"].items()):
        catalog = json.loads(_get(f"{BASE}/{revision}/{entry['catalog']}"))
        assert len(catalog) == entry["materials_count"], (
            f"{source}: catalog size {len(catalog)} != manifest "
            f"materials_count {entry['materials_count']}"
        )
        print(f"  {source}: catalog {len(catalog)} entries ✓")

        for tier, tier_info in (entry.get("tiers") or {}).items():
            rowmap = json.loads(_get(f"{BASE}/{revision}/{tier_info['rowmap']}"))
            mats = rowmap["materials"]
            assert mats, f"{source}/{tier}: rowmap has no materials"

            mid, channels = next(iter(mats.items()))
            ch, spec = next(iter(channels.items()))
            lo = spec["offset"]
            hi = lo + spec["length"] - 1

            png_bytes = _get(
                f"{BASE}/{revision}/{tier_info['tar']}",
                headers={"Range": f"bytes={lo}-{hi}"},
            )
            assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", f"{source}/{tier}/{mid}/{ch}: not a PNG"
            assert len(png_bytes) == spec["length"], (
                f"{source}/{tier}/{mid}/{ch}: range returned {len(png_bytes)} "
                f"bytes, rowmap said {spec['length']}"
            )
            img = Image.open(io.BytesIO(png_bytes))
            img.verify()
            print(
                f"  {source}/{tier}: {mid}/{ch} → {img.size[0]}x{img.size[1]} PNG "
                f"({spec['length'] / 1e6:.1f} MB) ✓"
            )
            any_tar_checked = True

    if not any_tar_checked:
        print("WARNING: no tar tiers in manifest — only scalar sources verified")
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: proof_hf_fetch.py <revision>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
