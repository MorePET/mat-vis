#!/usr/bin/env python3
"""mat-vis reference client — pure Python, zero dependencies.

Fetches PBR textures from mat-vis GitHub Releases via HTTP range reads.
Uses only urllib (stdlib). No pyarrow, no binary deps.

Usage as library:
    from mat_vis_client import MatVisClient
    client = MatVisClient()
    png_bytes = client.fetch_texture("ambientcg", "Rock064", "color", tier="1k")

Usage as CLI:
    python mat_vis_client.py list                              # list sources × tiers
    python mat_vis_client.py materials ambientcg 1k            # list materials
    python mat_vis_client.py fetch ambientcg Rock064 color 1k  # fetch PNG → stdout
    python mat_vis_client.py fetch ambientcg Rock064 color 1k -o rock.png
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = "MorePET/mat-vis"
GITHUB_RELEASES = f"https://github.com/{REPO}/releases"
LATEST_MANIFEST_URL = f"{GITHUB_RELEASES}/latest/download/release-manifest.json"
DEFAULT_CACHE_DIR = Path(os.environ.get("MAT_VIS_CACHE", Path.home() / ".cache" / "mat-vis"))
USER_AGENT = "mat-vis-client/0.1 (Python)"


def _get(url: str, headers: dict | None = None) -> bytes:
    """HTTP GET with User-Agent."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _get_json(url: str) -> dict | list:
    """Fetch and parse JSON."""
    return json.loads(_get(url))


class MatVisClient:
    """Lightweight client for mat-vis texture data."""

    def __init__(
        self,
        *,
        manifest_url: str | None = None,
        cache_dir: Path | None = None,
        tag: str | None = None,
    ):
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._manifest: dict | None = None
        self._rowmaps: dict[str, dict] = {}

        if manifest_url:
            self._manifest_url = manifest_url
        elif tag:
            self._manifest_url = f"{GITHUB_RELEASES}/download/{tag}/release-manifest.json"
        else:
            self._manifest_url = LATEST_MANIFEST_URL

    @property
    def manifest(self) -> dict:
        """Fetch and cache the release manifest."""
        if self._manifest is None:
            cache_path = self._cache_dir / ".manifest.json"
            if cache_path.exists():
                self._manifest = json.loads(cache_path.read_text())
            else:
                self._manifest = _get_json(self._manifest_url)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(self._manifest, indent=2))
        return self._manifest

    def sources(self, tier: str = "1k") -> list[str]:
        """List available sources for a tier."""
        tier_data = self.manifest.get("tiers", {}).get(tier, {})
        return list(tier_data.get("sources", {}).keys())

    def tiers(self) -> list[str]:
        """List available tiers."""
        return list(self.manifest.get("tiers", {}).keys())

    def rowmap(self, source: str, tier: str, category: str | None = None) -> dict:
        """Fetch and cache a rowmap."""
        key = f"{source}-{tier}-{category or 'all'}"
        if key not in self._rowmaps:
            tier_data = self.manifest["tiers"][tier]
            base_url = tier_data["base_url"]
            src_data = tier_data["sources"][source]

            # Find matching rowmap file
            rowmap_files = src_data.get("rowmap_files", [])
            if not rowmap_files:
                # Legacy single rowmap
                rowmap_file = src_data.get("rowmap_file", f"{source}-{tier}-rowmap.json")
                rowmap_files = [rowmap_file]

            if category:
                matches = [f for f in rowmap_files if category in f]
                rowmap_file = matches[0] if matches else rowmap_files[0]
            else:
                rowmap_file = rowmap_files[0]

            cache_path = self._cache_dir / ".rowmaps" / rowmap_file
            if cache_path.exists():
                self._rowmaps[key] = json.loads(cache_path.read_text())
            else:
                url = base_url + rowmap_file
                self._rowmaps[key] = _get_json(url)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(self._rowmaps[key], indent=2))

        return self._rowmaps[key]

    def materials(self, source: str, tier: str) -> list[str]:
        """List material IDs available for a source × tier."""
        rm = self.rowmap(source, tier)
        return sorted(rm.get("materials", {}).keys())

    def channels(self, source: str, material_id: str, tier: str) -> list[str]:
        """List channels available for a material."""
        rm = self.rowmap(source, tier)
        mat = rm.get("materials", {}).get(material_id, {})
        return sorted(mat.keys())

    def fetch_texture(
        self,
        source: str,
        material_id: str,
        channel: str,
        tier: str = "1k",
    ) -> bytes:
        """Fetch a single texture PNG via HTTP range read.

        Returns raw PNG bytes. Caches locally.
        """
        # Check cache first
        cache_path = self._cache_dir / source / tier / material_id / f"{channel}.png"
        if cache_path.exists():
            return cache_path.read_bytes()

        # Find in rowmap
        rm = self.rowmap(source, tier)
        mat = rm["materials"][material_id]
        rng = mat[channel]
        offset = rng["offset"]
        length = rng["length"]

        # Find parquet URL
        tier_data = self.manifest["tiers"][tier]
        base_url = tier_data["base_url"]
        parquet_file = rm["parquet_file"]
        url = base_url + parquet_file

        # HTTP range read
        range_header = f"bytes={offset}-{offset + length - 1}"
        data = _get(url, headers={"Range": range_header})

        # Verify PNG
        if data[:4] != b"\x89PNG":
            raise ValueError(f"Expected PNG, got {data[:4]!r}")

        # Cache
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)

        return data


# ── CLI ─────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(prog="mat-vis-client", description="mat-vis texture client")
    parser.add_argument("--tag", help="Release tag (default: latest)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List sources × tiers")

    p_mat = sub.add_parser("materials", help="List materials for a source × tier")
    p_mat.add_argument("source")
    p_mat.add_argument("tier", nargs="?", default="1k")

    p_fetch = sub.add_parser("fetch", help="Fetch a texture PNG")
    p_fetch.add_argument("source")
    p_fetch.add_argument("material")
    p_fetch.add_argument("channel")
    p_fetch.add_argument("tier", nargs="?", default="1k")
    p_fetch.add_argument("-o", "--output", help="Output file (default: stdout)")

    args = parser.parse_args()
    client = MatVisClient(tag=args.tag)

    if args.cmd == "list":
        for tier in client.tiers():
            sources = client.sources(tier)
            print(f"{tier}: {', '.join(sources)}")

    elif args.cmd == "materials":
        for mid in client.materials(args.source, args.tier):
            print(mid)

    elif args.cmd == "fetch":
        data = client.fetch_texture(args.source, args.material, args.channel, args.tier)
        if args.output:
            Path(args.output).write_bytes(data)
            print(f"Wrote {args.output} ({len(data):,} bytes)", file=sys.stderr)
        else:
            sys.stdout.buffer.write(data)


if __name__ == "__main__":
    main()
