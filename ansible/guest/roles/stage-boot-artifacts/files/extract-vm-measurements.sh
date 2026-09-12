#!/usr/bin/env bash
set -euo pipefail

IMG="${1:-}"
OUT_DIR="measure/boot"

if [[ -z "$IMG" ]]; then
  echo "Usage: $0 <path-to-qcow2>"
  exit 1
fi

if [[ ! -f "$IMG" ]]; then
  echo "ERROR: Image not found: $IMG"
  exit 1
fi

mkdir -p "$OUT_DIR"

echo "=== TDX Boot Artifact Extraction ==="
echo "Image: $IMG"
echo

echo "==> Detecting the boot filesystem (carries vmlinuz/initrd/grub)..."
# Match the ext4 partition that actually holds the versioned kernel, not just "any ext4".
# On an encrypted (prod) image the root is LUKS, so only the unencrypted /boot partition
# is ext4 — but on an unencrypted (debug) image BOTH the root and /boot partitions are
# ext4, so a bare /ext4/ match returns two devices and the later mount fails. The kernel
# (vmlinuz-*) lives only on the /boot partition, so pick by that.
ROOT_PART=""
for part in $(
  guestfish --ro -a "$IMG" <<'LIST' | awk '/ext4/ {sub(/:$/, "", $1); print $1}'
run
list-filesystems
LIST
); do
  if guestfish --ro -a "$IMG" <<CHECK 2>/dev/null | grep -q 'vmlinuz-'
run
mount $part /
ls /
CHECK
  then
    ROOT_PART="$part"
    break
  fi
done

if [[ -z "$ROOT_PART" ]]; then
  echo "ERROR: Could not find an ext4 partition containing the kernel (vmlinuz-*)."
  exit 1
fi

echo "Found boot partition: $ROOT_PART"
echo

#
# 1. Extract vmlinuz and initrd.img
#

echo "==> Extracting kernel and initrd..."

# guestfish has no if/then/else or $() substitution, so pick the filenames in bash from a
# directory listing, then download them by exact path. On the /boot partition the kernel
# and initrd are versioned (vmlinuz-* / initrd.img-*); the bare /vmlinuz symlink lives on
# the root fs, not here.
boot_ls=$(guestfish --ro -a "$IMG" run : mount "$ROOT_PART" / : ls /)

_pick() {  # $1 = base (vmlinuz|initrd.img): prefer a bare symlink, else the newest versioned file
  if grep -qx "$1" <<<"$boot_ls"; then
    printf '%s\n' "$1"
  else
    grep -E "^$1-" <<<"$boot_ls" | sort -V | tail -n1
  fi
}

vmlinuz=$(_pick vmlinuz)
initrd=$(_pick initrd.img)
if [[ -z "$vmlinuz" || -z "$initrd" ]]; then
  echo "ERROR: could not locate kernel/initrd on $ROOT_PART (contents: $(echo "$boot_ls" | tr '\n' ' '))" >&2
  exit 1
fi

guestfish --ro -a "$IMG" \
  run : \
  mount "$ROOT_PART" / : \
  download "/$vmlinuz" "$OUT_DIR/vmlinuz" : \
  download "/$initrd" "$OUT_DIR/initrd.img" : \
  download /grub/grub.cfg "$OUT_DIR/grub.cfg"

echo "✓ Extracted kernel → $OUT_DIR/vmlinuz"
echo "✓ Extracted initrd → $OUT_DIR/initrd.img"
echo "✓ Extracted grub.cfg → $OUT_DIR/grub.cfg"
echo

#
# 2. Parse kernel cmdline
#

echo "==> Parsing kernel cmdline..."
CMDLINE=$(grep -E "^[[:space:]]*linux" "$OUT_DIR/grub.cfg" \
  | head -n 1 \
  | sed -E 's/^[[:space:]]*linux[[:space:]]+[^[:space:]]+[[:space:]]+//'
)

if [[ -z "$CMDLINE" ]]; then
  echo "ERROR: Could not parse kernel cmdline"
  exit 1
fi

echo "$CMDLINE" > "$OUT_DIR/cmdline.txt"
echo "✓ Extracted cmdline → $OUT_DIR/cmdline.txt"

echo
echo "=== Extraction Complete ==="
