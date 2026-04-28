#!/usr/bin/env bash
# mat-vis reference client — curl + jq only.
#
# Substrate: per-file HF dataset (#186 / ADR-0012). One curl per
# texture; no rowmap, no tar, no range read.
#
# Usage:
#   mat-vis.sh list                                 # list sources × tiers
#   mat-vis.sh materials ambientcg 1k               # list material IDs
#   mat-vis.sh fetch ambientcg Rock064 color 1k     # fetch PNG → stdout
#   mat-vis.sh fetch ambientcg Rock064 color 1k -o rock.png
#
# Environment:
#   MAT_VIS_TAG     — release tag (default: main)
#   MAT_VIS_CACHE   — cache directory (default: ~/.cache/mat-vis)
#   MAT_VIS_HF_BASE — HF resolve URL prefix (default: prod)

set -euo pipefail

HF_DATASET="gerchowl/mat-vis"
HF_BASE="${MAT_VIS_HF_BASE:-https://huggingface.co/datasets/$HF_DATASET/resolve}"
TAG="${MAT_VIS_TAG:-main}"
CACHE="${MAT_VIS_CACHE:-$HOME/.cache/mat-vis}"
UA="mat-vis-client/0.6.0 (shell)"

# ── helpers ──────────────────────────────────────────────────────

die() { echo "error: $*" >&2; exit 1; }

hf_url() { echo "$HF_BASE/$TAG/$1"; }

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

get_manifest() {
    local m sv
    m=$(fetch_json "$(hf_url release-manifest.json)" "$CACHE/$TAG/.manifest.json")
    sv=$(echo "$m" | jq -r '.schema_version // empty')
    [ "$sv" = "3" ] || die "manifest schema_version=$sv (need 3 — per-file substrate, ADR-0012)"
    echo "$m"
}

# Fetch the catalog (ADR-0011 v3) for a source. Cached locally.
get_catalog() {
    local source=$1
    local manifest catalog_path
    manifest=$(get_manifest)
    catalog_path=$(echo "$manifest" | jq -r ".sources[\"$source\"].catalog // \"$source.json\"")
    fetch_json "$(hf_url "$catalog_path")" "$CACHE/$TAG/.catalogs/$catalog_path"
}

# Probe `<source>/<tier>/.tier_complete` once per (source, tier).
# ADR-0012: the sentinel is the final commit per tier — its presence
# is our atomicity guarantee; reject partial tiers loudly.
assert_tier_complete() {
    local source=$1 tier=$2
    local marker="$CACHE/$TAG/.sentinel/$source/$tier"
    [ -f "$marker" ] && return 0
    local url
    url=$(hf_url "$source/$tier/.tier_complete")
    if ! curl -sfL -H "User-Agent: $UA" -o /dev/null --head "$url"; then
        die "tier $source/$tier is not atomically complete on $TAG (no .tier_complete sentinel). Re-run the bake or pin a known-complete tag."
    fi
    mkdir -p "$(dirname "$marker")"
    : > "$marker"
}

# ── commands ─────────────────────────────────────────────────────

cmd_list() {
    get_manifest \
        | jq -r '.sources | to_entries[] | . as $s | ($s.value.tiers // {}) | keys[] | "\(.) \($s.key)"' \
        | sort -u \
        | awk '{ a[$1] = ($1 in a ? a[$1] ", " : "") $2 } END { for (t in a) print t ": " a[t] }' \
        | sort
}

cmd_materials() {
    local source=${1:?source required} tier=${2:-1k}
    get_catalog "$source" \
        | jq -r --arg tier "$tier" \
              '.[] | select((.available_tiers // []) | index($tier)) | .id' \
        | sort
}

emit() {
    local file=$1 output=$2 tag=$3
    if [ -n "$output" ]; then
        cp "$file" "$output"
        echo "$tag: $output ($(wc -c < "$file" | tr -d ' ') bytes)" >&2
    else
        cat "$file"
    fi
}

cmd_fetch() {
    local source=${1:?source required}
    local material=${2:?material required}
    local channel=${3:?channel required}
    local tier=${4:-1k}
    local output=""
    shift 4 || true
    while [ $# -gt 0 ]; do
        case "$1" in
            -o) output="$2"; shift 2 ;;
            *) die "Unknown flag: $1" ;;
        esac
    done

    local cache_dir="$CACHE/$TAG/$source/$tier/$material"
    for ext in png ktx2; do
        [ -f "$cache_dir/${channel}.${ext}" ] && {
            emit "$cache_dir/${channel}.${ext}" "$output" "Cached"
            return
        }
    done

    assert_tier_complete "$source" "$tier"
    mkdir -p "$cache_dir"
    local cache_file=""
    for ext in png ktx2; do
        local cf="$cache_dir/${channel}.${ext}"
        if curl -sfL -H "User-Agent: $UA" "$(hf_url "$source/$tier/$material/${channel}.${ext}")" -o "$cf"; then
            cache_file="$cf"; break
        fi
        rm -f "$cf"
    done
    [ -n "$cache_file" ] || die "$material/$channel not found at $source/$tier (tried .png and .ktx2)"

    local magic
    magic=$(head -c4 "$cache_file" | od -An -tx1 | tr -d ' \n')
    case "$magic" in
        89504e47|ab4b5458) ;;
        *) rm -f "$cache_file"; die "Not PNG or KTX2 (got $magic)" ;;
    esac

    emit "$cache_file" "$output" "Fetched"
}

# ── dispatch ─────────────────────────────────────────────────────

case "${1:-help}" in
    list)       cmd_list ;;
    materials)  shift; cmd_materials "$@" ;;
    fetch)      shift; cmd_fetch "$@" ;;
    *)
        echo "mat-vis client — per-file PBR texture fetch via curl + jq"
        echo ""
        echo "Usage:"
        echo "  mat-vis.sh list                                 List sources × tiers"
        echo "  mat-vis.sh materials <source> [tier]            List materials"
        echo "  mat-vis.sh fetch <source> <id> <channel> [tier] [-o file]"
        echo ""
        echo "Environment:"
        echo "  MAT_VIS_TAG     Release tag (default: main)"
        echo "  MAT_VIS_CACHE   Cache dir (default: ~/.cache/mat-vis)"
        echo "  MAT_VIS_HF_BASE HF resolve URL (default: prod)"
        ;;
esac
