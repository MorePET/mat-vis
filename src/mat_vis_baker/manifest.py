"""release-manifest.json generator.

v2 shape (ADR-0007 / schema docs/specs/release-manifest-schema-v2.json):

    {
      "schema_version": 2,
      "release_tag": "<tag>",
      "sources": {
        "<source>": {
          "catalog": "<source>.json",
          "materials_count": <N>,
          "tiers": {                          # optional (scalar sources omit)
            "<tier>": {
              "tar":    "<source>-<tier>.tar",
              "rowmap": "<source>-<tier>-rowmap.json"
            }
          }
        }
      }
    }

The baker writes one `(source, tier)` at a time, so the merge helper
reads the existing manifest from the HF revision (if any) and deep-
merges the new patch before re-pushing — this is the mechanism that
keeps per-batch bakes from clobbering earlier bakes' entries (which
was the substrate-level root of #99).
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

log = logging.getLogger("mat-vis-baker.manifest")

MANIFEST_SCHEMA_VERSION = 2


def generate_manifest_v2(release_tag: str, sources: dict) -> dict:
    """Build a manifest from an already-assembled ``sources`` dict.

    ``sources`` maps source name to the source-entry shape documented
    in the schema. Does no I/O — use ``merge_remote_manifest`` if you
    need to incorporate pre-existing entries from an HF revision.
    """
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_tag": release_tag,
        "sources": sources,
    }


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge ``patch`` into ``base``. Dict-valued keys merge;
    scalar and list values in ``patch`` replace ``base``. Returns a new
    dict; does not mutate inputs."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def merge_remote_manifest(
    *,
    repo_id: str,
    revision: str,
    release_tag: str,
    patch: dict,
    hf_token: str | None = None,
) -> dict:
    """Fetch the existing manifest from an HF revision and merge ``patch``.

    Returns the full merged manifest ready to re-push. If the revision
    does not exist yet (first bake for this release), returns a fresh
    v2 manifest seeded from ``patch``.
    """
    remote = _download_json(
        repo_id=repo_id, revision=revision, path="release-manifest.json", hf_token=hf_token
    )
    if remote is None:
        base = generate_manifest_v2(release_tag=release_tag, sources={})
    else:
        base = remote
        base["schema_version"] = MANIFEST_SCHEMA_VERSION
        base["release_tag"] = release_tag

    return _deep_merge(base, patch)


def _download_json(*, repo_id: str, revision: str, path: str, hf_token: str | None) -> dict | None:
    """Fetch a JSON file from HF. Returns None if the file/revision doesn't exist."""
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
        if status in (404,):
            return None
        raise

    return json.loads(Path(local).read_text())


def write_manifest(manifest: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    log.info("wrote %s", output_path)
    return output_path
