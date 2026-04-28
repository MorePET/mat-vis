"""CLI entry point for the mat-vis baker.

v0.5.0+ (ADR-0007): the primary bake path is ``hf-bake`` — one atomic
HF commit per ``(source, tier)``. The legacy ``all`` subcommand is a
thin back-compat wrapper that routes to ``hf-bake``. ``derive`` /
``derive-from-release`` / ``derive-ktx2`` are retired (issue #112
will re-implement them on the tar substrate).

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


def cmd_hf_derive(args: argparse.Namespace) -> int:
    from mat_vis_baker.hf_derive import derive_smaller_tier
    from mat_vis_baker.shard_utils import validate_shard_args

    shard = validate_shard_args(args.shard_index, args.shard_total)
    result = derive_smaller_tier(
        source=args.source,
        target_tier=args.target_tier,
        source_tier=args.source_tier,
        release_tag=args.release_tag,
        work_dir=Path(args.work_dir),
        repo_id=args.repo_id,
        dry_run=args.dry_run,
        shard=shard,
    )
    log.info("hf-derive result: %s", result)
    return 0 if "error" not in result else 1


def cmd_hf_derive_ktx2(args: argparse.Namespace) -> int:
    from mat_vis_baker.hf_derive import derive_ktx2_tier
    from mat_vis_baker.shard_utils import validate_shard_args

    shard = validate_shard_args(args.shard_index, args.shard_total)
    result = derive_ktx2_tier(
        source=args.source,
        source_tier=args.source_tier,
        release_tag=args.release_tag,
        work_dir=Path(args.work_dir),
        repo_id=args.repo_id,
        dry_run=args.dry_run,
        target_tier=args.target_tier,
        shard=shard,
    )
    log.info("hf-derive-ktx2 result: %s", result)
    return 0 if "error" not in result else 1


def cmd_merge_shards(args: argparse.Namespace) -> int:
    from mat_vis_baker.merge_shards import merge_shards

    result = merge_shards(
        source=args.source,
        tier=args.tier,
        release_tag=args.release_tag,
        work_dir=Path(args.work_dir),
        repo_id=args.repo_id,
        dry_run=args.dry_run,
        keep_shards=args.keep_shards,
    )
    log.info("merge-shards result: %s", result)
    # merge_shards never puts "error" in its result — it raises on any
    # fault (incomplete shard set, range-read mismatch). Always return 0.
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


def cmd_hf_bake(args: argparse.Namespace) -> int:
    """Bake (source, tier) → HF commit. Per-file substrate by default
    (ADR-0012); legacy tar via --legacy-tar for one transition cycle."""
    from mat_vis_baker.hf_bake import bake_one
    from mat_vis_baker.shard_utils import validate_shard_args

    tier = args.tier
    if args.source == "physicallybased" or tier == "scalar":
        # Scalar sources don't honor a tier — any non-"scalar" value
        # passed here is a user error.
        tier = "scalar"

    shard = validate_shard_args(args.shard_index, args.shard_total)
    result = bake_one(
        source=args.source,
        tier=tier,
        release_tag=args.release_tag,
        work_dir=Path(args.work_dir),
        repo_id=args.repo_id,
        limit=args.limit,
        offset=args.offset,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        shard=shard,
        legacy_tar=args.legacy_tar,
        allow_prod=args.allow_prod,
    )
    log.info("hf-bake result: %s", result)
    if "error" in result:
        return 1
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

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
        help="Bake (source, tier) and atomically push to HF Datasets (ADR-0007).",
    )
    p_hf.add_argument("source", choices=SOURCES)
    p_hf.add_argument(
        "tier",
        choices=VALID_TIERS + ["scalar"],
        help="Tier name, or 'scalar' for physicallybased (no textures).",
    )
    p_hf.add_argument("work_dir", help="Scratch dir for fetched + baked textures + tar.")
    p_hf.add_argument("--release-tag", required=True)
    p_hf.add_argument(
        "--repo-id",
        default="gerchowl/mat-vis",
        help="HF dataset repo (default: gerchowl/mat-vis).",
    )
    p_hf.add_argument("--limit", type=int, default=None)
    p_hf.add_argument("--offset", type=int, default=0)
    p_hf.add_argument("--batch-size", type=int, default=50)
    p_hf.add_argument(
        "--dry-run",
        action="store_true",
        help="Build tar + manifest locally; skip the HF push.",
    )
    p_hf.add_argument(
        "--shard-index",
        type=int,
        default=None,
        help="0-based shard index. Requires --shard-total. Legacy tar only.",
    )
    p_hf.add_argument(
        "--shard-total",
        type=int,
        default=None,
        help="Total number of shards. Requires --shard-index. Legacy tar only.",
    )
    p_hf.add_argument(
        "--legacy-tar",
        action="store_true",
        help=(
            "Use the pre-ADR-0012 tar+rowmap substrate instead of the "
            "default per-file layout. One-release-cycle escape hatch; "
            "retired by #189."
        ),
    )
    p_hf.add_argument(
        "--allow-prod",
        action="store_true",
        help=(
            "Permit writes to non-scratch HF dataset repos (per-file "
            "substrate guard). Scratch repos are named */mat-vis-tst "
            "and */mat-vis-*-tst; anything else requires this flag."
        ),
    )

    p_hd = sub.add_parser(
        "hf-derive",
        help="Derive a smaller tier from an existing HF PNG tar (resize).",
    )
    p_hd.add_argument("source", choices=SOURCES)
    p_hd.add_argument("target_tier", choices=VALID_TIERS)
    p_hd.add_argument("work_dir")
    p_hd.add_argument("--source-tier", default="1k", choices=VALID_TIERS)
    p_hd.add_argument("--release-tag", required=True)
    p_hd.add_argument("--repo-id", default="gerchowl/mat-vis")
    p_hd.add_argument("--dry-run", action="store_true")
    p_hd.add_argument(
        "--shard-index",
        type=int,
        default=None,
        help="0-based shard index. Requires --shard-total.",
    )
    p_hd.add_argument(
        "--shard-total",
        type=int,
        default=None,
        help="Total number of shards. Requires --shard-index.",
    )

    p_hk = sub.add_parser(
        "hf-derive-ktx2",
        help="Transcode an existing HF PNG tar → KTX2 (requires `toktx`).",
    )
    p_hk.add_argument("source", choices=SOURCES)
    p_hk.add_argument("work_dir")
    p_hk.add_argument("--source-tier", default="1k", choices=VALID_TIERS)
    p_hk.add_argument("--target-tier", default=None, help="Default: ktx2-<source-tier>.")
    p_hk.add_argument("--release-tag", required=True)
    p_hk.add_argument("--repo-id", default="gerchowl/mat-vis")
    p_hk.add_argument("--dry-run", action="store_true")
    p_hk.add_argument(
        "--shard-index",
        type=int,
        default=None,
        help="0-based shard index. Requires --shard-total.",
    )
    p_hk.add_argument(
        "--shard-total",
        type=int,
        default=None,
        help="Total number of shards. Requires --shard-index.",
    )

    p_merge = sub.add_parser(
        "merge-shards",
        help="Reassemble shard-N-of-K artifacts into one tar + rowmap (#134).",
    )
    p_merge.add_argument("source", choices=SOURCES)
    p_merge.add_argument(
        "tier",
        help="Tier name (e.g. '1k', 'ktx2-1k'). KTX2 tiers land under ktx2/.",
    )
    p_merge.add_argument("work_dir")
    p_merge.add_argument("--release-tag", required=True)
    p_merge.add_argument("--repo-id", default="gerchowl/mat-vis")
    p_merge.add_argument("--dry-run", action="store_true")
    p_merge.add_argument(
        "--keep-shards",
        action="store_true",
        help="Don't delete shard artifacts after merge (useful for debugging).",
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
    if args.command == "merge-shards":
        return cmd_merge_shards(args)
    if args.command == "audit-orphans":
        return cmd_audit_orphans(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
