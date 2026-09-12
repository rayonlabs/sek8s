#!/usr/bin/env bash
# publish-image.sh — Upload a built guest image + its direct-boot artifacts to R2.
#
# Uploads the versioned local build outputs to the canonical R2 object names that
# vm.chutes.ai serves and `chutes-cvm image download` fetches. The build writes each set
# into its own directory, guest-tools/image/<env>/<version>[-debug]/:
#   <set>/<version>[-debug].qcow2   -> <bucket>/tdx-guest[-debug].qcow2
#   <set>/<version>[-debug].vmlinuz -> <bucket>/tdx-guest[-debug].vmlinuz
#   <set>/<version>[-debug].initrd  -> <bucket>/tdx-guest[-debug].initrd
#   <set>/<version>[-debug].cmdline -> <bucket>/tdx-guest[-debug].cmdline
#   <set>/manifest.json (generated) -> <bucket>/tdx-guest[-debug].manifest.json
#
# The .vmlinuz/.initrd/.cmdline are produced by stage-boot-artifacts.sh during the
# build; all four must travel together so the fleet boots byte-identical bits. The
# manifest (sha256 + size per artifact, keyed by role) is the coherence contract that
# ties the set together — `chutes-cvm image download` verifies against it, so a stale or
# mismatched artifact fails loudly instead of as an opaque boot/attestation error.
#
# rclone remote "r2" must be configured. If the rclone config is password
# protected, export RCLONE_CONFIG_PASS (otherwise rclone prompts on each call).
#
# Usage: publish-image.sh [--debug] [--env <env>] [--version <ver>] [--bucket r2:chutes-tdx]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEBUG=false
ENV="prod"
VERSION="$(head -1 "$REPO_ROOT/ansible/guest/VERSION" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+')"
BUCKET="r2:chutes-tdx"

while [ $# -gt 0 ]; do
    case "$1" in
        --debug) DEBUG=true; shift ;;
        --env) ENV="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --bucket) BUCKET="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

command -v rclone >/dev/null 2>&1 || { echo "ERROR: rclone not found" >&2; exit 1; }

SUFFIX=""
REMOTE="tdx-guest"
if [ "$DEBUG" = true ]; then
    SUFFIX="-debug"
    REMOTE="tdx-guest-debug"
fi
# This image set's own directory (one per version AND variant, so prod and debug each have
# their own manifest.json), and the shared basename of the four artifacts inside it.
IMAGE_SET_DIR="$REPO_ROOT/guest-tools/image/$ENV/${VERSION}${SUFFIX}"
LOCAL_BASE="$IMAGE_SET_DIR/${VERSION}${SUFFIX}"
ARTIFACTS=(qcow2 vmlinuz initrd cmdline)

# Pre-flight: all four must exist so we never publish a qcow2 without its matching
# boot artifacts (which would leave the fleet unable to direct-boot).
for ext in "${ARTIFACTS[@]}"; do
    src="$LOCAL_BASE.$ext"
    [ -f "$src" ] || {
        echo "ERROR: missing $src" >&2
        echo "  (build the image; stage-boot-artifacts.sh produces the .vmlinuz/.initrd/.cmdline)" >&2
        exit 1
    }
done

# Generate the coherence manifest with the single generator (chutes_cvm.guest.image_set), the
# same one the build and the launcher use, so the schema never drifts. It hashes the qcow2
# and its <base>.{vmlinuz,initrd,cmdline} sidecars.
MANIFEST="$IMAGE_SET_DIR/manifest.json"
echo "Generating manifest -> $MANIFEST"
DEBUG_FLAG=()
[ "$DEBUG" = true ] && DEBUG_FLAG=(--debug)
PYTHONPATH="$REPO_ROOT/src/chutes-cvm" python3 -m chutes_cvm.guest.image_set manifest \
    "$LOCAL_BASE.qcow2" -o "$MANIFEST" --version "$VERSION" "${DEBUG_FLAG[@]}"

echo "Publishing ${VERSION}${SUFFIX} ($ENV) -> $BUCKET/$REMOTE.*"
for ext in "${ARTIFACTS[@]}"; do
    src="$LOCAL_BASE.$ext"
    dst="$BUCKET/$REMOTE.$ext"
    echo "==> $src -> $dst"
    rclone copyto --progress --s3-chunk-size 64M --transfers 4 "$src" "$dst"
done

# Publish the manifest last, so it never advertises a set that isn't fully uploaded yet.
echo "==> $MANIFEST -> $BUCKET/$REMOTE.manifest.json"
rclone copyto --progress "$MANIFEST" "$BUCKET/$REMOTE.manifest.json"

echo "✓ Published ${VERSION}${SUFFIX} (image + direct-boot artifacts + manifest)"
