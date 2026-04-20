"""release-manifest.json — convenience snapshot only (no longer authoritative).

v2 shape (ADR-0007, docs/specs/release-manifest-schema-v2.json):

    {
      "schema_version": 2,
      "release_tag": "<tag>",
      "sources": {
        "<source>": {
          "catalog": "<source>.json",
          "materials_count": <N>,
          "tiers": {                         # optional (scalar sources omit)
            "<tier>": {
              "tar":    "<source>-<tier>.tar",
              "rowmap": "<source>-<tier>-rowmap.json"
            }
          }
        }
      }
    }

**Where the manifest lives now.** The baker no longer writes this file
during bake / derive — bake/derive runs only write their own tar +
rowmap (and, for initial bakes, the source's catalog). Everything
they write is a unique path, so concurrent runs never touch the same
file → no merge races possible.

Instead, clients build the manifest in memory from the dataset tree
listing (``build_manifest_from_tree`` below), and an optional one-shot
freeze step (see ``scripts/freeze_release.py``) can serialise that
in-memory manifest to disk at tag-cutting time as a convenience index
for non-HF-aware tools.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("mat-vis-baker.manifest")

MANIFEST_SCHEMA_VERSION = 2

_KTX2_PREFIX = "ktx2/"
# Top-level tar filename: "<source>-<tier>.tar". Source names never
# contain hyphens; tier is the rest up to ".tar".
_TOP_TAR = re.compile(r"^(?P<src>[a-z]+)-(?P<tier>[A-Za-z0-9-]+)\.tar$")
# ktx2/<source>-<target_tier>.tar — target_tier typically "ktx2-1k" etc.
_KTX2_TAR = re.compile(r"^(?P<src>[a-z]+)-(?P<tier>[A-Za-z0-9-]+)\.tar$")


def generate_manifest_v2(release_tag: str, sources: dict) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_tag": release_tag,
        "sources": sources,
    }


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursive merge. Scalars + lists in ``patch`` replace ``base``."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def build_manifest_from_tree(
    *,
    release_tag: str,
    tree_paths: list[str],
    materials_counts: dict[str, int] | None = None,
) -> dict:
    """Assemble a v2 manifest from a flat list of repo paths.

    ``tree_paths`` is the set of file paths in the dataset revision
    (e.g. ``["polyhaven.json", "polyhaven-1k.tar", ...]``). The only
    read-from-network this needs is the tree listing itself; the
    baker/client feeds it in.

    ``materials_counts`` is optional — if provided, stamps per-source
    counts onto the manifest. Most clients don't need this (they call
    ``client.index(source)`` which returns the full list), so leaving
    it ``None`` is fine.
    """
    materials_counts = materials_counts or {}
    sources: dict[str, dict] = {}

    for path in tree_paths:
        # Per-source catalog at the root: "<source>.json"
        if path.endswith(".json") and "-rowmap" not in path and "/" not in path:
            src = path[:-5]
            if src == "release-manifest":
                continue
            sources.setdefault(src, {"catalog": path, "tiers": {}})
            continue

        # Top-level tar: "<source>-<tier>.tar"
        if path.endswith(".tar") and "/" not in path:
            stem = path[:-4]
            m = _TOP_TAR.match(path)
            if not m:
                continue
            src = m.group("src")
            tier = m.group("tier")
            sources.setdefault(src, {"catalog": f"{src}.json", "tiers": {}})
            sources[src]["tiers"][tier] = {
                "tar": path,
                "rowmap": f"{stem}-rowmap.json",
            }
            continue

        # KTX2 tar: "ktx2/<source>-<target_tier>.tar"
        if path.startswith(_KTX2_PREFIX) and path.endswith(".tar"):
            rel = path[len(_KTX2_PREFIX) :]
            stem = rel[:-4]
            m = _KTX2_TAR.match(rel)
            if not m:
                continue
            src = m.group("src")
            tier = m.group("tier")  # e.g. "ktx2-1k"
            sources.setdefault(src, {"catalog": f"{src}.json", "tiers": {}})
            sources[src]["tiers"][tier] = {
                "tar": path,
                "rowmap": f"{_KTX2_PREFIX}{stem}-rowmap.json",
            }

    for src, entry in sources.items():
        if src in materials_counts:
            entry["materials_count"] = materials_counts[src]

    return generate_manifest_v2(release_tag=release_tag, sources=sources)


def write_manifest(manifest: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    log.info("wrote %s", output_path)
    return output_path


def _download_json(*, repo_id: str, revision: str, path: str, hf_token: str | None) -> dict | None:
    """Fetch a JSON file from HF. Returns None on 404."""
    import os

    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import (
        EntryNotFoundError,
        HfHubHTTPError,
        RevisionNotFoundError,
    )

    token = hf_token if hf_token is not None else os.environ.get("HF_TOKEN")
    try:
        local = hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=revision,
            filename=path,
            token=token,
        )
    except (EntryNotFoundError, RevisionNotFoundError):
        return None
    except HfHubHTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 404:
            return None
        raise
    return json.loads(Path(local).read_text())
