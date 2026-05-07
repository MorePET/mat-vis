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

    # KTX2 fallback: serve a 404 on .png by removing it; .ktx2 must take over.
    rm -rf "$cache"
    cache=$(mktemp -d)
    export MAT_VIS_CACHE="$cache"
    rm -f "$mockroot/$TAG/ambientcg/1k/Rock064/color.png"
    {
        printf '\xab\x4b\x54\x58\x20\x32\x30\xbb\r\n\x1a\n'
        head -c 1500 /dev/zero
    } > "$mockroot/$TAG/ambientcg/1k/Rock064/color.ktx2"
    "$CLIENT" fetch ambientcg Rock064 color 1k > "$cache/ktx2.out"
    local k_magic
    k_magic=$(head -c4 "$cache/ktx2.out" | od -An -tx1 | tr -d ' \n')
    assert_eq "fetch falls back to .ktx2 when .png 404s" "$k_magic" "ab4b5458"
    # restore for further tests
    rm -f "$mockroot/$TAG/ambientcg/1k/Rock064/color.ktx2"
    {
        printf '\x89PNG\r\n\x1a\n'
        head -c 1500 /dev/zero
    } > "$mockroot/$TAG/ambientcg/1k/Rock064/color.png"

    # Magic-byte rejection: .png served with bogus magic must be rejected.
    rm -rf "$cache"
    cache=$(mktemp -d)
    export MAT_VIS_CACHE="$cache"
    head -c 64 /dev/zero > "$mockroot/$TAG/ambientcg/1k/Rock064/color.png"
    assert_fails "bogus magic rejected" \
        "$CLIENT" fetch ambientcg Rock064 color 1k
    {
        printf '\x89PNG\r\n\x1a\n'
        head -c 1500 /dev/zero
    } > "$mockroot/$TAG/ambientcg/1k/Rock064/color.png"

    # Pre-v3 manifest must be rejected loudly — the v0.6 client doesn't speak tar.
    rm -rf "$cache"
    cache=$(mktemp -d)
    export MAT_VIS_CACHE="$cache"
    cat > "$mockroot/$TAG/release-manifest.json" <<EOF
{ "schema_version": 2, "release_tag": "$TAG", "tiers": { "1k": { "base_url": "https://example/" } } }
EOF
    assert_fails "pre-v3 manifest rejected" "$CLIENT" list
    cat > "$mockroot/$TAG/release-manifest.json" <<EOF
{
  "schema_version": 3,
  "release_tag": "$TAG",
  "sources": {
    "ambientcg": {
      "catalog": "ambientcg.json",
      "tiers": { "1k": { "complete": true } }
    }
  }
}
EOF

    unset MAT_VIS_HF_BASE MAT_VIS_TAG MAT_VIS_CACHE
}

# ── default tag (#242; structural — no network) ───────────────────

default_tag_tests() {
    echo "=== default tag (#242) ==="
    # The script defaults MAT_VIS_TAG to a real CalVer release so a
    # tag-less invocation against prod HF returns real data. We assert
    # the literal here so an accidental flip back to "main" is caught
    # without any network round-trip.
    local default_line
    default_line=$(grep -E '^DEFAULT_TAG=' "$CLIENT" | head -1)
    assert_contains "DEFAULT_TAG is v2026.04.2" "$default_line" 'DEFAULT_TAG="v2026.04.2"'

    # The MAT_VIS_TAG fallback must reference DEFAULT_TAG (not "main").
    local tag_line
    tag_line=$(grep -E '^TAG=' "$CLIENT" | head -1)
    # shellcheck disable=SC2016  # asserting on the literal source text, not expansion
    assert_contains "TAG falls back to DEFAULT_TAG" "$tag_line" '${MAT_VIS_TAG:-$DEFAULT_TAG}'

    # help shows the new default in the env doc block.
    local help_out
    help_out=$(MAT_VIS_TAG="" "$CLIENT" help 2>&1 || true)
    assert_contains "help mentions v2026.04.2 default" "$help_out" "v2026.04.2"
}

# ── live default tag (#242; opt-in via MAT_VIS_LIVE_TESTS=1) ───────

live_default_tag_tests() {
    if [ "${MAT_VIS_LIVE_TESTS:-0}" != "1" ]; then
        echo "=== live default tag (#242; skipped; set MAT_VIS_LIVE_TESTS=1) ==="
        return
    fi
    echo "=== live default tag (#242; HF round-trip with no MAT_VIS_TAG) ==="
    # Unset MAT_VIS_TAG so the client falls back to DEFAULT_TAG.
    local cache
    cache=$(mktemp -d)
    export MAT_VIS_CACHE="$cache"
    unset MAT_VIS_TAG
    trap 'rm -rf "$cache"' RETURN

    local list_out
    if ! list_out=$("$CLIENT" list 2>&1); then
        echo "  FAIL default-tag list failed: $list_out"
        FAIL=$((FAIL + 1))
        return
    fi
    assert_contains "default-tag list includes 1k tier" "$list_out" "1k"
    assert_contains "default-tag list includes ambientcg" "$list_out" "ambientcg"

    unset MAT_VIS_CACHE
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
    # #297: avoid `echo "$mats" | head -1` — under `set -o pipefail`, when
    # $mats exceeds the kernel pipe buffer (64 KiB on Linux) `head` exits
    # after one line, the producer SIGPIPEs, and the script aborts 141.
    IFS= read -r first <<< "$mats" || true
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

    # #248: manifest-driven per-(source, tier) coverage.
    # Single manifest GET drives every (source, tier) marked
    # complete=true. Empty material lists are expected on some sources
    # (e.g. gpuopen on v2026.04.x) and skipped, not failed.
    local manifest_url manifest_json
    manifest_url="https://huggingface.co/datasets/gerchowl/mat-vis/resolve/$MAT_VIS_TAG/release-manifest.json"
    if ! manifest_json=$(curl -sfL -H "User-Agent: mat-vis-shell-test" "$manifest_url"); then
        echo "  FAIL #248: failed to fetch manifest from $manifest_url"
        FAIL=$((FAIL + 1))
    else
        local pairs attempted=0 skipped_empty=0 fails_248=0
        # Emit "<source> <tier>" lines for every complete=true pair.
        pairs=$(echo "$manifest_json" | jq -r \
            '.sources | to_entries[] | . as $s |
             ($s.value.tiers // {}) | to_entries[] |
             select(.value.complete == true) |
             "\($s.key) \(.key)"')
        while IFS=' ' read -r src tier; do
            [ -n "$src" ] || continue
            local mats first
            if ! mats=$("$CLIENT" materials "$src" "$tier" 2>/dev/null); then
                echo "  FAIL #248: materials lookup failed for ($src, $tier)"
                FAIL=$((FAIL + 1)); fails_248=$((fails_248 + 1))
                continue
            fi
            # #297: see comment at the structural-tests call site —
            # `echo "$mats" | head -1` SIGPIPEs under pipefail when the
            # producer exceeds the pipe buffer.
            IFS= read -r first <<< "$mats" || true
            if [ -z "$first" ]; then
                # Documented expected: some sources publish a tier but
                # carry no records with that tier in their catalog.
                skipped_empty=$((skipped_empty + 1))
                continue
            fi
            local out_248="$cache/_248_${src}_${tier}.bin"
            if ! "$CLIENT" fetch "$src" "$first" color "$tier" -o "$out_248" >/dev/null 2>&1; then
                echo "  FAIL #248: fetch failed for ($src, $tier, $first)"
                FAIL=$((FAIL + 1)); fails_248=$((fails_248 + 1))
                continue
            fi
            local m4
            m4=$(head -c4 "$out_248" | od -An -tx1 | tr -d ' \n')
            case "$m4" in
                89504e47|ab4b5458)
                    attempted=$((attempted + 1))
                    ;;
                *)
                    echo "  FAIL #248: ($src, $tier, $first, magic_bytes_hex_first_4=$m4) — expected PNG (89504e47) or KTX2 (ab4b5458) magic"
                    FAIL=$((FAIL + 1)); fails_248=$((fails_248 + 1))
                    ;;
            esac
        done <<< "$pairs"
        if [ "$fails_248" -eq 0 ] && [ "$attempted" -gt 0 ]; then
            echo "  PASS #248 manifest-driven coverage: $attempted (source,tier) pairs fetched, $skipped_empty empty pair(s) skipped"
            PASS=$((PASS + 1))
        elif [ "$fails_248" -eq 0 ] && [ "$attempted" -eq 0 ]; then
            echo "  FAIL #248: no complete (source,tier) pair yielded a non-empty material list (skipped_empty=$skipped_empty)"
            FAIL=$((FAIL + 1))
        fi
    fi
}

# ── e2e: opt-in via MAT_VIS_E2E=1 (mat-vis-tst per-file substrate, #193)
#
# Round-trips against the throwaway scratch dataset
# `gerchowl/mat-vis-tst`. Gated on MAT_VIS_E2E=1.
#
# Ordering contract (#193): the Python E2E suite at
# tests/e2e/test_per_file_roundtrip.py owns the bake/cleanup lifecycle
# for the throwaway tag (default `v0.0.0-e2e-184-perfile`). This block
# presumes that suite has already run (or is running in the same CI
# job) so the tag exists. Operators run:
#
#   MAT_VIS_E2E=1 pytest tests/e2e/      # bakes the tag
#   MAT_VIS_E2E=1 bash clients/test_client.sh   # rides on it
#
# We do NOT bake from shell — keeping the bake side-effect single-owner
# avoids racy commits to mat-vis-tst.

cmd_e2e() {
    if [ "${MAT_VIS_E2E:-0}" != "1" ]; then
        echo "=== e2e (skipped; set MAT_VIS_E2E=1 + a per-file mat-vis-tst tag) ==="
        return
    fi
    echo "=== e2e (mat-vis-tst HF round-trip, #193) ==="

    local repo="gerchowl/mat-vis-tst"
    export MAT_VIS_HF_BASE="https://huggingface.co/datasets/$repo/resolve"
    export MAT_VIS_TAG="${MAT_VIS_E2E_TAG:-v0.0.0-e2e-184-perfile}"
    local cache
    cache=$(mktemp -d)
    export MAT_VIS_CACHE="$cache"
    trap 'rm -rf "$cache"' RETURN

    local mats first
    if ! mats=$("$CLIENT" materials polyhaven 1k 2>&1); then
        echo "  FAIL materials lookup failed: $mats"
        FAIL=$((FAIL + 1))
        return
    fi
    # #297: same pipefail+SIGPIPE hazard as the structural call site.
    IFS= read -r first <<< "$mats" || true
    if [ -z "$first" ]; then
        echo "  FAIL polyhaven materials list empty (did the Python E2E suite bake the tag?)"
        FAIL=$((FAIL + 1))
        return
    fi
    echo "  PASS polyhaven materials non-empty (first=$first)"
    PASS=$((PASS + 1))

    local out="$cache/e2e.png"
    if ! "$CLIENT" fetch polyhaven "$first" color 1k -o "$out" 2>&1; then
        echo "  FAIL fetch failed for $first"
        FAIL=$((FAIL + 1))
        return
    fi
    local size
    size=$(wc -c < "$out" | tr -d ' ')
    if [ "$size" -gt 1000 ]; then
        echo "  PASS fetch wrote PNG bytes ($size B)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL fetch produced too-small file ($size B)"
        FAIL=$((FAIL + 1))
        return
    fi
    local magic
    magic=$(head -c4 "$out" | od -An -tx1 | tr -d ' \n')
    assert_eq "fetch emits PNG magic" "$magic" "89504e47"

    unset MAT_VIS_HF_BASE MAT_VIS_TAG MAT_VIS_CACHE
}

structural_tests
default_tag_tests
live_default_tag_tests
live_tests
cmd_e2e

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
