#!/usr/bin/env bash
# Tests for the shell reference client.
#
# Two modes:
#   1. Structural — always run. Stubs HF behind a local file:// tree
#      and exercises mat-vis.sh end-to-end. No network.
#   2. Live — gated on MAT_VIS_LIVE_TESTS=1. Hits prod HF; default-skipped
#      because v0.6 dropped tar support and prod isn't rebaked under
#      the per-file substrate yet (#179).
#
# Requires: curl, jq, od (tested on macOS + alpine).
# Run with: bash test_client.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLIENT="$SCRIPT_DIR/mat-vis.sh"

PASS=0
FAIL=0

assert_eq() {
    local desc="$1" actual="$2" expected="$3"
    if [ "$actual" = "$expected" ]; then
        echo "  PASS $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL $desc — expected '$expected', got '$actual'"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local desc="$1" output="$2" needle="$3"
    if echo "$output" | grep -q -- "$needle"; then
        echo "  PASS $desc"
        PASS=$((PASS + 1))
    else
        printf "  FAIL %s — expected '%s' in output:\n%s\n" "$desc" "$needle" "$output"
        FAIL=$((FAIL + 1))
    fi
}

assert_fails() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "  FAIL $desc — expected non-zero exit"
        FAIL=$((FAIL + 1))
    else
        echo "  PASS $desc"
        PASS=$((PASS + 1))
    fi
}

# ── structural: stub HF behind a file:// tree ──────────────────────

setup_mock_hf() {
    local root=$1 tag=$2
    local td="$root/$tag"
    mkdir -p "$td/ambientcg/1k/Rock064"

    cat > "$td/release-manifest.json" <<EOF
{
  "schema_version": 3,
  "release_tag": "$tag",
  "sources": {
    "ambientcg": {
      "catalog": "ambientcg.json",
      "tiers": { "1k": { "complete": true } }
    }
  }
}
EOF
    cat > "$td/ambientcg.json" <<'EOF'
[
  {
    "id": "Rock064",
    "source": "ambientcg",
    "mat_vis": { "name": "Rock064", "category": "stone" },
    "available_tiers": ["1k"],
    "maps": ["color", "normal"]
  }
]
EOF
    # PNG: 8-byte magic + 1.5KiB filler so it clears 'wc -c > 1000' checks
    {
        printf '\x89PNG\r\n\x1a\n'
        head -c 1500 /dev/zero
    } > "$td/ambientcg/1k/Rock064/color.png"
    : > "$td/ambientcg/1k/.tier_complete"
}

structural_tests() {
    echo "=== structural (stubbed HF tree) ==="
    local mockroot
    mockroot=$(mktemp -d)
    local cache
    cache=$(mktemp -d)
    trap 'rm -rf "$mockroot" "$cache"' RETURN

    local TAG="vtest"
    setup_mock_hf "$mockroot" "$TAG"

    export MAT_VIS_HF_BASE="file://$mockroot"
    export MAT_VIS_TAG="$TAG"
    export MAT_VIS_CACHE="$cache"

    # list
    local list_out
    list_out=$("$CLIENT" list)
    assert_contains "list shows 1k tier" "$list_out" "1k"
    assert_contains "list includes ambientcg" "$list_out" "ambientcg"

    # materials
    local mats
    mats=$("$CLIENT" materials ambientcg 1k)
    assert_eq "materials returns Rock064" "$mats" "Rock064"

    # fetch — stdout
    local fetched_size
    "$CLIENT" fetch ambientcg Rock064 color 1k > "$cache/out.png"
    fetched_size=$(wc -c < "$cache/out.png" | tr -d ' ')
    if [ "$fetched_size" -gt 100 ]; then
        echo "  PASS fetch wrote PNG bytes ($fetched_size B)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL fetch produced too-small file ($fetched_size B)"
        FAIL=$((FAIL + 1))
    fi
    local magic
    magic=$(head -c4 "$cache/out.png" | od -An -tx1 | tr -d ' \n')
    assert_eq "fetch emits PNG magic" "$magic" "89504e47"

    # cache hit on second fetch
    "$CLIENT" fetch ambientcg Rock064 color 1k > "$cache/out2.png"
    if cmp -s "$cache/out.png" "$cache/out2.png"; then
        echo "  PASS cache hit yields identical bytes"
        PASS=$((PASS + 1))
    else
        echo "  FAIL cache hit yields different bytes"
        FAIL=$((FAIL + 1))
    fi

    # missing-sentinel rejection
    rm -rf "$cache"
    cache=$(mktemp -d)
    export MAT_VIS_CACHE="$cache"
    rm -f "$mockroot/$TAG/ambientcg/1k/.tier_complete"
    assert_fails "missing sentinel rejects fetch" \
        "$CLIENT" fetch ambientcg Rock064 color 1k

    : > "$mockroot/$TAG/ambientcg/1k/.tier_complete"

    unset MAT_VIS_HF_BASE MAT_VIS_TAG MAT_VIS_CACHE
}

# ── live: opt-in via MAT_VIS_LIVE_TESTS=1 ──────────────────────────

live_tests() {
    if [ "${MAT_VIS_LIVE_TESTS:-0}" != "1" ]; then
        echo "=== live (skipped; set MAT_VIS_LIVE_TESTS=1 + a per-file MAT_VIS_LIVE_TAG) ==="
        return
    fi
    echo "=== live (HF round-trip) ==="
    export MAT_VIS_TAG="${MAT_VIS_LIVE_TAG:-v2026.04.1}"
    local cache
    cache=$(mktemp -d)
    export MAT_VIS_CACHE="$cache"
    trap 'rm -rf "$cache"' RETURN

    local list_out
    list_out=$("$CLIENT" list)
    assert_contains "list includes 1k tier" "$list_out" "1k"
    assert_contains "list includes ambientcg" "$list_out" "ambientcg"

    local mats first
    mats=$("$CLIENT" materials ambientcg 1k)
    first=$(echo "$mats" | head -1)
    [ -n "$first" ] || { echo "  FAIL materials list empty"; FAIL=$((FAIL + 1)); return; }
    echo "  PASS materials list non-empty (first=$first)"
    PASS=$((PASS + 1))

    local out="$cache/test_output.png"
    "$CLIENT" fetch ambientcg "$first" color 1k -o "$out"
    if [ -f "$out" ] && [ "$(wc -c < "$out" | tr -d ' ')" -gt 1000 ]; then
        echo "  PASS fetch wrote file ($(wc -c < "$out" | tr -d ' ') bytes)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL fetch did not produce valid file"
        FAIL=$((FAIL + 1))
    fi
    local magic
    magic=$(head -c4 "$out" | od -An -tx1 | tr -d ' \n')
    case "$magic" in
        89504e47|ab4b5458) echo "  PASS PNG/KTX2 magic verified ($magic)"; PASS=$((PASS + 1)) ;;
        *) echo "  FAIL unexpected magic: $magic"; FAIL=$((FAIL + 1)) ;;
    esac
}

structural_tests
live_tests

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
