#!/usr/bin/env bash
# mat-vis reference client — curl + jq only.
#
# Usage:
#   mat-vis.sh list                                 # list sources × tiers
#   mat-vis.sh materials ambientcg 1k               # list material IDs
#   mat-vis.sh fetch ambientcg Rock064 color 1k     # fetch PNG → stdout
#   mat-vis.sh fetch ambientcg Rock064 color 1k -o rock.png
#
# Environment:
#   MAT_VIS_TAG     — release tag (default: latest)
#   MAT_VIS_CACHE   — cache directory (default: ~/.cache/mat-vis)

set -euo pipefail

REPO="MorePET/mat-vis"
RELEASES="https://github.com/$REPO/releases"
TAG="${MAT_VIS_TAG:-latest}"
CACHE="${MAT_VIS_CACHE:-$HOME/.cache/mat-vis}"
UA="mat-vis-client/0.1 (shell)"

# ── helpers ──────────────────────────────────────────────────────

die() { echo "error: $*" >&2; exit 1; }

fetch_json() {
    local url=$1 cache_file=$2
    if [ -f "$cache_file" ]; then
        cat "$cache_file"
        return
    fi
    mkdir -p "$(dirname "$cache_file")"
    curl -sfL -H "User-Agent: $UA" "$url" -o "$cache_file" || die "Failed to fetch $url"
    cat "$cache_file"
}

manifest_url() {
    if [ "$TAG" = "latest" ]; then
        echo "$RELEASES/latest/download/release-manifest.json"
    else
        echo "$RELEASES/download/$TAG/release-manifest.json"
    fi
}

get_manifest() {
    fetch_json "$(manifest_url)" "$CACHE/.manifest.json"
}

# ── commands ─────────────────────────────────────────────────────

cmd_list() {
    get_manifest | jq -r '.tiers | to_entries[] | "\(.key): \(.value.sources | keys | join(", "))"'
}

cmd_materials() {
    local source=${1:?source required} tier=${2:-1k}
    local manifest
    manifest=$(get_manifest)

    local base_url rowmap_file rowmap_url
    base_url=$(echo "$manifest" | jq -r ".tiers[\"$tier\"].base_url")
    rowmap_file=$(echo "$manifest" | jq -r ".tiers[\"$tier\"].sources[\"$source\"].rowmap_files[0]")

    [ "$rowmap_file" = "null" ] && die "No rowmap for $source/$tier"

    rowmap_url="${base_url}${rowmap_file}"
    fetch_json "$rowmap_url" "$CACHE/.rowmaps/$rowmap_file" | jq -r '.materials | keys[]' | sort
}

cmd_fetch() {
    local source=${1:?source required}
    local material=${2:?material required}
    local channel=${3:?channel required}
    local tier=${4:-1k}
    local output=""

    # Parse -o flag
    shift 4 || true
    while [ $# -gt 0 ]; do
        case "$1" in
            -o) output="$2"; shift 2 ;;
            *) die "Unknown flag: $1" ;;
        esac
    done

    # Check cache
    local cache_file="$CACHE/$source/$tier/$material/${channel}.png"
    if [ -f "$cache_file" ]; then
        if [ -n "$output" ]; then
            cp "$cache_file" "$output"
            echo "Cached: $output ($(wc -c < "$cache_file") bytes)" >&2
        else
            cat "$cache_file"
        fi
        return
    fi

    # Get manifest + rowmap
    local manifest
    manifest=$(get_manifest)
    local base_url rowmap_file
    base_url=$(echo "$manifest" | jq -r ".tiers[\"$tier\"].base_url")
    rowmap_file=$(echo "$manifest" | jq -r ".tiers[\"$tier\"].sources[\"$source\"].rowmap_files[0]")
    [ "$rowmap_file" = "null" ] && die "No rowmap for $source/$tier"

    local rowmap
    rowmap=$(fetch_json "${base_url}${rowmap_file}" "$CACHE/.rowmaps/$rowmap_file")

    # Get offset + length
    local offset length parquet_file
    offset=$(echo "$rowmap" | jq -r ".materials[\"$material\"][\"$channel\"].offset")
    length=$(echo "$rowmap" | jq -r ".materials[\"$material\"][\"$channel\"].length")
    parquet_file=$(echo "$rowmap" | jq -r ".parquet_file")

    [ "$offset" = "null" ] && die "$material/$channel not found in rowmap"

    local parquet_url="${base_url}${parquet_file}"
    local range_end=$((offset + length - 1))

    # Range read
    mkdir -p "$(dirname "$cache_file")"
    curl -sfL -H "User-Agent: $UA" -H "Range: bytes=${offset}-${range_end}" "$parquet_url" -o "$cache_file" \
        || die "Range read failed: $parquet_url"

    # Verify PNG
    local magic
    magic=$(head -c4 "$cache_file" | xxd -p)
    [ "$magic" = "89504e47" ] || die "Not a PNG (got $magic)"

    if [ -n "$output" ]; then
        cp "$cache_file" "$output"
        echo "Fetched: $output ($length bytes)" >&2
    else
        cat "$cache_file"
    fi
}

# ── dispatch ─────────────────────────────────────────────────────

case "${1:-help}" in
    list)       cmd_list ;;
    materials)  shift; cmd_materials "$@" ;;
    fetch)      shift; cmd_fetch "$@" ;;
    *)
        echo "mat-vis client — fetch PBR textures via HTTP range reads"
        echo ""
        echo "Usage:"
        echo "  mat-vis.sh list                                 List sources × tiers"
        echo "  mat-vis.sh materials <source> [tier]            List materials"
        echo "  mat-vis.sh fetch <source> <id> <channel> [tier] [-o file]"
        echo ""
        echo "Environment:"
        echo "  MAT_VIS_TAG     Release tag (default: latest)"
        echo "  MAT_VIS_CACHE   Cache dir (default: ~/.cache/mat-vis)"
        ;;
esac
