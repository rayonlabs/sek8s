#!/bin/bash
# reset-gpus.sh - Reset all GPUs via nvidia-gpu-tools Secondary Bus Reset.
#
# Ensures no VM is running before resetting to prevent corrupting active
# workloads. The VM must be stopped gracefully before running this.
#
# Usage:
#   sudo ./devices/reset-gpus.sh
#   chutes-cvm host reset-gpus   # via PATH (after host setup)

set -euo pipefail

PROCESS_NAME="chutes-td"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--help]

Reset all NVIDIA GPUs via Secondary Bus Reset (SBR) using nvidia-gpu-tools.

The VM process ($PROCESS_NAME) must not be running during reset.
SBR resets clear GPU state and fabric configuration, which would
corrupt any active workloads.

To stop the VM gracefully first:
  chutes-miner tee shutdown --ip <HOST_IP> --confirm
  chutes-miner tee shutdown --name <SERVER_NAME> --confirm
EOF
}

case "${1:-}" in
    --help|-h)
        usage
        exit 0
        ;;
    "")
        ;;
    *)
        echo "Unknown option: $1"
        usage
        exit 1
        ;;
esac

# Non-zombie QEMU whose cmdline includes this guest name (-name / process=).
_live_chutes_td_qemu_running() {
    local pid state cmdline
    while read -r pid; do
        [[ -z "$pid" ]] && continue
        [[ -r "/proc/$pid/stat" ]] || continue
        state=$(ps -p "$pid" -o stat= 2>/dev/null || echo "")
        [[ "$state" == Z* ]] && continue
        cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
        if [[ "$cmdline" != *qemu-system* && "$cmdline" != *qemu-kvm* ]]; then
            continue
        fi
        [[ "$cmdline" == *"$PROCESS_NAME"* ]] || continue
        return 0
    done < <(
        { pgrep -f 'qemu-system' 2>/dev/null || true
          pgrep -f 'qemu-kvm' 2>/dev/null || true
        } | sort -un
    )
    return 1
}

if _live_chutes_td_qemu_running; then
    echo "Error: TDX VM (QEMU, $PROCESS_NAME) is running."
    echo ""
    echo "Stop the VM gracefully before resetting GPUs:"
    echo "  chutes-miner tee shutdown --ip <HOST_IP> --confirm"
    echo "  chutes-miner tee shutdown --name <SERVER_NAME> --confirm"
    exit 1
fi

CMD=$(which nvidia-gpu-tools 2>/dev/null || echo "")
if [[ -z "$CMD" ]]; then
    echo "Error: nvidia-gpu-tools not found in PATH."
    echo "It is installed automatically when chutes-cvm guest launch launches a VM,"
    echo "or install manually from the bundled wheel in chutes_cvm/scripts/gpu-tools/."
    exit 1
fi

# Abort if nvidia-gpu-tools processes are already running (likely stuck in D state).
STUCK_PIDS=$(pgrep -f 'nvidia-gpu-tools' 2>/dev/null | grep -v "^$$\$" || true)
if [[ -n "$STUCK_PIDS" ]]; then
    echo "Error: nvidia-gpu-tools process(es) already running:"
    ps -fp $STUCK_PIDS 2>/dev/null || true
    echo ""
    echo "These are likely stuck in uninterruptible sleep (D state) from a"
    echo "previous reset attempt. A host reboot is required to clear them."
    echo "Running another reset will also hang."
    exit 1
fi

GPU_TOOLS_TIMEOUT=120

# CC-mode Blackwell / RTX use --reset-after-cc-mode-switch; H200 PPCIe uses ppcie flag.
if lspci -Dnn 2>/dev/null | grep -qE '10de:(3182|2901|2bb1|2bb5)'; then
    SBR_ARGS=(--reset-with-sbr --reset-after-cc-mode-switch)
    SBR_LABEL="CC-mode Blackwell/RTX"
else
    SBR_ARGS=(--reset-with-sbr --reset-after-ppcie-mode-switch)
    SBR_LABEL="PPCIe (H200/H100)"
fi

echo "Resetting GPUs via Secondary Bus Reset (${SBR_LABEL}, timeout: ${GPU_TOOLS_TIMEOUT}s)..."
if ! timeout "$GPU_TOOLS_TIMEOUT" sudo "$CMD" "${SBR_ARGS[@]}"; then
    echo ""
    echo "Error: GPU reset timed out or failed after ${GPU_TOOLS_TIMEOUT}s."
    echo "GPU hardware may be wedged at the PCIe level."
    echo "A host reboot is likely required to recover."
    exit 1
fi
echo "GPU reset complete."

# PCI remove + rescan: clear stale kernel state (driver bindings, iommufd
# refs, AER errors) so the next VM launch starts with a clean device tree.
NVIDIA_VENDOR="10de"
GPU_BDFS=$(lspci -Dnn | grep "$NVIDIA_VENDOR" | awk '{print $1}')

if [[ -n "$GPU_BDFS" ]]; then
    echo "Removing PCI devices to clear kernel state..."
    for bdf in $GPU_BDFS; do
        remove_path="/sys/bus/pci/devices/${bdf}/remove"
        if [[ -w "$remove_path" ]]; then
            echo "  Removing $bdf"
            echo 1 > "$remove_path"
        fi
    done

    echo "Rescanning PCI bus..."
    echo 1 > /sys/bus/pci/rescan

    # Wait for devices to reappear (up to 5 seconds).
    for _attempt in $(seq 1 10); do
        all_back=true
        for bdf in $GPU_BDFS; do
            if [[ ! -d "/sys/bus/pci/devices/${bdf}" ]]; then
                all_back=false
                break
            fi
        done
        if $all_back; then
            echo "All devices reappeared after rescan."
            break
        fi
        sleep 0.5
    done
fi
