#!/usr/bin/env python
"""Probe every upstream source's category + tag vocabulary.

Writes `docs/sources/metadata-vocabulary.json` — the machine-readable
sidecar to `docs/sources/metadata-vocabulary.md`. Re-run whenever
upstream schemas shift or the normalizer map expands so the committed
vocabulary record stays in sync with what the baker actually sees.

Usage::

    uv run python scripts/probe-metadata-vocab.py
    # → rewrites docs/sources/metadata-vocabulary.json

No arguments. Needs ambient network access to:
  - https://ambientcg.com/api/v2/full_json
  - https://api.polyhaven.com/assets
  - https://api.matlib.gpuopen.com/api/{materials,categories,tags,packages}
  - https://api.physicallybased.info/materials

Takes ~60s total (gpuopen paginates).
"""

from __future__ import annotations

import collections
import json
import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "sources" / "metadata-vocabulary.json"


def _str(x: object) -> str:
    """Coerce to a stable string. Lists collapse to first element so
    they're hashable for Counter; nested vocabulary is flattened
    earlier by the caller."""
    if isinstance(x, list):
        return x[0] if x else ""
    return x or "" if isinstance(x, str) else ""


def _enum(records, cat_fn, tag_fn) -> tuple[collections.Counter, collections.Counter]:
    cats: collections.Counter = collections.Counter()
    tags: collections.Counter = collections.Counter()
    for r in records:
        c = cat_fn(r)
        if isinstance(c, list):
            for x in c:
                if x:
                    cats[x] += 1
        elif c:
            cats[c] += 1
        for t in tag_fn(r) or []:
            if t:
                tags[t] += 1
    return cats, tags


def main() -> int:
    from mat_vis_baker.sources import ambientcg, gpuopen, polyhaven

    print("probing ambientcg...", file=sys.stderr)
    acg = ambientcg.discover()
    acg_c, acg_t = _enum(
        acg,
        lambda r: _str(r.get("displayCategory")),
        lambda r: r.get("tags") or [],
    )

    print("probing polyhaven...", file=sys.stderr)
    ph = list(polyhaven.discover().values())
    ph_c, ph_t = _enum(
        ph,
        lambda r: r.get("categories") or [],
        lambda r: r.get("tags") or [],
    )

    print("probing gpuopen...", file=sys.stderr)
    go = gpuopen.discover()
    go_c, go_t = _enum(
        go,
        lambda r: _str(r.get("_category_title")),
        lambda r: r.get("_tag_titles") or [],
    )

    print("probing physicallybased...", file=sys.stderr)
    pb = requests.get("https://api.physicallybased.info/materials", timeout=30).json()
    pb_c, pb_t = _enum(
        pb,
        lambda r: _str(r.get("category")),
        lambda r: r.get("tags") or [],
    )

    for c in (acg_c, ph_c, go_c, pb_c):
        c.pop("", None)

    out = {
        "meta": {
            "probe_date": date.today().isoformat(),
            "description": (
                "Raw metadata vocabulary observed from each upstream source's "
                "discovery API. Input to the baker's normalize_category / "
                "normalize_tags mapping (src/mat_vis_baker/common.py), populating "
                "mat_vis.category per ADR-0011."
            ),
            "source_counts": {
                "ambientcg": len(acg),
                "polyhaven": len(ph),
                "gpuopen": len(go),
                "physicallybased": len(pb),
            },
        },
        "ambientcg": {
            "categories": dict(sorted(acg_c.items(), key=lambda x: -x[1])),
            "tags_top100": dict(acg_t.most_common(100)),
        },
        "polyhaven": {
            "categories": dict(sorted(ph_c.items(), key=lambda x: -x[1])),
            "tags_top100": dict(ph_t.most_common(100)),
        },
        "gpuopen": {
            "categories": dict(sorted(go_c.items(), key=lambda x: -x[1])),
            "tags_top100": dict(go_t.most_common(100)),
        },
        "physicallybased": {
            "categories": dict(sorted(pb_c.items(), key=lambda x: -x[1])),
            "tags_top100": dict(pb_t.most_common(100)),
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
