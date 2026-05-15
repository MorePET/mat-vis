"""VLM-based visual QA — compare our thumbs against upstream previews.

Fetches upstream preview images and our shader-ball thumbnails from HF,
pairs them per material, and sends batches to Claude's vision API to
flag materials where the render looks fundamentally wrong.

This is a **flagging** tool, not a gate — it surfaces suspicious pairs
for human eyeballing, not automated rejection.

Usage::

    python -m scripts.visual_qa \\
        --release-tag v2026.04.99-tst-full-369 \\
        --repo-id gerchowl/mat-vis-tst \\
        [--sources ambientcg polyhaven gpuopen] \\
        [--limit 20] \\
        [--batch-size 5] \\
        [--json]

Requires: ``ANTHROPIC_API_KEY`` env var and ``anthropic`` package.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
from dataclasses import dataclass, field

import requests

log = logging.getLogger("visual-qa")

__all__ = [
    "MaterialPair",
    "QAResult",
    "QAReport",
    "fetch_upstream_preview",
    "fetch_our_thumb",
    "build_pairs",
    "judge_batch",
]

THUMB_SOURCES = ["ambientcg", "polyhaven", "gpuopen"]


# ── data classes ───────────────────────────────────────────────────


@dataclass
class MaterialPair:
    """A paired upstream preview + our thumbnail for one material."""

    source: str
    material_id: str
    upstream_url: str
    upstream_bytes: bytes | None = None
    our_thumb_bytes: bytes | None = None

    @property
    def has_both(self) -> bool:
        return bool(self.upstream_bytes and self.our_thumb_bytes)


@dataclass
class QAResult:
    """VLM verdict for one material."""

    source: str
    material_id: str
    verdict: str  # "ok" | "suspicious" | "error" | "skip"
    reason: str = ""
    confidence: str = ""  # "high" | "medium" | "low"


@dataclass
class QAReport:
    """Aggregated QA results."""

    release_tag: str
    repo_id: str
    results: list[QAResult] = field(default_factory=list)
    model: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    @property
    def suspicious(self) -> list[QAResult]:
        return [r for r in self.results if r.verdict == "suspicious"]

    @property
    def errors(self) -> list[QAResult]:
        return [r for r in self.results if r.verdict == "error"]

    def to_dict(self) -> dict:
        return {
            "release_tag": self.release_tag,
            "repo_id": self.repo_id,
            "model": self.model,
            "total_materials": len(self.results),
            "suspicious": len(self.suspicious),
            "errors": len(self.errors),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "results": [
                {
                    "source": r.source,
                    "material_id": r.material_id,
                    "verdict": r.verdict,
                    "reason": r.reason,
                    "confidence": r.confidence,
                }
                for r in self.results
            ],
        }

    def print_summary(self) -> None:
        print(f"visual-qa {self.release_tag} (repo={self.repo_id})")
        print(f"  model: {self.model}")
        print(f"  materials checked: {len(self.results)}")
        print(f"  suspicious: {len(self.suspicious)}")
        print(f"  errors: {len(self.errors)}")
        print(
            f"  tokens: {self.total_input_tokens} in / "
            f"{self.total_output_tokens} out"
        )
        if self.suspicious:
            print()
            print("flagged materials:")
            for r in self.suspicious:
                print(f"  {r.source}/{r.material_id}: {r.reason} [{r.confidence}]")
        if self.errors:
            print()
            print("errors:")
            for r in self.errors:
                print(f"  {r.source}/{r.material_id}: {r.reason}")


# ── upstream preview fetchers ──────────────────────────────────────


def _upstream_preview_url(source: str, material_id: str, entry: dict) -> str | None:
    """Resolve the upstream preview URL for a material."""
    if source == "ambientcg":
        pi = entry.get("previewImage", {})
        return pi.get("256-PNG") or pi.get("256-JPG-FFFFFF")
    if source == "polyhaven":
        return (
            f"https://cdn.polyhaven.com/asset_img/thumbs/"
            f"{material_id}.png?width=256&height=256"
        )
    if source == "gpuopen":
        renders = entry.get("renders", [])
        if renders:
            rid = renders[0]
            return (
                f"https://api.matlib.gpuopen.com/api/renders/"
                f"{rid}/download_thumbnail/"
            )
    return None


def fetch_upstream_preview(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 15,
) -> bytes | None:
    """Download an upstream preview image. Returns PNG/JPG bytes or None."""
    s = session or requests.Session()
    try:
        resp = s.get(url, timeout=timeout)
        if resp.status_code == 200 and len(resp.content) > 100:
            return resp.content
    except Exception:
        pass
    return None


def fetch_our_thumb(
    source: str,
    material_id: str,
    *,
    repo_id: str,
    release_tag: str,
    session: requests.Session | None = None,
    timeout: int = 15,
) -> bytes | None:
    """Download our thumb from HF. Returns PNG bytes or None."""
    s = session or requests.Session()
    url = (
        f"https://huggingface.co/datasets/{repo_id}/resolve/"
        f"{release_tag}/{source}/thumb/{material_id}/thumb.png"
    )
    try:
        resp = s.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and len(resp.content) > 100:
            return resp.content
    except Exception:
        pass
    return None


# ── pairing ────────────────────────────────────────────────────────


def build_pairs(
    source: str,
    material_ids: list[str],
    catalog_entries: dict[str, dict],
    *,
    repo_id: str,
    release_tag: str,
    session: requests.Session | None = None,
) -> list[MaterialPair]:
    """Build (upstream, ours) pairs for a list of material IDs."""
    s = session or requests.Session()
    pairs = []
    for mid in material_ids:
        entry = catalog_entries.get(mid, {})
        upstream_url = _upstream_preview_url(source, mid, entry)
        if not upstream_url:
            continue

        pair = MaterialPair(
            source=source,
            material_id=mid,
            upstream_url=upstream_url,
        )
        pair.upstream_bytes = fetch_upstream_preview(upstream_url, session=s)
        pair.our_thumb_bytes = fetch_our_thumb(
            source,
            mid,
            repo_id=repo_id,
            release_tag=release_tag,
            session=s,
        )
        if pair.has_both:
            pairs.append(pair)
        else:
            log.debug(
                "%s/%s: skipped (upstream=%s, ours=%s)",
                source,
                mid,
                bool(pair.upstream_bytes),
                bool(pair.our_thumb_bytes),
            )
    return pairs


# ── VLM judging ────────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You are a PBR material quality reviewer for a texture library.

You will see pairs of images for the same material:
- **upstream**: the source library's preview (may be a sphere render, \
flat perspective swatch, or textured object)
- **ours**: our shader-ball render of the same material (sphere on pedestal, \
dark background)

For each pair, judge whether our render looks like it uses the \
SAME material as the upstream preview.

Flag ONLY if the render looks fundamentally wrong:
- Completely different color or pattern
- Inverted or broken normals (lighting from wrong direction, flat shading)
- Channel swap (metal looks plastic or vice versa)
- Missing/black/white/default-grey render
- Major color shift (wrong colorspace or gamma)

Do NOT flag expected differences:
- Different render style (3D sphere vs flat swatch)
- Different lighting or background
- Resolution or sharpness differences
- Minor tiling scale differences

Respond with a JSON array. Each element:
{"material_id": "...", "verdict": "ok"|"suspicious", "reason": "...", \
"confidence": "high"|"medium"|"low"}

If uncertain, lean toward "ok" — this is a flagging tool, not a gate. \
Only flag things a human should look at."""


def _img_to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _detect_media_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "image/png"  # fallback


def _build_batch_content(pairs: list[MaterialPair]) -> list[dict]:
    """Build Claude API content blocks for a batch of pairs."""
    content: list[dict] = []
    for pair in pairs:
        content.append(
            {"type": "text", "text": f"--- Material: {pair.source}/{pair.material_id} ---"}
        )
        content.append(
            {"type": "text", "text": "**upstream preview:**"}
        )
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _detect_media_type(pair.upstream_bytes),
                    "data": _img_to_base64(pair.upstream_bytes),
                },
            }
        )
        content.append(
            {"type": "text", "text": "**our shader-ball render:**"}
        )
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _detect_media_type(pair.our_thumb_bytes),
                    "data": _img_to_base64(pair.our_thumb_bytes),
                },
            }
        )
    content.append(
        {
            "type": "text",
            "text": (
                f"Judge all {len(pairs)} materials above. "
                "Return a JSON array with one verdict per material."
            ),
        }
    )
    return content


def judge_batch(
    pairs: list[MaterialPair],
    *,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-20250514",
) -> tuple[list[QAResult], dict]:
    """Send a batch of pairs to Claude vision and parse results.

    Returns (results, usage_dict).
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    content = _build_batch_content(pairs)

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }

    # Parse response — expect JSON array in the text
    results = _parse_verdicts(pairs, response)
    return results, usage


def _parse_verdicts(
    pairs: list[MaterialPair],
    response,
) -> list[QAResult]:
    """Extract QAResults from Claude's response."""
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    # Try to extract JSON from the response
    results: list[QAResult] = []
    try:
        # Find JSON array in the text
        start = text.index("[")
        end = text.rindex("]") + 1
        verdicts = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        # If parsing fails, return error results for all
        for pair in pairs:
            results.append(
                QAResult(
                    source=pair.source,
                    material_id=pair.material_id,
                    verdict="error",
                    reason=f"Failed to parse VLM response: {text[:200]}",
                )
            )
        return results

    # Match verdicts to pairs by material_id
    verdict_map = {v.get("material_id", ""): v for v in verdicts}
    for pair in pairs:
        key = pair.material_id
        # Also try source/material_id format
        v = verdict_map.get(key) or verdict_map.get(
            f"{pair.source}/{pair.material_id}"
        )
        if v:
            results.append(
                QAResult(
                    source=pair.source,
                    material_id=pair.material_id,
                    verdict=v.get("verdict", "error"),
                    reason=v.get("reason", ""),
                    confidence=v.get("confidence", ""),
                )
            )
        else:
            results.append(
                QAResult(
                    source=pair.source,
                    material_id=pair.material_id,
                    verdict="error",
                    reason="No verdict in VLM response",
                )
            )
    return results


# ── catalog loaders (for upstream preview URLs) ────────────────────


def _load_hf_catalog(
    source: str,
    *,
    repo_id: str,
    release_tag: str,
    session: requests.Session | None = None,
) -> dict[str, dict]:
    """Load <source>.json from HF → {material_id: entry_dict}."""
    s = session or requests.Session()
    url = (
        f"https://huggingface.co/datasets/{repo_id}/resolve/"
        f"{release_tag}/{source}.json"
    )
    resp = s.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    entries = resp.json()
    return {e["id"]: e for e in entries}


def _load_upstream_entries(
    source: str,
) -> dict[str, dict]:
    """Load upstream API entries keyed by material ID."""
    if source == "ambientcg":
        from mat_vis_baker.sources.ambientcg import discover

        entries = discover()
        return {e["assetId"]: e for e in entries}
    if source == "polyhaven":
        from mat_vis_baker.sources.polyhaven import discover

        return discover()
    if source == "gpuopen":
        from mat_vis_baker.sources.gpuopen import discover

        entries = discover()
        return {e["id"]: e for e in entries}
    raise ValueError(f"unknown source: {source!r}")


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="visual-qa",
        description="VLM-based visual QA — compare thumbs vs upstream.",
    )
    p.add_argument("--release-tag", required=True)
    p.add_argument("--repo-id", default="gerchowl/mat-vis-tst")
    p.add_argument(
        "--sources",
        nargs="+",
        default=THUMB_SOURCES,
        choices=THUMB_SOURCES,
    )
    p.add_argument("--limit", type=int, default=0, help="Max materials per source (0=all)")
    p.add_argument("--batch-size", type=int, default=5, help="Pairs per VLM call")
    p.add_argument("--model", default="claude-sonnet-4-20250514")
    p.add_argument("--json", action="store_true", dest="json_output")
    p.add_argument(
        "--material-ids",
        nargs="+",
        default=None,
        help="Specific material IDs to check (overrides --limit)",
    )
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set")
        return 2

    report = QAReport(
        release_tag=args.release_tag,
        repo_id=args.repo_id,
        model=args.model,
    )
    session = requests.Session()

    for source in args.sources:
        log.info("=== %s ===", source)

        # Load catalog from HF to get material IDs + upstream entry data
        log.info("loading HF catalog for %s ...", source)
        try:
            hf_catalog = _load_hf_catalog(
                source,
                repo_id=args.repo_id,
                release_tag=args.release_tag,
                session=session,
            )
        except Exception as e:
            log.error("failed to load HF catalog for %s: %s", source, e)
            continue

        # Load upstream entries (for preview URLs)
        log.info("loading upstream entries for %s ...", source)
        try:
            upstream = _load_upstream_entries(source)
        except Exception as e:
            log.error("failed to load upstream for %s: %s", source, e)
            continue

        # Select material IDs
        if args.material_ids:
            material_ids = [
                mid for mid in args.material_ids if mid in hf_catalog
            ]
        else:
            ok_entries = [
                e for e in hf_catalog.values() if e.get("status") != "failed"
            ]
            material_ids = [e["id"] for e in ok_entries]
            if args.limit:
                material_ids = material_ids[: args.limit]

        log.info("building %d pairs for %s ...", len(material_ids), source)
        pairs = build_pairs(
            source,
            material_ids,
            upstream,
            repo_id=args.repo_id,
            release_tag=args.release_tag,
            session=session,
        )
        log.info("  %d pairs with both images", len(pairs))

        # Batch and judge
        for i in range(0, len(pairs), args.batch_size):
            batch = pairs[i : i + args.batch_size]
            batch_ids = [f"{p.material_id}" for p in batch]
            log.info(
                "  judging batch %d/%d (%s) ...",
                i // args.batch_size + 1,
                (len(pairs) + args.batch_size - 1) // args.batch_size,
                ", ".join(batch_ids),
            )
            try:
                results, usage = judge_batch(
                    batch,
                    api_key=api_key,
                    model=args.model,
                )
                report.results.extend(results)
                report.total_input_tokens += usage.get("input_tokens", 0)
                report.total_output_tokens += usage.get("output_tokens", 0)
            except Exception as e:
                log.error("  batch failed: %s", e)
                for pair in batch:
                    report.results.append(
                        QAResult(
                            source=pair.source,
                            material_id=pair.material_id,
                            verdict="error",
                            reason=str(e),
                        )
                    )

    # Output
    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        report.print_summary()

    if report.suspicious:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
