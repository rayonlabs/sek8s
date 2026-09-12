#!/usr/bin/env bash
# create-cache-volume.sh - Create and format a raw cache volume for TDX VMs
# Usage: ./create-cache.sh <output-path> <size> <label>
# Example: ./create-cache.sh cache-volume.raw 5000G tdx-cache
#
# Only raw format is supported for new volumes. Existing qcow2 volumes can
# still be used at VM launch but cannot be created by this script.

set -euo pipefail

LABEL=""

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_error() {
    echo -e "${RED}ERROR: $1${NC}" >&2
}

print_success() {
    echo -e "${GREEN}SUCCESS: $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}WARNING: $1${NC}"
}

print_info() {
    echo -e "$1"
}

ensure_parent_directory() {
    local target_path="$1"
    local parent_dir
    parent_dir=$(dirname "$target_path")

    if [[ -z "$parent_dir" ]] || [[ "$parent_dir" == "." ]]; then
        return 0
    fi

    if [ ! -d "$parent_dir" ]; then
        print_info "Creating directory: $parent_dir"
        if ! mkdir -p "$parent_dir"; then
            print_error "Failed to create directory: $parent_dir"
            exit 1
        fi
    fi
}

# Check for help flag
if [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then
    cat << EOF
Usage: $0 <output-path> <size> <label>

Create and format a raw cache volume for TDX VMs with the specified label.
Only raw format is supported for new volumes. Existing qcow2 volumes can
still be used at VM launch but cannot be created by this script.

Arguments:
  output-path    Path where the raw volume will be created (.raw or block device)
  size           Size of the volume (e.g., 5000G, 5T, 1000G)
  label          Filesystem label (required, max 12 chars for XFS)

Examples:
  $0 cache-volume.raw 5000G tdx-cache
  $0 /path/to/my-cache.raw 1T my-custom-label
  $0 /dev/vg0/tdx_cache 5000G tdx-cache

The volume will be formatted with:
  - Format: raw
  - Filesystem: XFS
  - Label: As specified

Requirements:
  - qemu-img (for creating raw images)
  - qemu-nbd (for exposing as block device)
  - mkfs.xfs (for formatting)
  - Root/sudo access (for NBD operations)
  - NBD kernel module loaded
EOF
    exit 0
fi

# Validate arguments
if [ $# -ne 3 ]; then
    print_error "Invalid number of arguments"
    echo "Usage: $0 <output-path> <size> <label>"
    echo "Example: $0 cache-volume.raw 5000G tdx-cache"
    echo "Run '$0 --help' for more information"
    exit 1
fi

OUTPUT_PATH="$1"
SIZE="$2"
LABEL="$3"

# Reject qcow2 paths - only raw is supported for new volumes
if [[ "$OUTPUT_PATH" == *.qcow2 ]]; then
    print_error "qcow2 format is not supported for new volumes. Use .raw extension."
    echo "Example: $0 cache-volume.raw $SIZE $LABEL"
    echo "Existing qcow2 volumes can still be used at VM launch."
    exit 1
fi

# Validate label length (XFS max is 12 chars)
if [ ${#LABEL} -gt 12 ]; then
    print_error "Label too long: $LABEL (max 12 characters for XFS)"
    exit 1
fi

# Validate size format
if ! [[ "$SIZE" =~ ^[0-9]+[KMGT]?$ ]]; then
    print_error "Invalid size format: $SIZE"
    echo "Size must be a number followed by optional unit (K, M, G, T)"
    echo "Examples: 5000G, 5T, 1000000M"
    exit 1
fi

# Check if output file already exists (skip for block devices - we format those in place)
if [ -f "$OUTPUT_PATH" ]; then
    print_error "Output file already exists: $OUTPUT_PATH"
    echo "Please remove it first or choose a different path"
    exit 1
fi

ensure_parent_directory "$OUTPUT_PATH"

# Check for required commands
for cmd in qemu-img qemu-nbd mkfs.xfs blkid; do
    if ! command -v "$cmd" &> /dev/null; then
        print_error "Required command not found: $cmd"
        case "$cmd" in
            qemu-img|qemu-nbd)
                echo "Install QEMU tools: sudo apt-get install qemu-utils"
                ;;
            mkfs.xfs|blkid)
                echo "Install filesystem tools: sudo apt-get install xfsprogs"
                ;;
        esac
        exit 1
    fi
done

# Check if running as root (needed for NBD operations)
if [ "$EUID" -ne 0 ]; then
    print_error "This script must be run with sudo/root privileges"
    echo "Try: sudo $0 $OUTPUT_PATH $SIZE"
    exit 1
fi

# Check if NBD module is loaded
if ! lsmod | grep -q '^nbd\s'; then
    print_info "Loading NBD kernel module..."
    if ! modprobe nbd max_part=8; then
        print_error "Failed to load NBD kernel module"
        echo "Ensure the nbd module is available in your kernel"
        exit 1
    fi
    print_success "NBD module loaded"
fi

# Find available NBD device
NBD_DEVICE=""
for i in {0..15}; do
    if [ -b "/dev/nbd$i" ] && ! qemu-nbd --list 2>/dev/null | grep -q "nbd$i"; then
        NBD_DEVICE="/dev/nbd$i"
        break
    fi
done

if [ -z "$NBD_DEVICE" ]; then
    print_error "No available NBD device found"
    echo "All NBD devices are in use. Try disconnecting unused devices:"
    echo "  sudo qemu-nbd --disconnect /dev/nbd0"
    exit 1
fi

print_info "Using NBD device: $NBD_DEVICE"

# Cleanup function (only disconnects if we used NBD)
cleanup() {
    if [ "${USE_NBD:-true}" = true ] && [ -n "${NBD_DEVICE:-}" ]; then
        print_info "Cleaning up NBD connection..."
        qemu-nbd --disconnect "$NBD_DEVICE" &> /dev/null || true
    fi
}

trap cleanup EXIT

USE_NBD=true
FORMAT_DEVICE="$NBD_DEVICE"

# Block device path (e.g. LVM LV): format directly, no NBD
if [[ "$OUTPUT_PATH" == /dev/* ]]; then
    if [ ! -b "$OUTPUT_PATH" ]; then
        print_error "Block device does not exist: $OUTPUT_PATH"
        exit 1
    fi
    USE_NBD=false
    FORMAT_DEVICE="$OUTPUT_PATH"
    print_info ""
    print_info "Using block device directly (no NBD): $OUTPUT_PATH"
fi

if [ "$USE_NBD" = true ]; then
    # Step 1: Create raw image file
    print_info ""
    print_info "Step 1/4: Creating raw image..."
    print_info "  Path: $OUTPUT_PATH"
    print_info "  Size: $SIZE"

    if ! qemu-img create -f raw -o preallocation=falloc "$OUTPUT_PATH" "$SIZE" 2>/dev/null; then
        # falloc may fail on some filesystems (e.g. NFS); try full
        if ! qemu-img create -f raw -o preallocation=full "$OUTPUT_PATH" "$SIZE"; then
            print_error "Failed to create raw image"
            exit 1
        fi
    fi

    print_success "Raw image created"

    # Step 2: Connect to NBD
    print_info ""
    print_info "Step 2/4: Connecting to NBD device..."

    if ! qemu-nbd --connect="$NBD_DEVICE" --format=raw "$OUTPUT_PATH"; then
        print_error "Failed to connect image to NBD device"
        rm -f "$OUTPUT_PATH"
        exit 1
    fi

    # Wait for device to be ready
    sleep 1

    if [ ! -b "$NBD_DEVICE" ]; then
        print_error "NBD device not available after connection"
        exit 1
    fi

    print_success "Connected to $NBD_DEVICE"
fi

# Step 3: Format with XFS and label
print_info ""
print_info "Step 3/4: Formatting with XFS..."
print_info "  Label: $LABEL"

if ! mkfs.xfs -L "$LABEL" "$FORMAT_DEVICE"; then
    print_error "Failed to format device"
    exit 1
fi

print_success "Formatted with XFS"

# Step 4: Verify
print_info ""
print_info "Step 4/4: Verifying volume..."

FS_INFO=$(blkid -o export "$FORMAT_DEVICE" 2>/dev/null || true)
FS_TYPE=$(echo "$FS_INFO" | grep '^TYPE=' | cut -d= -f2 || echo "unknown")
FS_LABEL=$(echo "$FS_INFO" | grep '^LABEL=' | cut -d= -f2 || echo "none")

if [ "$FS_TYPE" != "xfs" ]; then
    print_error "Filesystem type verification failed: expected xfs, got $FS_TYPE"
    exit 1
fi

if [ "$FS_LABEL" != "$LABEL" ]; then
    print_error "Label verification failed: expected $LABEL, got $FS_LABEL"
    exit 1
fi

print_success "Volume verified successfully"

# Disconnect NBD if we used it (cleanup trap also handles this)
if [ "$USE_NBD" = true ]; then
    qemu-nbd --disconnect "$NBD_DEVICE" &> /dev/null
fi

print_info ""
print_success "Cache volume created successfully!"
print_info ""
print_info "Volume details:"
print_info "  Path: $OUTPUT_PATH"
print_info "  Format: raw"
print_info "  Size: $SIZE"
print_info "  Filesystem: XFS"
print_info "  Label: $LABEL"
print_info ""
if [ "$LABEL" = "storage" ]; then
    print_info "Note: This volume is configured for VM storage (auto-encrypted at boot in production mode)"
fi
if [ "$USE_NBD" = true ]; then
    print_info ""
    print_info "To verify the volume:"
    print_info "  sudo qemu-nbd --connect=/dev/nbd0 --format=raw $OUTPUT_PATH"
    print_info "  sudo blkid /dev/nbd0"
    print_info "  sudo qemu-nbd --disconnect /dev/nbd0"
fi

exit 0