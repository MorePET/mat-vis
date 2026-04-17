#!/usr/bin/env bash
# Tests for the shell reference client against live release data.
#
# Requires: curl, jq, xxd (usually in vim or xxd package)
# Run with: bash test_client.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLIENT="$SCRIPT_DIR/mat-vis.sh"

export MAT_VIS_TAG="${MAT_VIS_TAG:-v2026.04.0}"
export MAT_VIS_CACHE
MAT_VIS_CACHE="$(mktemp -d)"

trap 'rm -rf "$MAT_VIS_CACHE"' EXIT

PASS=0
FAIL=0

assert_ok() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "  PASS $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL $desc"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local desc="$1" output="$2" needle="$3"
    if echo "$output" | grep -q "$needle"; then
        echo "  PASS $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL $desc — expected '$needle' in output"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== manifest ==="

LIST_OUTPUT=$("$CLIENT" list)
assert_contains "list includes 1k tier" "$LIST_OUTPUT" "1k"
assert_contains "list includes ambientcg" "$LIST_OUTPUT" "ambientcg"

echo "=== materials ==="

MATERIALS=$("$CLIENT" materials ambientcg 1k)
MAT_COUNT=$(echo "$MATERIALS" | wc -l | tr -d ' ')
FIRST_MAT=$(echo "$MATERIALS" | head -1)

if [ "$MAT_COUNT" -gt 0 ] && [ -n "$FIRST_MAT" ]; then
    echo "  PASS materials list non-empty ($MAT_COUNT materials)"
    PASS=$((PASS + 1))
else
    echo "  FAIL materials list empty"
    FAIL=$((FAIL + 1))
fi

echo "=== fetch texture ==="

TEXTURE_FILE="$MAT_VIS_CACHE/test_output.png"
"$CLIENT" fetch ambientcg "$FIRST_MAT" color 1k -o "$TEXTURE_FILE"

# Verify file exists and is non-trivial
if [ -f "$TEXTURE_FILE" ] && [ "$(wc -c < "$TEXTURE_FILE")" -gt 1000 ]; then
    echo "  PASS fetch wrote file ($(wc -c < "$TEXTURE_FILE") bytes)"
    PASS=$((PASS + 1))
else
    echo "  FAIL fetch did not produce valid file"
    FAIL=$((FAIL + 1))
fi

# Verify PNG magic bytes
MAGIC=$(head -c4 "$TEXTURE_FILE" | xxd -p)
if [ "$MAGIC" = "89504e47" ]; then
    echo "  PASS PNG magic bytes verified"
    PASS=$((PASS + 1))
else
    echo "  FAIL expected PNG magic 89504e47, got $MAGIC"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
