#!/usr/bin/env bash
# Run multiple Bake workflow chunks sequentially.
#
# Usage:
#   ./scripts/bake-chunks.sh <source> <tier> <total> <chunk_size> [release-tag]
#
# Example:
#   ./scripts/bake-chunks.sh ambientcg 2k 1965 100 v2026.04.0
#   → triggers 20 workflow runs (offset 0, 100, 200, ...) of 100 materials each
#
# Each run uploads its parquet partitions to the release. Manifest is
# rebuilt at the end of each run. Sequential execution avoids GH
# concurrency limits.

set -euo pipefail

source="${1:-}"
tier="${2:-}"
total="${3:-}"
chunk="${4:-100}"
tag="${5:-v2026.04.0}"

if [[ -z "$source" || -z "$tier" || -z "$total" ]]; then
    echo "usage: $0 <source> <tier> <total> [chunk_size=100] [release-tag=v2026.04.0]" >&2
    exit 2
fi

n_chunks=$(( (total + chunk - 1) / chunk ))
echo "Triggering $n_chunks chunks of $chunk materials each ($source $tier → $tag)"
echo

for ((i=0; i<n_chunks; i++)); do
    offset=$(( i * chunk ))
    echo "=== chunk $((i+1))/$n_chunks: offset=$offset limit=$chunk ==="

    # Note: workflow doesn't expose offset yet — needs adding to bake.yml
    # For now this requires a workflow that supports offset
    gh workflow run bake.yml --ref main \
        -f source="$source" \
        -f tier="$tier" \
        -f limit="$chunk" \
        -f offset="$offset" \
        -f release-tag="$tag" || { echo "trigger failed"; exit 1; }

    sleep 5
    run_id=$(gh run list --workflow=bake.yml --limit 1 --json databaseId --jq '.[0].databaseId')
    echo "run id: $run_id"
    echo "waiting for completion..."

    while true; do
        s=$(gh run view "$run_id" --json status,conclusion --jq '.status + ":" + (.conclusion // "")' 2>/dev/null || echo "error:")
        if [[ "$s" != "in_progress:" && "$s" != "queued:" ]]; then
            echo "chunk $((i+1)) result: $s"
            if [[ "$s" != "completed:success" ]]; then
                echo "FAIL — chunk $((i+1)) did not succeed, aborting"
                exit 1
            fi
            break
        fi
        sleep 60
    done
done

echo
echo "All $n_chunks chunks complete."
