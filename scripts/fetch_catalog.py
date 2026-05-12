#!/usr/bin/env python3
"""Fetch HF substrate catalogs and emit a trimmed JSON for the Pages site.

Reads the release manifest from a pinned HF dataset tag, downloads each
source catalog, trims every material entry to the UI-relevant fields
(dropping `upstream.raw` which is heavy and unused by the gallery), and
writes:

  docs/site/build/catalog.json       -- array of trimmed entries
  docs/site/build/catalog.meta.json  -- facet enums + provenance

The script is stdlib-only (`urllib` + `json` + `argparse`) so it has no
deps beyond CPython itself — keeps the build job lean.

Usage:
  python scripts/fetch_catalog.py
  python scripts/fetch_catalog.py --release-tag v2026.04.99-tst-full-369 \
      --repo-id gerchowl/mat-vis-tst
  MAT_VIS_DATASET=gerchowl/mat-vis-tst@v2026.04.99-tst-full-369 \
      python scripts/fetch_catalog.py

Resolution precedence for repo + tag (mirrors the client at
``clients/python/src/mat_vis_client/client.py``):

  1. ``--release-tag`` / ``--repo-id`` CLI args (explicit wins).
  2. ``MAT_VIS_DATASET=<repo>@<tag>`` combined env.
  3. ``MAT_VIS_HF_DATASET=<repo>`` + ``MAT_VIS_TAG=<tag>`` split env.
  4. Defaults: ``gerchowl/mat-vis`` @ ``DEFAULT_TAG``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

# Keep in lockstep with `clients/python/src/mat_vis_client/client.py`'s
# `DEFAULT_TAG`. Bump alongside that file when a new prod release is
# verified. This script doesn't import the client (we want zero deps),
# so the value lives here too.
DEFAULT_REPO = "gerchowl/mat-vis"
DEFAULT_TAG = "v2026.04.2"

HF_BASE = "https://huggingface.co/datasets/{repo}/resolve/{tag}/{path}"


def resolve_coords(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve (repo, tag) from CLI args + env, mirroring the client.

    Returns the effective coordinates for the fetch. Pure function —
    raises nothing; falls through to defaults when nothing else is set.
    """
    if args.repo_id and args.release_tag:
        return args.repo_id, args.release_tag

    combined = os.environ.get("MAT_VIS_DATASET", "")
    env_repo: str | None = None
    env_tag: str | None = None
    if combined and "@" in combined:
        env_repo, env_tag = combined.rsplit("@", 1)
        env_repo = env_repo or None
        env_tag = env_tag or None
    env_repo = env_repo or os.environ.get("MAT_VIS_HF_DATASET")
    env_tag = env_tag or os.environ.get("MAT_VIS_TAG")

    repo = args.repo_id or env_repo or DEFAULT_REPO
    tag = args.release_tag or env_tag or DEFAULT_TAG
    return repo, tag


def fetch_json(url: str) -> object:
    """GET ``url`` and parse as JSON. Follows redirects.

    HuggingFace serves a 307 redirect from the friendly resolve URL to
    a CDN-backed cache URL — ``urllib.request.urlopen`` follows that
    transparently. Keeping the helper thin lets us swap in a session-
    based fetcher (httpx, requests) later if we ever need parallelism.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "mat-vis-fetch_catalog/1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def trim_pbr(pbr: dict) -> dict:
    """Drop null PBR fields. Saves ~1KB/material at 5200 materials."""
    if not isinstance(pbr, dict):
        return {}
    return {k: v for k, v in pbr.items() if v is not None}


def trim_entry(raw: dict) -> dict | None:
    """Trim one catalog entry to the UI shape.

    Returns ``None`` for entries missing the required `id` / `source`
    keys — those are malformed and we'd rather drop them than ship a
    broken card. Logs a warning to stderr.
    """
    mv = raw.get("mat_vis") or {}
    src = raw.get("source")
    mid = raw.get("id")
    if not src or not mid:
        print(f"[fetch_catalog] skipping malformed entry: {raw!r}"[:200], file=sys.stderr)
        return None

    attribution = mv.get("attribution") or {}
    dates = mv.get("dates") or {}
    tiers = raw.get("available_tiers") or []

    return {
        "id": mid,
        "s": src,
        "n": mv.get("name") or mid,
        "c": mv.get("category"),
        "t": mv.get("tags") or [],
        "l": attribution.get("license_spdx"),
        "u": attribution.get("source_url"),
        "a": attribution.get("authors") or [],
        "p": trim_pbr(mv.get("pbr") or {}),
        "d": {
            "p": dates.get("published"),
            "u": dates.get("updated"),
        },
        "tiers": list(tiers),
        "thumb": "thumb" in tiers,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--release-tag", default=None, help="HF revision (default: env / DEFAULT_TAG)")
    ap.add_argument(
        "--repo-id", default=None, help="HF dataset repo (default: env / gerchowl/mat-vis)"
    )
    ap.add_argument(
        "--out-dir",
        default="docs/site/build",
        help="output directory for catalog.json + catalog.meta.json (default: docs/site/build)",
    )
    args = ap.parse_args()

    repo, tag = resolve_coords(args)
    print(f"[fetch_catalog] repo={repo} tag={tag}", file=sys.stderr)

    manifest_url = HF_BASE.format(repo=repo, tag=tag, path="release-manifest.json")
    print(f"[fetch_catalog] fetching manifest: {manifest_url}", file=sys.stderr)
    manifest = fetch_json(manifest_url)
    if not isinstance(manifest, dict):
        print("[fetch_catalog] manifest is not a JSON object", file=sys.stderr)
        return 2

    sources = manifest.get("sources") or {}
    if not sources:
        print("[fetch_catalog] manifest has no `sources` block", file=sys.stderr)
        return 2

    merged: list[dict] = []
    for src_name, src_info in sources.items():
        cat_path = src_info.get("catalog")
        if not cat_path:
            print(f"[fetch_catalog] skipping {src_name}: no catalog field", file=sys.stderr)
            continue
        cat_url = HF_BASE.format(repo=repo, tag=tag, path=cat_path)
        print(f"[fetch_catalog] fetching {src_name}: {cat_url}", file=sys.stderr)
        cat = fetch_json(cat_url)
        if not isinstance(cat, list):
            print(
                f"[fetch_catalog] {src_name} catalog is not a list, got {type(cat).__name__}",
                file=sys.stderr,
            )
            continue
        before = len(merged)
        for raw in cat:
            trimmed = trim_entry(raw)
            if trimmed is not None:
                merged.append(trimmed)
        print(f"[fetch_catalog]   {src_name}: {len(merged) - before} entries", file=sys.stderr)

    # Stable order: by source, then by name. The site re-sorts client-
    # side as needed but a stable on-wire order makes diffs readable.
    merged.sort(key=lambda e: (e["s"], e["n"].lower()))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = out_dir / "catalog.json"
    meta_path = out_dir / "catalog.meta.json"

    # Compact JSON (no whitespace) — every byte counts on a 5K-entry payload.
    catalog_path.write_text(json.dumps(merged, separators=(",", ":")), encoding="utf-8")

    # Facet enums for the UI's filter dropdowns. Sorted for stable order.
    src_set = sorted({e["s"] for e in merged})
    cat_set = sorted({e["c"] for e in merged if e["c"]})
    lic_set = sorted({e["l"] for e in merged if e["l"]})

    from datetime import datetime, timezone

    meta_doc = {
        "schema": 1,
        "release_tag": tag,
        "repo_id": repo,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": src_set,
        "categories": cat_set,
        "licenses": lic_set,
        "total": len(merged),
    }
    meta_path.write_text(json.dumps(meta_doc, indent=2), encoding="utf-8")

    # Also copy into `public/` so Astro's static handler serves them at
    # `${BASE_URL}catalog.json` directly. The detail page reads from
    # `build/` at build time (via load.ts); the client island reads
    # from `public/` at runtime (over HTTP).
    public_dir = Path("docs/site/public")
    if public_dir.exists():
        (public_dir / "catalog.json").write_text(
            json.dumps(merged, separators=(",", ":")), encoding="utf-8"
        )
        (public_dir / "catalog.meta.json").write_text(
            json.dumps(meta_doc, indent=2), encoding="utf-8"
        )

    size_kb = catalog_path.stat().st_size / 1024
    print(
        f"[fetch_catalog] wrote {len(merged)} entries to {catalog_path} ({size_kb:.1f} KB)",
        file=sys.stderr,
    )
    print(f"[fetch_catalog] wrote meta to {meta_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
