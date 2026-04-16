#!/usr/bin/env bash
# Fetch a single texture map from mat-vis via HTTP range read.
# Usage: ./example.sh <parquet_url> <offset> <length> <output_file>
set -euo pipefail
curl -sH "Range: bytes=${2}-$(($2 + $3 - 1))" "$1" -o "$4"
echo "Wrote $4 ($3 bytes from offset $2)"
