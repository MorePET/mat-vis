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
#   MAT_VIS_TAG     — release tag (default: $DEFAULT_TAG, see #242)
#   MAT_VIS_CACHE   — cache directory (default: ~/.cache/mat-vis)
#   MAT_VIS_HF_BASE — HF resolve URL prefix (default: prod)

set -euo pipefail

HF_DATASET="gerchowl/mat-vis"
HF_BASE="${MAT_VIS_HF_BASE:-https://huggingface.co/datasets/$HF_DATASET/resolve}"
# Default tag when MAT_VIS_TAG is unset (#242). The dataset's `main`
# branch is an empty baseline — every release lives on a CalVer branch
# — so a tag-less invocation must default to a real release. Keep in
# lockstep with the Python/JS/Rust clients' DEFAULT_TAG.
DEFAULT_TAG="v2026.04.2"
TAG="${MAT_VIS_TAG:-$DEFAULT_TAG}"
CACHE="${MAT_VIS_CACHE:-$HOME/.cache/mat-vis}"
UA="mat-vis-client/0.6.3 (shell)"

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
    # Issue #258 — manifest cache validates against the origin per
    # invocation via a conditional GET. Body + ETag are stored side-by-
    # side under $CACHE/$TAG/.manifest.{json,etag}; on 304 the cached
    # body is served, on 200 both are replaced atomically. Falls back
    # to unconditional GET when no .manifest.etag is on disk (cold
    # start or after a cache prune) so a stale etag can't lock us out.
    local body_path etag_path url etag http_code body sv
    body_path="$CACHE/$TAG/.manifest.json"
    etag_path="$CACHE/$TAG/.manifest.etag"
    url=$(hf_url release-manifest.json)
    mkdir -p "$(dirname "$body_path")"

    etag=""
    if [ -f "$etag_path" ] && [ -f "$body_path" ]; then
        etag=$(cat "$etag_path")
    fi

    local tmp_body tmp_headers
    tmp_body=$(mktemp)
    tmp_headers=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmp_body' '$tmp_headers'" RETURN

    local curl_status
    if [ -n "$etag" ]; then
        http_code=$(curl -sL -o "$tmp_body" -D "$tmp_headers" \
            -w '%{http_code}' \
            -H "User-Agent: $UA" \
            -H "If-None-Match: $etag" \
            "$url")
        curl_status=$?
    else
        http_code=$(curl -sL -o "$tmp_body" -D "$tmp_headers" \
            -w '%{http_code}' \
            -H "User-Agent: $UA" \
            "$url")
        curl_status=$?
    fi
    [ "$curl_status" -eq 0 ] || die "Failed to fetch $url"

    # 000 = non-HTTP scheme (file://, used by structural tests). Treat
    # as 200 when curl exited cleanly: there's no ETag semantics on
    # local files, so we always overwrite the body cache.
    if [ "$http_code" = "304" ] && [ -f "$body_path" ]; then
        body=$(cat "$body_path")
    elif [ "$http_code" = "200" ] || [ "$http_code" = "000" ]; then
        cp "$tmp_body" "$body_path"
        # Extract latest ETag header (case-insensitive, last wins on
        # redirect chains). Strip CR + surrounding whitespace.
        local new_etag
        new_etag=$(awk 'BEGIN{IGNORECASE=1} /^etag:/ {sub(/^[Ee][Tt][Aa][Gg]:[ \t]*/, ""); sub(/\r$/, ""); val=$0} END{print val}' "$tmp_headers")
        if [ -n "$new_etag" ]; then
            printf '%s' "$new_etag" > "$etag_path"
        else
            # Defensive: server gave no ETag — drop any stale file so
            # next invocation refetches unconditionally.
            rm -f "$etag_path"
        fi
        body=$(cat "$body_path")
    else
        die "Failed to fetch $url (HTTP $http_code)"
    fi

    sv=$(echo "$body" | jq -r '.schema_version // empty')
    [ "$sv" = "3" ] || die "manifest schema_version=$sv (need 3 — per-file substrate, ADR-0012)"
    echo "$body"
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
    # Use GET (not HEAD) to mirror the Python client's sentinel
    # probe — HF's CDN is documented to behave consistently for GET
    # but HEAD has occasionally diverged (auth gates, byte-counts).
    if ! curl -sfL -H "User-Agent: $UA" -o /dev/null "$url"; then
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
        echo "Note: tier defaults to '1k' here. The 'auto'/'best' sentinels"
        echo "      (mat-vis#374) are Python-client only — bash callers must"
        echo "      pass an explicit tier name."
        echo ""
        echo "Environment:"
        echo "  MAT_VIS_TAG     Release tag (default: $DEFAULT_TAG)"
        echo "  MAT_VIS_CACHE   Cache dir (default: ~/.cache/mat-vis)"
        echo "  MAT_VIS_HF_BASE HF resolve URL (default: prod)"
        ;;
esac
