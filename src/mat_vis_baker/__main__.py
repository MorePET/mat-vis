"""CLI entry point for the mat-vis baker.

v0.6.0+ (ADR-0012): per-file HF substrate. ``hf-bake`` is the only
bake path — one HF file per ``(source, tier, material, channel)``.
The legacy ``all`` subcommand is a thin back-compat wrapper.

The tar-based ``hf-derive`` / ``hf-derive-ktx2`` / ``merge-shards``
subcommands were retired by #189. ``hf-derive`` and ``hf-derive-ktx2``
have been reborn against the per-file substrate (#204). ``derive`` /
``derive-from-release`` / ``derive-ktx2`` (the v0.4.x subcommands)
remain retired (issue #112).

Usage:
    mat-vis-baker hf-bake <source> <tier> <work_dir> --release-tag <tag> [--limit N]
    mat-vis-baker all <source> <tier> <work_dir> --release-tag <tag>  # back-compat → hf-bake
    mat-vis-baker fetch <source> <tier> <output_dir> [--limit N]
    mat-vis-baker catalog <release_tag>
    mat-vis-baker pack-mtlx <output_dir>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from mat_vis_baker.common import CANONICAL_CATEGORIES, TIER_TO_PX, VALID_TIERS  # noqa: F401

log = logging.getLogger("mat-vis-baker")

SOURCES = ["ambientcg", "polyhaven", "gpuopen", "physicallybased"]


_RETIRED_DERIVE_MSG = (
    "The {cmd!r} subcommand was retired by ADR-0007 (parquet/GH-Releases substrate "
    "removed). The tar-based replacement is tracked in issue #112 — "
    "https://github.com/MorePET/mat-vis/issues/112. Use `mat-vis-baker hf-bake` for "
    "primary bakes in the meantime."
)


def _get_fetcher(source: str):
    if source == "ambientcg":
        from mat_vis_baker.sources.ambientcg import fetch

        return fetch
    if source == "polyhaven":
        from mat_vis_baker.sources.polyhaven import fetch

        return fetch
    if source == "gpuopen":
        from mat_vis_baker.sources.gpuopen import fetch

        return fetch
    if source == "physicallybased":
        from mat_vis_baker.sources.physicallybased import fetch

        return fetch
    raise NotImplementedError(f"Source {source!r} not yet implemented")


def cmd_all(args: argparse.Namespace) -> int:
    """Back-compat wrapper that routes to ``hf-bake`` (ADR-0007).

    The old parquet/GH-Releases bake path is gone. Existing callers
    (bake.yml via Dagger, integration tests) invoke ``mat-vis-baker all``
    with ``output_dir`` + ``--release-tag``; we translate those into a
    ``hf-bake`` call with the same semantics. Flags that the HF path
    supersedes (``--upload-chunks``, ``--category``) are ignored with a
    warning — atomic HF commits eliminate the original reason they
    existed.
    """
    from mat_vis_baker.hf_bake import bake_one

    if getattr(args, "upload_chunks", False):
        log.warning("--upload-chunks ignored: atomic HF commits replace chunk uploads")
    if getattr(args, "category", None):
        log.warning("--category ignored: per-category partitioning was retired by ADR-0007")

    tier = "scalar" if args.source == "physicallybased" else args.tier
    result = bake_one(
        source=args.source,
        tier=tier,
        release_tag=args.release_tag,
        work_dir=Path(args.output_dir),
        limit=args.limit,
        offset=args.offset,
        batch_size=getattr(args, "batch_size", 50) or 50,
        dry_run=getattr(args, "dry_run", False),
    )
    log.info("hf-bake (via legacy `all`): %s", result)
    return 0 if "error" not in result else 1


def cmd_derive(args: argparse.Namespace) -> int:
    raise NotImplementedError(_RETIRED_DERIVE_MSG.format(cmd="derive"))


def cmd_derive_from_release(args: argparse.Namespace) -> int:
    raise NotImplementedError(_RETIRED_DERIVE_MSG.format(cmd="derive-from-release"))


def cmd_catalog(args: argparse.Namespace) -> int:
    """Generate catalog + thumbnails from published release."""
    from mat_vis_baker.catalog_from_release import (
        fetch_thumbnails_from_release,
        generate_catalog_from_release,
    )

    output_dir = Path(args.output_dir)
    thumb_dir = output_dir / "mtlx"
    index_dir = output_dir / "index"

    if not args.skip_thumbnails:
        log.info("=== fetching thumbnails from release ===")
        count = fetch_thumbnails_from_release(args.release_tag, thumb_dir)
        log.info("saved %d thumbnails", count)

    log.info("=== generating catalog ===")
    md = generate_catalog_from_release(
        args.release_tag,
        thumb_dir,
        index_dir if index_dir.exists() else None,
    )
    catalog_path = output_dir / "docs" / "catalog.md"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(md)
    log.info("wrote %s", catalog_path)
    return 0


def cmd_derive_ktx2(args: argparse.Namespace) -> int:
    raise NotImplementedError(_RETIRED_DERIVE_MSG.format(cmd="derive-ktx2"))


def cmd_pack_mtlx(args: argparse.Namespace) -> int:
    """Pack original upstream MaterialX files into a JSON map."""
    from mat_vis_baker.mtlx_tier import pack_original_mtlx_json

    output_dir = Path(args.output_dir)
    source = args.source or "gpuopen"

    path = pack_original_mtlx_json(
        mtlx_dir=Path(args.mtlx_dir),
        source=source,
        output_dir=output_dir,
    )
    log.info("wrote %s", path)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch only — download textures from upstream."""
    fetch = _get_fetcher(args.source)
    records = fetch(args.tier, Path(args.output_dir), limit=args.limit)
    ok = sum(1 for r in records if r.status == "ok")
    log.info("fetch done: %d ok, %d failed", ok, len(records) - ok)
    return 0


def _resolve_hf_token(value: str | None) -> str | None:
    """Accept either a raw token or ``env:VAR`` for indirection."""
    if value is None:
        return None
    if value.startswith("env:"):
        var = value[len("env:") :]
        return os.environ.get(var)
    return value


def cmd_audit_orphans(args: argparse.Namespace) -> int:
    """Audit (and optionally clean up) mid-batch orphan LFS blobs (#190)."""
    from mat_vis_baker.audit_orphans import _confirm_delete, audit_orphans

    token = _resolve_hf_token(args.hf_token)

    if args.delete and not _confirm_delete():
        print("aborted: confirmation not given", file=sys.stderr)
        return 1

    try:
        result = audit_orphans(
            repo_id=args.repo,
            revision=args.revision,
            delete=args.delete,
            allow_prod=args.allow_prod,
            hf_token=token,
        )
    except ValueError as e:
        # Prod guard or other input-validation error — keep the
        # message on stderr and bail without a stack trace.
        print(f"error: {e}", file=sys.stderr)
        return 2

    log.info(
        "audit-orphans %s@%s: total_lfs=%d referenced=%d orphans=%d deleted=%s",
        result["repo_id"],
        result["revision"],
        result["total_lfs"],
        result["referenced"],
        len(result["orphans"]),
        result["deleted"],
    )
    if result["orphans"]:
        log.info("orphan oids:")
        for oid in result["orphans"]:
            log.info("  %s", oid)
    return 0


def cmd_hf_derive(args: argparse.Namespace) -> int:
    """Derive a smaller PNG tier from an existing per-file HF tier (#204)."""
    from mat_vis_baker.hf_derive_per_file import derive_smaller_tier

    token = _resolve_hf_token(args.hf_token)
    result = derive_smaller_tier(
        source=args.source,
        target_tier=args.target_tier,
        source_tier=args.source_tier,
        release_tag=args.release_tag,
        work_dir=Path(args.work_dir),
        repo_id=args.repo_id,
        hf_token=token,
        dry_run=args.dry_run,
        allow_prod=args.allow_prod,
        limit=args.limit,
        batch_size=args.batch_size,
        batch_max_bytes=args.batch_max_bytes,
    )
    log.info("hf-derive result: %s", result)
    if "error" in result:
        return 1
    return 0


def cmd_hf_derive_ktx2(args: argparse.Namespace) -> int:
    """Transcode a per-file PNG tier to a KTX2 tier (#204)."""
    from mat_vis_baker.hf_derive_per_file import derive_ktx2_tier

    token = _resolve_hf_token(args.hf_token)
    target_tier = args.target_tier or f"ktx2-{args.source_tier}"
    result = derive_ktx2_tier(
        source=args.source,
        source_tier=args.source_tier,
        target_tier=target_tier,
        release_tag=args.release_tag,
        work_dir=Path(args.work_dir),
        repo_id=args.repo_id,
        hf_token=token,
        dry_run=args.dry_run,
        allow_prod=args.allow_prod,
        limit=args.limit,
        batch_size=args.batch_size,
        batch_max_bytes=args.batch_max_bytes,
    )
    log.info("hf-derive-ktx2 result: %s", result)
    if "error" in result:
        return 1
    return 0


def cmd_hf_bake(args: argparse.Namespace) -> int:
    """Bake (source, tier) → HF commit. Per-file substrate (ADR-0012)."""
    from mat_vis_baker.hf_bake import bake_one

    tier = args.tier
    if args.source == "physicallybased" or tier == "scalar":
        # Scalar sources don't honor a tier — any non-"scalar" value
        # passed here is a user error.
        tier = "scalar"

    result = bake_one(
        source=args.source,
        tier=tier,
        release_tag=args.release_tag,
        work_dir=Path(args.work_dir),
        repo_id=args.repo_id,
        limit=args.limit,
        offset=args.offset,
        batch_size=args.batch_size,
        batch_max_bytes=args.batch_max_bytes,
        dry_run=args.dry_run,
        allow_prod=args.allow_prod,
    )
    log.info("hf-bake result: %s", result)
    if "error" in result:
        return 1
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    # #217: line-buffered stdout so structured `bake_plan` / `bake_progress`
    # / `bake_done` lines reach the parent process (GitHub Actions live
    # log) within the OS pipe-flush window. `PYTHONUNBUFFERED=1` is set
    # on the Dagger baker container, but this is the belt to that
    # suspenders for non-Dagger callers.
    from mat_vis_baker.progress import enable_line_buffering

    enable_line_buffering()

    parser = argparse.ArgumentParser(prog="mat-vis-baker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_all = sub.add_parser("all", help="Full pipeline: fetch → bake → pack → index")
    p_all.add_argument("source", choices=SOURCES)
    p_all.add_argument("tier", choices=VALID_TIERS)
    p_all.add_argument("output_dir")
    p_all.add_argument("--offset", type=int, default=0, help="Skip first N materials")
    p_all.add_argument("--limit", type=int, default=None)
    p_all.add_argument("--release-tag", required=True)
    p_all.add_argument(
        "--batch-size", type=int, default=50, help="Materials per streaming batch (default: 50)"
    )
    p_all.add_argument(
        "--upload-chunks",
        action="store_true",
        help="Upload + delete each parquet partition as it closes (frees disk during run)",
    )
    p_all.add_argument(
        "--category",
        choices=sorted(CANONICAL_CATEGORIES),
        default=None,
        help=(
            "Restrict bake to materials whose normalized category matches. "
            "Produces exactly one parquet + rowmap. Used for surgical gap-fills "
            "on an existing release without touching other categories' offsets."
        ),
    )
    p_all.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run fetch + bake to local disk as usual, but skip any release uploads. "
            "Output stays in <output_dir>/ so you can inspect it before committing. "
            "Composes with --category and --limit for quick smoke runs."
        ),
    )

    p_derive = sub.add_parser("derive", help="Derive smaller tier from existing bake output")
    p_derive.add_argument("source", choices=SOURCES)
    p_derive.add_argument("tier", choices=VALID_TIERS)
    p_derive.add_argument(
        "source_dir", help="Directory with existing bake output (textures + index)"
    )
    p_derive.add_argument("output_dir")
    p_derive.add_argument("--release-tag", required=True)

    p_dfr = sub.add_parser(
        "derive-from-release",
        help="Derive smaller tier from existing release parquets (no upstream download)",
    )
    p_dfr.add_argument("source", choices=SOURCES)
    p_dfr.add_argument("tier", choices=VALID_TIERS)
    p_dfr.add_argument("output_dir")
    p_dfr.add_argument(
        "--source-tier", default="1k", choices=VALID_TIERS, help="Tier to read from (default: 1k)"
    )
    p_dfr.add_argument("--release-tag", required=True)
    p_dfr.add_argument("--limit", type=int, default=None, help="Process only first N materials")

    p_fetch = sub.add_parser("fetch", help="Fetch textures from upstream")
    p_fetch.add_argument("source", choices=SOURCES)
    p_fetch.add_argument("tier", choices=VALID_TIERS)
    p_fetch.add_argument("output_dir")
    p_fetch.add_argument("--limit", type=int, default=None)

    p_cat = sub.add_parser("catalog", help="Generate catalog + thumbnails from release")
    p_cat.add_argument("release_tag", help="Release tag (e.g. v2026.04.0)")
    p_cat.add_argument(
        "--output-dir", default=".", help="Repo root (writes docs/catalog.md + mtlx/)"
    )
    p_cat.add_argument("--skip-thumbnails", action="store_true", help="Skip thumbnail download")

    p_ktx2 = sub.add_parser(
        "derive-ktx2",
        help="Derive KTX2-compressed tier from existing release PNGs",
    )
    p_ktx2.add_argument("output_dir")
    p_ktx2.add_argument("--release-tag", required=True)
    p_ktx2.add_argument(
        "--source-tier", default="1k", help="PNG tier to transcode from (default: 1k)"
    )
    p_ktx2.add_argument(
        "--target-tier", default=None, help="KTX2 tier name (default: ktx2-{source-tier})"
    )
    p_ktx2.add_argument("--source", default=None, help="Restrict to one source")

    p_hf = sub.add_parser(
        "hf-bake",
        help="Bake (source, tier) and atomically push to HF Datasets (ADR-0012).",
    )
    p_hf.add_argument("source", choices=SOURCES)
    p_hf.add_argument(
        "tier",
        choices=VALID_TIERS + ["scalar"],
        help="Tier name, or 'scalar' for physicallybased (no textures).",
    )
    p_hf.add_argument("work_dir", help="Scratch dir for fetched + baked textures.")
    p_hf.add_argument("--release-tag", required=True)
    p_hf.add_argument(
        "--repo-id",
        default="gerchowl/mat-vis",
        help="HF dataset repo (default: gerchowl/mat-vis).",
    )
    p_hf.add_argument("--limit", type=int, default=None)
    p_hf.add_argument("--offset", type=int, default=0)
    p_hf.add_argument(
        "--batch-size",
        type=int,
        default=300,
        help=(
            "Materials per atomic commit (count ceiling). #228: bytes is "
            "the binding constraint at typical content (~1.5 MiB/material), "
            "so 300 is a safe overshoot — flush trips on whichever bound hits first."
        ),
    )
    p_hf.add_argument(
        "--batch-max-bytes",
        type=int,
        default=700 * 1024 * 1024,
        help=(
            "Max bytes per atomic commit (default 734003200 = 700 MiB). "
            "Flush triggers on first-of-N-or-bytes — whichever bound trips "
            "first. Bytes only; no human-friendly units. HF hard-caps at "
            "1 GiB/commit; 700 MiB leaves headroom for catalog + manifest "
            "+ sentinel commits sharing the 128/hr/repo budget."
        ),
    )
    p_hf.add_argument(
        "--dry-run",
        action="store_true",
        help="Build per-file artifacts locally; skip the HF push.",
    )
    p_hf.add_argument(
        "--allow-prod",
        action="store_true",
        help=(
            "Permit writes to non-scratch HF dataset repos. Scratch "
            "repos are named */mat-vis-tst and */mat-vis-*-tst; anything "
            "else requires this flag."
        ),
    )

    # ── per-file derive (#204) ────────────────────────────────────
    p_hfd = sub.add_parser(
        "hf-derive",
        help=(
            "Derive a smaller PNG tier from an existing per-file HF tier "
            "(no upstream re-fetch). ADR-0012 / #204."
        ),
    )
    p_hfd.add_argument("--source", required=True, choices=SOURCES)
    p_hfd.add_argument(
        "--source-tier",
        required=True,
        choices=VALID_TIERS,
        help="Tier to read from on HF (must already be baked).",
    )
    p_hfd.add_argument(
        "--target-tier",
        required=True,
        choices=VALID_TIERS,
        help="Smaller tier to derive. Must be ≤ source-tier (no upscale).",
    )
    p_hfd.add_argument("--release-tag", required=True)
    p_hfd.add_argument("--work-dir", required=True, help="Scratch directory.")
    p_hfd.add_argument(
        "--repo-id",
        default="gerchowl/mat-vis",
        help="HF dataset repo (default: gerchowl/mat-vis).",
    )
    p_hfd.add_argument(
        "--hf-token",
        default=None,
        help="HfApi token; raw or 'env:VAR'. Falls back to cached HF login.",
    )
    p_hfd.add_argument("--limit", type=int, default=None)
    p_hfd.add_argument(
        "--batch-size",
        type=int,
        default=300,
        help=(
            "Materials per atomic commit (count ceiling, #228). Bytes is "
            "the binding constraint at typical content; 300 is a safe overshoot."
        ),
    )
    p_hfd.add_argument(
        "--batch-max-bytes",
        type=int,
        default=700 * 1024 * 1024,
        help=(
            "Max bytes per atomic commit (default 734003200 = 700 MiB). "
            "Flush triggers on first-of-N-or-bytes. HF caps at 1 GiB/commit; "
            "700 MiB leaves headroom for catalog + manifest + sentinel."
        ),
    )
    p_hfd.add_argument("--dry-run", action="store_true")
    p_hfd.add_argument(
        "--allow-prod",
        action="store_true",
        help="Required to target any non-*-tst HF dataset repo.",
    )

    p_hfk = sub.add_parser(
        "hf-derive-ktx2",
        help=(
            "Transcode an existing per-file PNG tier on HF into a KTX2 tier. "
            "Requires toktx on PATH. ADR-0012 / #204."
        ),
    )
    p_hfk.add_argument("--source", required=True, choices=SOURCES)
    p_hfk.add_argument(
        "--source-tier",
        required=True,
        choices=VALID_TIERS,
        help="PNG tier to transcode from (must already be baked).",
    )
    p_hfk.add_argument(
        "--target-tier",
        default=None,
        help="KTX2 tier label (default: ktx2-<source-tier>).",
    )
    p_hfk.add_argument("--release-tag", required=True)
    p_hfk.add_argument("--work-dir", required=True, help="Scratch directory.")
    p_hfk.add_argument(
        "--repo-id",
        default="gerchowl/mat-vis",
        help="HF dataset repo (default: gerchowl/mat-vis).",
    )
    p_hfk.add_argument(
        "--hf-token",
        default=None,
        help="HfApi token; raw or 'env:VAR'. Falls back to cached HF login.",
    )
    p_hfk.add_argument("--limit", type=int, default=None)
    p_hfk.add_argument(
        "--batch-size",
        type=int,
        default=300,
        help=(
            "Materials per atomic commit (count ceiling, #228). Bytes is "
            "the binding constraint at typical content; 300 is a safe overshoot."
        ),
    )
    p_hfk.add_argument(
        "--batch-max-bytes",
        type=int,
        default=700 * 1024 * 1024,
        help=(
            "Max bytes per atomic commit (default 734003200 = 700 MiB). "
            "Flush triggers on first-of-N-or-bytes. HF caps at 1 GiB/commit; "
            "700 MiB leaves headroom for catalog + manifest + sentinel."
        ),
    )
    p_hfk.add_argument("--dry-run", action="store_true")
    p_hfk.add_argument(
        "--allow-prod",
        action="store_true",
        help="Required to target any non-*-tst HF dataset repo.",
    )

    p_mtlx = sub.add_parser(
        "pack-mtlx",
        help="Pack original upstream .mtlx files into JSON map for release",
    )
    p_mtlx.add_argument("output_dir")
    p_mtlx.add_argument("--source", default=None, help="Source (default: gpuopen)")
    p_mtlx.add_argument("--mtlx-dir", default="mtlx", help="Directory with upstream .mtlx files")

    p_audit = sub.add_parser(
        "audit-orphans",
        help=(
            "List (and optionally delete) orphan LFS blobs left by mid-batch "
            "crashes under the per-file substrate (#190 / ADR-0012 follow-up)."
        ),
    )
    p_audit.add_argument(
        "--repo",
        required=True,
        help="HF dataset repo (owner/name), e.g. gerchowl/mat-vis-tst.",
    )
    p_audit.add_argument(
        "--revision",
        default="main",
        help="Git ref to audit against (default: main).",
    )
    p_audit.add_argument(
        "--delete",
        action="store_true",
        help=(
            "Permanently delete orphan blobs. Dry-run otherwise. "
            "Prompts for 'DELETE' on stdin; bypass with MAT_VIS_AUDIT_FORCE=1."
        ),
    )
    p_audit.add_argument(
        "--allow-prod",
        action="store_true",
        help=(
            "Required to audit non-scratch repos. Scratch repos are named "
            "*/mat-vis-tst or */mat-vis-*-tst."
        ),
    )
    p_audit.add_argument(
        "--hf-token",
        default=None,
        help=(
            "Token for HfApi. Accepts either a raw token or 'env:VAR'; "
            "falls back to the cached huggingface_hub login."
        ),
    )

    args = parser.parse_args()

    if args.command == "all":
        return cmd_all(args)
    if args.command == "derive":
        return cmd_derive(args)
    if args.command == "derive-from-release":
        return cmd_derive_from_release(args)
    if args.command == "fetch":
        return cmd_fetch(args)
    if args.command == "catalog":
        return cmd_catalog(args)
    if args.command == "derive-ktx2":
        return cmd_derive_ktx2(args)
    if args.command == "pack-mtlx":
        return cmd_pack_mtlx(args)
    if args.command == "hf-bake":
        return cmd_hf_bake(args)
    if args.command == "hf-derive":
        return cmd_hf_derive(args)
    if args.command == "hf-derive-ktx2":
        return cmd_hf_derive_ktx2(args)
    if args.command == "audit-orphans":
        return cmd_audit_orphans(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
