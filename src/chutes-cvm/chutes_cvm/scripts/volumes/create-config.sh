#!/usr/bin/env bash
# create-config-volume.sh - Create or refresh a config qcow2 for TDX VMs
# Usage: ./create-config.sh <output-path> <hostname> <miner-ss58> <miner-seed> <vm-ip> <vm-gateway> [vm-dns] [docker-hub-user] [docker-hub-token]
# If output-path exists, the image is mounted and config files are replaced in place (stop QEMU if it holds the file open).
# Example: ./create-config.sh config.qcow2 chutes-miner "ss58_value" "seed_value" 192.168.100.2 192.168.100.1
# With Docker Hub (optional 8th/9th args, requires vm-dns as 7th): ... 8.8.8.8 "hubuser" "dckr_pat_..."

set -euo pipefail

# Max 16 chars for filesystem label
LABEL="tdx-config"

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

populate_config_into_mount() {
    local MOUNT_DIR="$1"
    print_info "  Clearing all entries at volume root..."
    if ! find "$MOUNT_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +; then
        print_error "Failed to clear config volume"
        exit 1
    fi

    echo "$HOSTNAME" > "$MOUNT_DIR/hostname"
    print_info "  ✓ hostname: $HOSTNAME"

    if [[ -n "$MINER_SS58" && -n "$MINER_SEED" ]]; then
        echo "$MINER_SS58" > "$MOUNT_DIR/miner-ss58"
        echo "$MINER_SEED" > "$MOUNT_DIR/miner-seed"
        chmod 600 "$MOUNT_DIR/miner-ss58" "$MOUNT_DIR/miner-seed"
        print_info "  ✓ miner credential files"
    else
        print_info "  - miner credentials empty, skipping (benchmark mode)"
    fi

    cat > "$MOUNT_DIR/network-config.yaml" << EOF
network:
  version: 2
  ethernets:
    any-ethernet:
      match:
        name: "en*"
      addresses:
        - ${VM_IP}/24
      routes:
        - to: default
          via: ${VM_GATEWAY}
      nameservers:
        addresses:
          - ${VM_DNS}
EOF
    print_info "  ✓ network-config.yaml (${VM_IP} via ${VM_GATEWAY})"

    chmod 644 "$MOUNT_DIR/hostname" "$MOUNT_DIR/network-config.yaml"

    if [[ -n "$DOCKER_HUB_USER" && -n "$DOCKER_HUB_TOKEN" ]]; then
        printf '%s' "$DOCKER_HUB_USER" > "$MOUNT_DIR/docker-hub-username"
        printf '%s' "$DOCKER_HUB_TOKEN" > "$MOUNT_DIR/docker-hub-token"
        chmod 600 "$MOUNT_DIR/docker-hub-username" "$MOUNT_DIR/docker-hub-token"
        print_info "  ✓ Docker Hub credential files"
    fi

}

# Check for help flag
if [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then
    cat << EOF
Usage: $0 <output-path> [hostname] [miner-ss58] [miner-seed] [vm-ip] [vm-gateway] [vm-dns] [docker-hub-user] [docker-hub-token]

Create a new config qcow2, or refresh an existing one (same path, ext4 label $LABEL).

Every value may be given as a positional arg OR its same-named env var (the positional
wins when present, else the env var, else the default) — so callers can export a few env
vars and invoke with just <output-path> instead of a long positional list.

Values (positional order = env var name):
  output-path / OUTPUT_PATH    qcow2 path (created if missing; refreshed in place otherwise)
  hostname    / HOSTNAME       VM hostname
  miner-ss58  / MINER_SS58     Miner SS58 credential
  miner-seed  / MINER_SEED     Miner seed credential
  vm-ip       / VM_IP          VM IP address
  vm-gateway  / VM_GATEWAY     VM gateway IP
  vm-dns      / VM_DNS         VM DNS server (default: 8.8.8.8)
  docker-hub-user  / DOCKER_HUB_USER   Optional Docker Hub username (with token)
  docker-hub-token / DOCKER_HUB_TOKEN  Optional Docker Hub PAT/password (with user)

Examples:
  $0 config.qcow2 chutes-miner "5abc..." "seed123" 192.168.100.2 192.168.100.1
  HOSTNAME=chutes-miner MINER_SS58="5abc..." MINER_SEED=seed VM_IP=192.168.100.2 \\
    VM_GATEWAY=192.168.100.1 $0 config.qcow2

The volume will contain:
  /hostname         - VM hostname
  /miner-ss58       - Miner SS58 credential
  /miner-seed       - Miner seed credential
  /network-config.yaml - Netplan network configuration
  /docker-hub-username, /docker-hub-token - optional Docker Hub credentials (mode 0600)

The volume will be formatted with:
  - Filesystem: ext4
  - Label: $LABEL (required by TDX VMs)
  - Size: 10M (small, just for config files)

Requirements:
  - qemu-img (for creating qcow2 images)
  - qemu-nbd (for mounting qcow2 as block device)
  - mkfs.ext4 (for formatting)
  - Root/sudo access (for NBD operations)
  - NBD kernel module loaded
EOF
    exit 0
fi

# Config values come from a positional arg OR the same-named environment variable —
# the positional wins when given, else the env var, else the default. This lets a caller
# (the launch orchestrator, chutes_cvm.guest.launch) set a handful of env vars and invoke with just the output path instead
# of a long, order-fragile positional list, while direct/manual callers can still pass
# everything positionally. See --help for the full name list.
OUTPUT_PATH="${1:-${OUTPUT_PATH:-}}"
HOSTNAME="${2:-${HOSTNAME:-}}"
MINER_SS58="${3:-${MINER_SS58:-}}"
MINER_SEED="${4:-${MINER_SEED:-}}"
VM_IP="${5:-${VM_IP:-}}"
VM_GATEWAY="${6:-${VM_GATEWAY:-}}"
VM_DNS="${7:-${VM_DNS:-8.8.8.8}}"
DOCKER_HUB_USER="${8:-${DOCKER_HUB_USER:-}}"
DOCKER_HUB_TOKEN="${9:-${DOCKER_HUB_TOKEN:-}}"

if [[ -z "$OUTPUT_PATH" ]]; then
    print_error "Output path is required (arg 1 or \$OUTPUT_PATH)"
    echo "Usage: $0 <output-path> [hostname] [miner-ss58] [miner-seed] [vm-ip] [vm-gateway] [vm-dns] [docker-hub-user] [docker-hub-token]"
    echo "  Any value may instead be supplied via its same-named env var. Run '$0 --help'."
    exit 1
fi

# Basic validation
if [[ -z "$HOSTNAME" || -z "$VM_IP" || -z "$VM_GATEWAY" || -z "$VM_DNS" ]]; then
    print_error "Hostname, VM IP, gateway, and DNS must be non-empty"
    exit 1
fi

if [[ -n "$MINER_SS58" && -z "$MINER_SEED" ]] || [[ -z "$MINER_SS58" && -n "$MINER_SEED" ]]; then
    print_error "Miner SS58 and seed must both be provided or both be empty"
    exit 1
fi

if [[ -n "$DOCKER_HUB_USER" || -n "$DOCKER_HUB_TOKEN" ]]; then
    if [[ -z "$DOCKER_HUB_USER" || -z "$DOCKER_HUB_TOKEN" ]]; then
        print_error "Docker Hub username and token must both be provided when using Hub auth"
        exit 1
    fi
fi

# Validate hostname (basic check)
if ! [[ "$HOSTNAME" =~ ^[a-zA-Z0-9-]+$ ]]; then
    print_error "Invalid hostname format: $HOSTNAME"
    echo "Hostname must contain only letters, numbers, and hyphens"
    exit 1
fi

EXISTING_VOLUME=false
if [ -f "$OUTPUT_PATH" ]; then
    EXISTING_VOLUME=true
elif [ -e "$OUTPUT_PATH" ]; then
    print_error "Path exists and is not a regular file: $OUTPUT_PATH"
    exit 1
else
    ensure_parent_directory "$OUTPUT_PATH"
fi

# Check for required commands
REQUIRED_CMDS=(qemu-nbd blkid)
if [ "$EXISTING_VOLUME" != true ]; then
    REQUIRED_CMDS=(qemu-img qemu-nbd mkfs.ext4 blkid)
fi
for cmd in "${REQUIRED_CMDS[@]}"; do
    if ! command -v "$cmd" &> /dev/null; then
        print_error "Required command not found: $cmd"
        case "$cmd" in
            qemu-img|qemu-nbd)
                echo "Install QEMU tools: sudo apt-get install qemu-utils"
                ;;
            mkfs.ext4|blkid)
                echo "Install filesystem tools: sudo apt-get install e2fsprogs"
                ;;
        esac
        exit 1
    fi
done

# Check if running as root (needed for NBD operations)
if [ "$EUID" -ne 0 ]; then
    print_error "This script must be run with sudo/root privileges"
    echo "Try: sudo $0 \"$OUTPUT_PATH\" \"$HOSTNAME\" \"$MINER_SS58\" \"$MINER_SEED\" \"$VM_IP\" \"$VM_GATEWAY\" \"$VM_DNS\""
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

# Cleanup function
cleanup() {
    if [ -n "$NBD_DEVICE" ]; then
        print_info "Cleaning up NBD connection..."
        qemu-nbd --disconnect "$NBD_DEVICE" &> /dev/null || true
    fi
}

trap cleanup EXIT

_image_real_path() {
    readlink -f "$1" 2>/dev/null || echo "$1"
}

# Disconnect stale qemu-nbd exports of the target image left behind by an
# interrupted run. A long-lived qemu-nbd on the config image is always stale:
# the running VM opens the qcow2 with qemu-system directly, never via qemu-nbd.
# Returns 0 if at least one stale holder was cleared (retry worthwhile), else 1.
recover_stale_nbd_lock() {
    local target="$1" real base cleared=1 pid line dev
    real=$(_image_real_path "$target")
    base=$(basename "$target")
    while read -r pid line; do
        [[ -z "$pid" ]] && continue
        dev=$(grep -oE '/dev/nbd[0-9]+' <<<"$line" | head -1 || true)
        print_warning "Clearing stale qemu-nbd (pid $pid${dev:+, $dev}) holding $base"
        if [[ -n "$dev" ]]; then
            qemu-nbd --disconnect "$dev" &>/dev/null || true
        fi
        kill "$pid" 2>/dev/null || true
        cleared=0
    done < <(pgrep -af qemu-nbd 2>/dev/null | awk -v r="$real" -v b="$base" 'index($0, r) || index($0, b) {print $1" "$0}')
    [[ $cleared -eq 0 ]] && sleep 1
    return $cleared
}

# Report what still holds the image open so the operator can act, distinguishing
# a stale tool from a live guest VM that must be stopped (or the host rebooted).
report_image_holders() {
    local target="$1" real
    real=$(_image_real_path "$target")
    print_error "Something is still holding $target open (qcow2 write lock)."
    if command -v lsof &>/dev/null; then
        print_info "Open file handles (lsof):"
        lsof -- "$real" 2>/dev/null || lsof 2>/dev/null | grep -F "$(basename "$target")" || true
    elif command -v fuser &>/dev/null; then
        print_info "Open file handles (fuser):"
        fuser -v "$real" 2>&1 || true
    fi
    print_info ""
    print_info "If a qemu-system / qemu-kvm process is listed, the guest VM is still running."
    print_info "Stop it first (chutes-cvm guest down). If it will not die (D-state),"
    print_info "reboot the host before retrying."
}

if [ "$EXISTING_VOLUME" = true ]; then
    print_info ""
    print_info "Updating existing config volume (replace files in place)..."
    print_info "  Path: $OUTPUT_PATH"
else
    print_info ""
    print_info "Step 1/5: Creating qcow2 config volume..."
    print_info "  Path: $OUTPUT_PATH"
    print_info "  Size: 10M"

    if ! qemu-img create -f qcow2 "$OUTPUT_PATH" 10M; then
        print_error "Failed to create qcow2 image"
        exit 1
    fi

    print_success "qcow2 image created"
fi

print_info ""
if [ "$EXISTING_VOLUME" = true ]; then
    print_info "Step 1/3: Connecting to NBD device..."
else
    print_info "Step 2/5: Connecting to NBD device..."
fi

if ! qemu-nbd --connect="$NBD_DEVICE" "$OUTPUT_PATH"; then
    # Most common cause: a previous run left a stale qemu-nbd holding the image
    # (write-lock collision). Try to clear it and reconnect once before failing.
    print_warning "NBD connect failed — checking for a stale lock on $OUTPUT_PATH"
    if recover_stale_nbd_lock "$OUTPUT_PATH" && qemu-nbd --connect="$NBD_DEVICE" "$OUTPUT_PATH"; then
        print_success "Recovered stale lock and connected"
    else
        print_error "Failed to connect qcow2 to NBD device"
        report_image_holders "$OUTPUT_PATH"
        if [ "$EXISTING_VOLUME" != true ]; then
            rm -f "$OUTPUT_PATH"
        fi
        exit 1
    fi
fi

sleep 1

if [ ! -b "$NBD_DEVICE" ]; then
    print_error "NBD device not available after connection"
    exit 1
fi

print_success "Connected to $NBD_DEVICE"

if [ "$EXISTING_VOLUME" != true ]; then
    print_info ""
    print_info "Step 3/5: Formatting with ext4..."
    print_info "  Label: $LABEL"

    if ! mkfs.ext4 -L "$LABEL" "$NBD_DEVICE"; then
        print_error "Failed to format device"
        exit 1
    fi

    print_success "Formatted with ext4"
fi

print_info ""
if [ "$EXISTING_VOLUME" = true ]; then
    print_info "Step 2/3: Populating config files..."
else
    print_info "Step 4/5: Populating config files..."
fi

MOUNT_DIR="/tmp/tdx-config-mount-$$"
mkdir -p "$MOUNT_DIR"

if ! mount "$NBD_DEVICE" "$MOUNT_DIR"; then
    print_error "Failed to mount config volume"
    rmdir "$MOUNT_DIR" 2>/dev/null || true
    exit 1
fi

populate_config_into_mount "$MOUNT_DIR"

sync
umount "$MOUNT_DIR"
rmdir "$MOUNT_DIR"

if [ "$EXISTING_VOLUME" = true ]; then
    print_success "Config files written and volume unmounted"
else
    print_success "Config files created and volume unmounted"
fi

print_info ""
if [ "$EXISTING_VOLUME" = true ]; then
    print_info "Step 3/3: Verifying volume..."
else
    print_info "Step 5/5: Verifying volume..."
fi

FS_INFO=$(blkid -o export "$NBD_DEVICE" 2>/dev/null || true)
FS_TYPE=$(echo "$FS_INFO" | grep '^TYPE=' | cut -d= -f2 || echo "unknown")
FS_LABEL=$(echo "$FS_INFO" | grep '^LABEL=' | cut -d= -f2 || echo "none")

if [ "$FS_TYPE" != "ext4" ]; then
    print_error "Filesystem type verification failed: expected ext4, got $FS_TYPE"
    exit 1
fi

if [ "$FS_LABEL" != "$LABEL" ]; then
    print_error "Label verification failed: expected $LABEL, got $FS_LABEL"
    exit 1
fi

print_success "Volume verified successfully"

qemu-nbd --disconnect "$NBD_DEVICE" &> /dev/null

print_info ""
if [ "$EXISTING_VOLUME" = true ]; then
    print_success "Config volume updated successfully!"
else
    print_success "Config volume created successfully!"
fi
print_info ""
print_info "Volume details:"
print_info "  Path: $OUTPUT_PATH"
if [ "$EXISTING_VOLUME" != true ]; then
    print_info "  Size: 10M"
fi
print_info "  Filesystem: ext4"
print_info "  Label: $LABEL"
print_info ""
print_info "Config files:"
print_info "  /hostname         : $HOSTNAME"
print_info "  /miner-ss58       : [credential file]"
print_info "  /miner-seed       : [credential file]"
print_info "  /network-config.yaml : ${VM_IP} via ${VM_GATEWAY}"
if [[ -n "$DOCKER_HUB_USER" && -n "$DOCKER_HUB_TOKEN" ]]; then
    print_info "  /docker-hub-username : [credential file]"
    print_info "  /docker-hub-token    : [credential file]"
fi
print_info ""
print_info "To use with chutes-cvm guest launch:"
print_info "  chutes-cvm guest launch --config-volume $OUTPUT_PATH [other options...]"
print_info ""
print_info "To verify the volume contents later:"
print_info "  sudo qemu-nbd --connect=/dev/nbd0 $OUTPUT_PATH"
print_info "  sudo mkdir /mnt/verify && sudo mount /dev/nbd0 /mnt/verify"
print_info "  sudo ls -la /mnt/verify/"
print_info "  sudo umount /mnt/verify && sudo qemu-nbd --disconnect /dev/nbd0"

exit 0
