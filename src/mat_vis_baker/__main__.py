"""CLI entry point for the mat-vis baker.

Usage:
    mat-vis-baker fetch <source> <tier> <output_dir>
    mat-vis-baker bake <mtlx_dir> <output_dir>
    mat-vis-baker pack <baked_dir> <output.parquet>
    mat-vis-baker index <source> <output.json>
    mat-vis-baker all <source> <tier> <output_dir>

Called directly in release.yml. Not a user-facing tool.
"""
import sys


def main():
    print("mat-vis-baker: not yet implemented", file=sys.stderr)
    print(f"args: {sys.argv[1:]}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
