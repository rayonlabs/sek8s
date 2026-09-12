#!/bin/bash
# download-image-set.sh — fetch a published base image set and verify it against its manifest.
#
# Invoked by `chutes-cvm image download [--debug]` (image_set.py _cmd_download). Downloads a full
# image set (1.4.0+) into its own per-variant directory under /var/lib/chutes/base-images/:
# the qcow2, the direct-boot kernel/initrd/cmdline OVMF boots directly, and the manifest
# that ties them together. `image verify --full` then verifies every downloaded byte
# against the manifest (the R2-published integrity source).
#
#   download-image-set.sh <base>      # base = tdx-guest | tdx-guest-debug
set -euo pipefail

BASE="${1:?usage: download-image-set.sh <tdx-guest|tdx-guest-debug>}"

if ! command -v aria2c >/dev/null 2>&1; then
  echo "Error: aria2c not found. Install with: sudo apt install aria2" >&2
  exit 1
fi

DIR="/var/lib/chutes/base-images/${BASE}"
sudo mkdir -p "$DIR"

# Download into a fixed per-variant directory (overwrites in place — keep an old build by
# moving its directory aside before re-downloading). Manifest last so a partial download
# never leaves a manifest advertising bytes that aren't there yet.
echo "Downloading ${BASE}.qcow2..."
aria2c -x 16 -s 16 -k 1M --allow-overwrite=true -d "$DIR" -o "${BASE}.qcow2" \
  "https://vm.chutes.ai/${BASE}.qcow2" || { echo "Download failed for ${BASE}.qcow2" >&2; exit 1; }
for ext in vmlinuz initrd cmdline; do
  echo "Downloading ${BASE}.${ext} (direct-boot artifact)..."
  aria2c -x 16 -s 16 -k 1M --allow-overwrite=true -d "$DIR" -o "${BASE}.${ext}" \
    "https://vm.chutes.ai/${BASE}.${ext}" || {
    echo "Download failed for ${BASE}.${ext}. It must be published alongside the qcow2 (1.4.0+)." >&2
    exit 1
  }
done
echo "Downloading manifest.json (coherence contract)..."
aria2c -x 16 -s 16 -k 1M --allow-overwrite=true -d "$DIR" -o "manifest.json" \
  "https://vm.chutes.ai/${BASE}.manifest.json" || {
  echo "Download failed for ${BASE}.manifest.json. It must be published alongside the qcow2 (1.4.0+)." >&2
  exit 1
}

echo "Verifying the downloaded image set against its manifest..."
chutes-cvm image verify --full "$DIR" >/dev/null || {
  echo "ERROR: downloaded image set failed manifest verification (see above)." >&2
  exit 1
}
echo "✓ Image set downloaded and verified: $DIR"
echo "  Point base_image at this directory (or leave it empty to use the default)."
