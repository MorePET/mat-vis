"""CI helper: assert no baked thumb matches a known-failure fingerprint
AND no two baked thumbs are byte-identical (#385).

Usage:
    uv run python bake/preview/utils/check_thumbs.py <thumbs_dir>
    uv run python bake/preview/utils/check_thumbs.py <thumbs_dir> --release-tag v2026.05.0
    uv run python bake/preview/utils/check_thumbs.py <thumbs_dir> --json-out path/to/report.json
    uv run python bake/preview/utils/check_thumbs.py --dump-stale-allowlist --release-tag v2026.05.0

Exit codes:
    0  pass
    1  duplicate-bytes bucket survived the allow-list (#385)
    2  fingerprint hit (existing matcher fired)
    3  incomplete bake — `_bake_complete.json` sentinel missing
    4  CLI / IO error (bad args, missing dir, malformed allow-list)

Two layers of defence:

1. Per-file fingerprint match against ``bake/preview/assets/blank_default.png``
   and ``blank_default_grey.png`` (the original DISTANCE_THRESHOLD-based check —
   catches bakes where the renderer fell back to Three.js / pymat defaults).
2. Cross-thumb md5 bucketing (#385) — catches scalar-collapse,
   orchestrator closure-mixup, and texture-binding regressions where many
   distinct materials produce the same bytes. Spike consensus: md5-only,
   no perceptual layer (a perceptual gate low enough to catch these would
   false on legitimately-distinct materials measured at L2≈0.08 in the
   production substrate audit — see #385 spike).

Comparison for layer 1 uses mean per-pixel L2 distance over the 256x256
grid; threshold is conservative (≤2 in 0-255 scale ≈ visually identical)
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

import argparse
import hashlib
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[3]
ASSETS = REPO_ROOT / "bake" / "preview" / "assets"
ALLOWLIST_PATH = REPO_ROOT / "bake" / "preview" / "utils" / "thumb_check_allow.yml"
FINGERPRINTS = ["blank_default.png", "blank_default_grey.png"]
DISTANCE_THRESHOLD = 2.0  # mean per-pixel L2 in 0-255 scale
SENTINEL_NAME = "_bake_complete.json"
JSON_REPORT_NAME = "thumb-check.json"
JSON_REPORT_VERSION = 1

# Exit codes (kept module-level so tests / GHA gates can refer to them).
EXIT_OK = 0
EXIT_DUPLICATE = 1
EXIT_FINGERPRINT = 2
EXIT_NO_SENTINEL = 3
EXIT_CLI = 4

log = logging.getLogger("check-thumbs")


# ---------------------------------------------------------------------------
# Fingerprint layer (existing — untouched semantics).
# ---------------------------------------------------------------------------


def _load_rgb(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)


def _mean_pixel_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Mean L2 distance per pixel over the channel axis. Robust to
    minor compression artifacts; sensitive to material-content changes."""
    if a.shape != b.shape:
        return float("inf")
    diff = a - b
    return float(np.sqrt((diff * diff).sum(axis=-1)).mean())


# ---------------------------------------------------------------------------
# Duplicate-bytes detector (#385).
# ---------------------------------------------------------------------------


def _iter_baked_thumbs(thumbs_dir: Path):
    """Yield every PNG under ``thumbs_dir`` that should be checked.

    Skips the fingerprint reference assets in ``bake/preview/assets/`` (when
    they happen to live under the scanned tree, e.g. during in-tree dev runs).
    """
    for png in sorted(thumbs_dir.rglob("*.png")):
        if png.parent == ASSETS and png.name in FINGERPRINTS:
            continue
        yield png


def detect_duplicate_renders(
    thumbs_dir: Path,
) -> list[tuple[str, list[Path]]]:
    """Return every md5 bucket containing >1 PNG under ``thumbs_dir``.

    Implementation notes:

    - Pre-bucketed by file size first (cheap rejection — md5 is only run
      against files of the same size, which is the common case for a bake
      regression).
    - md5 chosen over sha256 per spike consensus — collision risk is moot
      for a few-thousand-PNG bake and the speed difference matters at scale.
    - Allow-list filtering happens at the caller (``check``) so this stays
      a pure detector.
    """
    by_size: dict[int, list[Path]] = defaultdict(list)
    for png in _iter_baked_thumbs(thumbs_dir):
        try:
            by_size[png.stat().st_size].append(png)
        except OSError as e:
            log.warning("stat failed for %s: %s", png, e)

    buckets: dict[str, list[Path]] = defaultdict(list)
    for size, group in by_size.items():
        if len(group) < 2:
            # Unique file size → can't collide; skip md5 entirely.
            continue
        for png in group:
            try:
                digest = hashlib.md5(png.read_bytes()).hexdigest()  # noqa: S324
            except OSError as e:
                log.warning("read failed for %s: %s", png, e)
                continue
            buckets[digest].append(png)

    return [(digest, sorted(paths)) for digest, paths in sorted(buckets.items()) if len(paths) > 1]


def _suggest_root_cause(
    bucket_paths: list[Path],
    total_baked: int,
    thumbs_dir: Path,
) -> str:
    """Heuristic hint for the runbook reader.

    See bake/preview/utils/thumb_check_runbook.md for how to act on each.
    """
    count = len(bucket_paths)
    if total_baked > 0 and count == total_baked:
        return "scalar collapse — check substrate (empty _scalars_for? wrong --repo-id?)"
    if count == 3:
        prefixes = {_source_prefix(p, thumbs_dir) for p in bucket_paths}
        if len(prefixes) == 1:
            return "orchestrator closure bug — check _build_threejs_for in run.py"
    if 1 < count <= 5 and total_baked > 0 and count < total_baked / 4:
        return "texture-binding regression — check renderer / to_threejs scalar+texture merge"
    return "unknown — see thumb_check_runbook.md"


def _source_prefix(path: Path, thumbs_dir: Path) -> str:
    """Top-level segment under thumbs_dir (the source name in a normal bake)."""
    try:
        rel = path.relative_to(thumbs_dir)
    except ValueError:
        return ""
    parts = rel.parts
    return parts[0] if parts else ""


# ---------------------------------------------------------------------------
# Allow-list (YAML — minimal stdlib parser; constrained schema).
# ---------------------------------------------------------------------------

REQUIRED_ALLOW_FIELDS = ("md5", "reason", "ticket", "until")


class AllowlistError(Exception):
    """Raised when the allow-list YAML is malformed."""


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML parser for the constrained allow-list schema:

        allowed:
          - md5: "..."
            reason: "..."
            ticket: "..."
            until: "..."

    Supports: top-level mapping with one key whose value is a sequence of
    mappings of scalar string values. Comments (``# ...``) and blank lines
    are ignored. Quoted strings (single or double) and bare strings are
    accepted. Anything else (nested mappings, multiline strings, anchors,
    flow style) raises ``AllowlistError`` — keep the file boring.
    """
    result: dict = {}
    current_seq: list | None = None
    current_item: dict | None = None
    seq_key: str | None = None  # noqa: F841 (tracked for error messages, future-proof)

    def _strip_quotes(v: str) -> str:
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            return v[1:-1]
        return v

    for raw_lineno, raw_line in enumerate(text.splitlines(), start=1):
        # Skip comment-only lines.
        if raw_line.lstrip().startswith("#"):
            continue
        line = raw_line.rstrip()
        if not line.strip():
            continue

        # Strip trailing inline comments only when no quotes on the line.
        if "#" in line and '"' not in line and "'" not in line:
            line = line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue

        # Top-level key — supports both forms:
        #   `key:`        → opens a sequence to be filled by indented `- ...` lines
        #   `key: []`     → explicit empty sequence (closes immediately)
        if not line.startswith((" ", "\t")) and ":" in line:
            top_key, _, top_val = line.partition(":")
            top_key = top_key.strip()
            top_val = top_val.strip()
            if not top_val:
                seq_key = top_key
                current_seq = []
                current_item = None
                result[seq_key] = current_seq
                continue
            if top_val == "[]":
                seq_key = top_key
                current_seq = []
                current_item = None
                result[seq_key] = []
                continue
            # Any other top-level scalar / inline value isn't part of our
            # constrained schema — be loud rather than silently drop it.
            raise AllowlistError(
                f"line {raw_lineno}: top-level key {top_key!r} only accepts "
                "an empty `[]` or a sequence of mapping items below it"
            )

        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if current_seq is None:
            raise AllowlistError(f"line {raw_lineno}: content before any top-level key")

        # Sequence item start: "- key: value"
        if stripped.startswith("- "):
            current_item = {}
            current_seq.append(current_item)
            stripped = stripped[2:]
            if ":" not in stripped:
                raise AllowlistError(f"line {raw_lineno}: sequence item must start with key:value")
            k, _, v = stripped.partition(":")
            current_item[k.strip()] = _strip_quotes(v)
            continue

        # Continuation line of current item: "  key: value"
        if current_item is None:
            raise AllowlistError(f"line {raw_lineno}: indented content outside a sequence item")
        if ":" not in stripped:
            raise AllowlistError(f"line {raw_lineno}: expected key:value")
        if indent < 2:
            raise AllowlistError(f"line {raw_lineno}: continuation lines must be indented")
        k, _, v = stripped.partition(":")
        current_item[k.strip()] = _strip_quotes(v)

    return result


def load_allowlist(path: Path = ALLOWLIST_PATH) -> list[dict]:
    """Return the validated list of allow-list entries.

    Missing file → empty list (allow-list is optional). Malformed file →
    AllowlistError (caller maps to exit 4).
    """
    if not path.exists():
        return []
    try:
        data = _parse_simple_yaml(path.read_text(encoding="utf-8"))
    except AllowlistError:
        raise
    except Exception as e:  # noqa: BLE001
        raise AllowlistError(f"{path}: {e}") from e

    entries = data.get("allowed", []) or []
    if not isinstance(entries, list):
        raise AllowlistError(f"{path}: top-level `allowed:` must be a sequence")

    validated: list[dict] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AllowlistError(f"{path}: entry #{idx} is not a mapping")
        missing = [f for f in REQUIRED_ALLOW_FIELDS if not entry.get(f)]
        if missing:
            raise AllowlistError(
                f"{path}: entry #{idx} missing required field(s): {', '.join(missing)}"
            )
        md5 = entry["md5"].lower()
        if len(md5) != 32 or not all(c in "0123456789abcdef" for c in md5):
            raise AllowlistError(
                f"{path}: entry #{idx} md5 must be a 32-char lowercase hex digest, got {md5!r}"
            )
        validated.append(
            {
                "md5": md5,
                "reason": entry["reason"],
                "ticket": entry["ticket"],
                "until": entry["until"],
            }
        )
    return validated


def _is_stale(until: str, current_tag: str | None) -> bool:
    """True if `until` has been reached or surpassed by `current_tag`.

    Both are calver tags like ``v2026.05.0``; lexicographic compare is
    correct for that format. When ``current_tag`` is unknown, we never
    flag entries as stale (warning silently suppressed).
    """
    if not current_tag:
        return False
    return current_tag >= until


# ---------------------------------------------------------------------------
# Sentinel.
# ---------------------------------------------------------------------------


def _has_sentinel(thumbs_dir: Path) -> bool:
    return (thumbs_dir / SENTINEL_NAME).is_file()


# ---------------------------------------------------------------------------
# Top-level check + JSON report.
# ---------------------------------------------------------------------------


def check(
    thumbs_dir: Path,
    *,
    allowlist: list[dict] | None = None,
    release_tag: str | None = None,
    json_out: Path | None = None,
    require_sentinel: bool = True,
) -> int:
    """Run both gates against ``thumbs_dir`` and (optionally) emit a JSON
    report. Return one of EXIT_*.

    Order matters: sentinel → fingerprint hits → duplicate buckets. The
    most-actionable failure wins the exit code so CI logs surface the
    right thing first.
    """
    if require_sentinel and not _has_sentinel(thumbs_dir):
        msg = (
            f"FATAL: no {SENTINEL_NAME} sentinel under {thumbs_dir} — "
            "bake didn't complete (or run.py is older than #385)"
        )
        print(msg)
        if json_out is not None:
            _write_report(
                json_out,
                {
                    "version": JSON_REPORT_VERSION,
                    "checked": 0,
                    "fingerprint_hits": [],
                    "duplicate_buckets": [],
                    "allowlisted": [],
                    "exit_code": EXIT_NO_SENTINEL,
                    "exit_reason": "missing_sentinel",
                    "release_tag": release_tag,
                    "generated_at": _now_iso(),
                },
            )
        return EXIT_NO_SENTINEL

    # ---- Layer 1: fingerprint match ----
    fps: dict[str, np.ndarray] = {}
    for name in FINGERPRINTS:
        p = ASSETS / name
        if not p.exists():
            print(f"FATAL: missing fingerprint {p.relative_to(REPO_ROOT)}")
            print("  run `uv run python bake/preview/utils/bake_blanks.py` to regenerate")
            return EXIT_CLI
        fps[name] = _load_rgb(p)

    fingerprint_hits: list[tuple[Path, str, float]] = []
    checked = 0
    for thumb in _iter_baked_thumbs(thumbs_dir):
        try:
            thumb_arr = _load_rgb(thumb)
        except Exception as e:  # noqa: BLE001
            log.warning("could not load %s: %s", thumb, e)
            continue
        checked += 1
        for fp_name, fp_arr in fps.items():
            d = _mean_pixel_distance(thumb_arr, fp_arr)
            if d <= DISTANCE_THRESHOLD:
                fingerprint_hits.append((thumb, fp_name, d))

    # ---- Layer 2: duplicate-bytes ----
    raw_buckets = detect_duplicate_renders(thumbs_dir)

    allow = allowlist if allowlist is not None else load_allowlist()
    allow_md5s = {e["md5"]: e for e in allow}

    surviving: list[tuple[str, list[Path]]] = []
    allowlisted_used: list[dict] = []
    for digest, paths in raw_buckets:
        entry = allow_md5s.get(digest)
        if entry is not None:
            allowlisted_used.append(
                {
                    "md5": digest,
                    "ticket": entry["ticket"],
                    "until": entry["until"],
                    "count": len(paths),
                }
            )
        else:
            surviving.append((digest, paths))

    # Stale-allow-list warnings (non-fatal).
    for entry in allow:
        if _is_stale(entry["until"], release_tag):
            log.warning(
                "stale allow-list entry md5=%s ticket=%s until=%s (release=%s) — drop or bump",
                entry["md5"],
                entry["ticket"],
                entry["until"],
                release_tag,
            )

    # ---- Print human-readable summary ----
    if fingerprint_hits:
        print(f"FAIL — {len(fingerprint_hits)} thumb(s) match a known-failure fingerprint:")
        for thumb, fp_name, d in fingerprint_hits:
            try:
                rel = thumb.relative_to(thumbs_dir)
            except ValueError:
                rel = thumb
            print(f"  {rel}  ←  {fp_name}  (distance={d:.2f})")

    if surviving:
        print(f"FAIL — {len(surviving)} duplicate-bytes bucket(s) detected (#385):")
        for digest, paths in surviving:
            suggestion = _suggest_root_cause(paths, checked, thumbs_dir)
            print(f"  md5={digest}  count={len(paths)}  → {suggestion}")
            for p in paths:
                try:
                    rel = p.relative_to(thumbs_dir)
                except ValueError:
                    rel = p
                print(f"    {rel}")

    if not fingerprint_hits and not surviving:
        if allowlisted_used:
            print(
                f"OK — {checked} thumbs checked, "
                f"{len(allowlisted_used)} allow-listed bucket(s) ignored, "
                "no other duplicates or fingerprint hits"
            )
        else:
            print(f"OK — {checked} thumbs checked, no duplicates, no fingerprint hits")

    # ---- JSON report ----
    if fingerprint_hits:
        exit_code = EXIT_FINGERPRINT
        exit_reason = "fingerprint_match"
    elif surviving:
        exit_code = EXIT_DUPLICATE
        exit_reason = "duplicate_bytes"
    else:
        exit_code = EXIT_OK
        exit_reason = "ok"

    if json_out is not None:
        _write_report(
            json_out,
            {
                "version": JSON_REPORT_VERSION,
                "checked": checked,
                "fingerprint_hits": [
                    {
                        "path": str(_safe_rel(p, thumbs_dir)),
                        "matches": fp_name,
                        "rms": round(d, 4),
                    }
                    for p, fp_name, d in fingerprint_hits
                ],
                "duplicate_buckets": [
                    {
                        "md5": digest,
                        "count": len(paths),
                        "paths": [str(_safe_rel(p, thumbs_dir)) for p in paths],
                        "suggested": _suggest_root_cause(paths, checked, thumbs_dir),
                    }
                    for digest, paths in surviving
                ],
                "allowlisted": allowlisted_used,
                "exit_code": exit_code,
                "exit_reason": exit_reason,
                "release_tag": release_tag,
                "generated_at": _now_iso(),
            },
        )

    return exit_code


def _safe_rel(p: Path, base: Path) -> Path:
    try:
        return p.relative_to(base)
    except ValueError:
        return p


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _dump_stale_allowlist(release_tag: str | None) -> int:
    try:
        entries = load_allowlist()
    except AllowlistError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return EXIT_CLI
    stale = [e for e in entries if _is_stale(e["until"], release_tag)]
    if not stale:
        print(f"OK — no stale allow-list entries (release={release_tag or 'unknown'})")
        return EXIT_OK
    print(f"STALE — {len(stale)} allow-list entry(ies) at or past their `until`:")
    for e in stale:
        print(f"  md5={e['md5']}  ticket={e['ticket']}  until={e['until']}  reason={e['reason']}")
    return EXIT_OK  # informational only; not a CI gate.


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("thumbs_dir", nargs="?", type=Path, help="output directory of a thumb bake")
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help=f"write structured JSON report (default: <thumbs_dir>/{JSON_REPORT_NAME})",
    )
    parser.add_argument(
        "--release-tag",
        default=None,
        help="current dataset release tag (e.g. v2026.05.0); used to flag stale allow-list entries",
    )
    parser.add_argument(
        "--no-sentinel",
        action="store_true",
        help="don't require _bake_complete.json (for ad-hoc audits of legacy directories)",
    )
    parser.add_argument(
        "--dump-stale-allowlist",
        action="store_true",
        help="print allow-list entries whose `until` ≤ --release-tag and exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.dump_stale_allowlist:
        return _dump_stale_allowlist(args.release_tag)

    if args.thumbs_dir is None:
        parser.print_help(sys.stderr)
        return EXIT_CLI

    thumbs_dir = args.thumbs_dir.resolve()
    if not thumbs_dir.is_dir():
        print(f"FATAL: {thumbs_dir} is not a directory", file=sys.stderr)
        return EXIT_CLI

    json_out = args.json_out if args.json_out is not None else (thumbs_dir / JSON_REPORT_NAME)

    try:
        allowlist = load_allowlist()
    except AllowlistError as e:
        print(f"FATAL: malformed allow-list: {e}", file=sys.stderr)
        return EXIT_CLI

    return check(
        thumbs_dir,
        allowlist=allowlist,
        release_tag=args.release_tag,
        json_out=json_out,
        require_sentinel=not args.no_sentinel,
    )


if __name__ == "__main__":
    sys.exit(main())
