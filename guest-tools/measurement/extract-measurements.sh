#!/bin/bash
# extract-measurements.sh - Report a running TDX guest's measurements.
#
# Runs inside the guest: generates a fresh TDX quote and decodes MRTD + RTMR0-3
# from it. This is the "what are this VM's measurements right now?" tool — used to
# verify a running guest against the expected teeMeasurements block.
#
# This is distinct from capture-measurement-artifacts.sh, which captures the raw
# boot artifacts (CCEL, fw_cfg preimages) needed to *reproduce* measurements
# offline. This script only reads the finished measurements out of a quote.
#
# Usage: extract-measurements.sh [--json] [--out DIR]
#   --json   emit the decoded fields as JSON (default: human-readable)
#   --out    keep quote.bin + the decode in DIR (default: a temp dir, discarded)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

JSON=""
OUT_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --json) JSON="--json"; shift ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$OUT_DIR" ]; then
    OUT_DIR="$(mktemp -d)"
    trap 'rm -rf "$OUT_DIR"' EXIT
else
    mkdir -p "$OUT_DIR"
fi

# The quote is the source of truth for a running VM's measurements.
if ! command -v tdx-quote-generator >/dev/null 2>&1; then
    echo "ERROR: tdx-quote-generator not found — cannot produce a quote to read measurements from." >&2
    echo "       (For offline reproduction inputs use capture-measurement-artifacts.sh instead.)" >&2
    exit 1
fi
echo "Generating TDX quote..." >&2
tdx-quote-generator -o "$OUT_DIR/quote.bin"

# Locate the quote decoder next to this script (compiled from utils/extract_tdx_quote.c).
DECODER=""
for cand in "$SCRIPT_DIR/extract-tdx-quote" "$SCRIPT_DIR/utils/extract-tdx-quote"; do
    [ -x "$cand" ] && DECODER="$cand" && break
done
if [ -z "$DECODER" ]; then
    echo "ERROR: extract-tdx-quote decoder not found next to this script." >&2
    echo "       Build it: cc -O2 -o extract-tdx-quote utils/extract_tdx_quote.c" >&2
    exit 1
fi

( cd "$OUT_DIR" && "$DECODER" $JSON )
