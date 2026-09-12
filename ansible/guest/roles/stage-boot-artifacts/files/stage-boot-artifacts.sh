#!/usr/bin/env bash
# stage-boot-artifacts.sh — Extract the direct-boot kernel/initrd/cmdline from an
# image and persist them next to it as publishable artifacts:
#   <image-base>.vmlinuz   <image-base>.initrd   <image-base>.cmdline
#
# Produced once at build time (pre-encryption, plaintext image) and published to
# R2 alongside the qcow2, so every fleet host boots byte-identical kernel/initrd.
# Both consumers read these same files:
#   - compute-rtmr1-2.sh pins RTMR1/2 from them at build time
#   - the launcher (chutes_cvm.guest.direct_boot) boots them
# so the pinned measurements match the running VM by construction.
#
# The cmdline is the image's GRUB default entry minus the BOOT_IMAGE= prefix (what
# OVMF gets as -append). Nothing is extracted at launch.
#
# Usage: stage-boot-artifacts.sh <path-to-qcow2>
# Prerequisite on the build host: guestfish (libguestfs-tools).

set -euo pipefail

IMG="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -n "$IMG" ] || { echo "Usage: $0 <path-to-qcow2>" >&2; exit 1; }
[ -f "$IMG" ] || { echo "ERROR: image not found: $IMG" >&2; exit 1; }

BASE="${IMG%.*}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# extract-vm-measurements.sh writes to <cwd>/measure/boot/{vmlinuz,initrd.img,cmdline.txt}
( cd "$WORK" && bash "$SCRIPT_DIR/extract-vm-measurements.sh" "$IMG" )
BOOT="$WORK/measure/boot"

cp -f "$BOOT/vmlinuz" "$BASE.vmlinuz"
cp -f "$BOOT/initrd.img" "$BASE.initrd"
cp -f "$BOOT/cmdline.txt" "$BASE.cmdline"

echo "==> Staged direct-boot artifacts:" >&2
echo "      $BASE.vmlinuz ($(stat -c%s "$BASE.vmlinuz") bytes)" >&2
echo "      $BASE.initrd  ($(stat -c%s "$BASE.initrd") bytes)" >&2
echo "      $BASE.cmdline ($(cat "$BASE.cmdline"))" >&2
