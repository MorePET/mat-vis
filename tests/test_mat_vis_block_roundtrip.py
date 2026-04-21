"""End-to-end ADR-0011 round-trip validation (#178).

Confirms the two-layer index record contract survives the full write
path (baker → atomic HF commit → catalog JSON) against the scratch
dataset ``gerchowl/mat-vis-tst`` at ``v0.0.1-smoke``.

The MatVisClient today has ``gerchowl/mat-vis`` hardcoded (see
``clients/python/src/mat_vis_client/client.py:47``), so the client
read path doesn't yet accept a test-repo override. This test reads
the catalog JSON via raw HTTP instead — still a true round-trip
(baker wrote → HF stored → test reads), just without the client's
manifest/tree plumbing layered on top. Once a ``repo=`` knob lands
on the client, the same assertions move into a client-level test.

Skipped by default; set ``HF_INTEGRATION=1`` to enable.
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HF_INTEGRATION"),
    reason="requires HF_INTEGRATION=1; hits real Hugging Face",
)

REPO = "gerchowl/mat-vis-tst"
# Dedicated revision for ADR-0011-shape records. v0.0.1-smoke predates
# the mat_vis block and cannot be overwritten in place because hf-bake
# writes catalogs once (skip-if-remote-exists, ADR-0008 race-benign).
TAG = "v0.0.2-smoke-adr0011"
RESOLVE_BASE = f"https://huggingface.co/datasets/{REPO}/resolve/{TAG}"
TREE_URL = f"https://huggingface.co/api/datasets/{REPO}/tree/{TAG}?recursive=true"

REQUIRED_MAT_VIS_KEYS = {
    "name",
    "category",
    "tags",
    "description",
    "physical",
    "pbr",
    "attribution",
    "dates",
    "upstream_id",
}


def _fetch_json(url: str) -> object:
    # HF ``resolve/`` URLs 307-redirect to a CDN host; urllib's default
    # opener follows HTTP redirects but strips Authorization headers
    # across hosts. We keep Authorization only on the first hop (where
    # the server may check it) and let the CDN serve the body anon.
    req = urllib.request.Request(url)
    token = os.environ.get("HF_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body)


def _list_catalogs() -> list[str]:
    """Per-source catalog paths at REPO@TAG (e.g. ``polyhaven.json``)."""
    tree = _fetch_json(TREE_URL)
    assert isinstance(tree, list)
    return sorted(
        e["path"]
        for e in tree
        if e.get("type") == "file"
        and e["path"].endswith(".json")
        and not e["path"].startswith((".", "release-manifest"))
        and "-rowmap" not in e["path"]
    )


@pytest.fixture(scope="module")
def catalogs() -> dict[str, list[dict]]:
    """Map source → list of records, fetched once per test module."""
    result = {}
    for cat_path in _list_catalogs():
        source = cat_path.replace(".json", "")
        records = _fetch_json(f"{RESOLVE_BASE}/{cat_path}")
        assert isinstance(records, list)
        result[source] = records
    assert result, f"no per-source catalogs on {REPO}@{TAG}"
    return result


# ── Layer 1 invariants (mat_vis block) ───────────────────────────


def test_every_record_has_mat_vis_block(catalogs):
    for source, records in catalogs.items():
        assert records, f"{source} catalog is empty"
        for r in records:
            assert "mat_vis" in r, f"missing mat_vis in {source}/{r.get('id')}"
            assert set(r["mat_vis"].keys()) >= REQUIRED_MAT_VIS_KEYS, (
                f"mat_vis key drift on {source}/{r.get('id')}: {sorted(r['mat_vis'].keys())}"
            )


def test_mat_vis_curated_fields_populated(catalogs):
    """Phase B landed real values — a silent regression would show up
    as empty names or category='other' dominating every record."""
    for source, records in catalogs.items():
        for r in records:
            mv = r["mat_vis"]
            assert mv["name"], f"mat_vis.name empty on {source}/{r['id']}"
            assert mv["category"], f"mat_vis.category empty on {source}/{r['id']}"
            assert mv["attribution"]["license_spdx"], f"no SPDX license on {source}/{r['id']}"
            assert mv["upstream_id"], f"mat_vis.upstream_id empty on {source}/{r['id']}"


def test_category_not_universally_other(catalogs):
    """A catalog where EVERY material has category='other' would mean
    normalize_category lost the upstream classification."""
    for source, records in catalogs.items():
        cats = {r["mat_vis"]["category"] for r in records}
        assert cats - {"other"}, (
            f"{source} has no non-'other' categories — normalizer drift? {cats}"
        )


def test_mat_vis_keyset_stable_across_sources(catalogs):
    """ADR-0011 §Layer 1: missing values are null, never absent —
    so the key set must match across sources."""
    keysets = {source: set() for source in catalogs}
    for source, records in catalogs.items():
        for r in records:
            keysets[source] |= set(r["mat_vis"].keys())
    # Reference = whichever source we saw first; others must match.
    ref_source, ref_keys = next(iter(keysets.items()))
    for source, keys in keysets.items():
        assert keys == ref_keys, (
            f"mat_vis key drift: {source} has {keys ^ ref_keys} vs {ref_source}"
        )


# ── Layer 2 invariants (upstream.raw mirror) ─────────────────────


def test_upstream_block_shape_when_present(catalogs):
    """When a record carries ``upstream``, it has source + schema_version
    + raw per ADR-0011 §Layer 2. Records without upstream are legal on
    scalar-only sources; the baker writes it only when there's a payload."""
    for source, records in catalogs.items():
        for r in records:
            up = r.get("upstream")
            if up is None:
                continue
            assert up.get("source") == source, (
                f"upstream.source mismatch on {source}/{r['id']}: {up.get('source')}"
            )
            assert isinstance(up.get("schema_version"), int), (
                f"upstream.schema_version not int on {source}/{r['id']}"
            )
            assert "raw" in up, f"upstream missing 'raw' on {source}/{r['id']}: {sorted(up)}"
