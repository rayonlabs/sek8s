#!/bin/bash
# teardown.sh — full teardown of a TEE VM environment (stop VM + bridge + benchmark-netlog).
#
# Invoked by `chutes-cvm guest down` (guest/cli.py _cmd_down), which resolves the network values from
# config in Python and passes them as flags so bridge cleanup uses the right PUBLIC_IFACE /
# BRIDGE_IP / VM_IP. Force-kills the VM (via `chutes-cvm guest stop --force`), tears the bridge down,
# and stops the benchmark-netlog service.
#
# For a VM-only stop that LEAVES the shared bridge in place (e.g. the measurement capture
# VM), use `chutes-cvm guest stop` directly instead of this.
#
#   teardown.sh [--bridge-ip IP/CIDR] [--vm-ip IP] [--public-iface IFACE] [--no-stop]
#
# --no-stop: skip the force-kill (`chutes-cvm guest stop --force`) — the caller already asked the guest
# to power off gracefully (chutes-cvm guest down); we still wait for it to exit, then clean up.
set -euo pipefail

# Defaults mirror the launch orchestrator (chutes_cvm.guest.launch; used when a flag is omitted).
VM_IP="192.168.100.2"
BRIDGE_IP="192.168.100.1/24"
PUBLIC_IFACE=""
NO_STOP="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bridge-ip) BRIDGE_IP="$2"; shift 2 ;;
    --vm-ip) VM_IP="$2"; shift 2 ;;
    --public-iface) PUBLIC_IFACE="$2"; shift 2 ;;
    --no-stop) NO_STOP="true"; shift ;;
    *) echo "teardown.sh: unknown argument '$1'" >&2; exit 1 ;;
  esac
done

# Resolve the public interface the same way the launch orchestrator does: empty or a stale NIC name
# falls back to the default-route device so bridge --clean removes the right iptables rules.
if [[ -z "$PUBLIC_IFACE" ]] || ! ip link show "$PUBLIC_IFACE" >/dev/null 2>&1; then
  DETECTED_IFACE=$(ip -j route show default 2>/dev/null \
    | python3 -c "import json,sys; r=json.load(sys.stdin); print(r[0]['dev'] if r else '')" \
    2>/dev/null || true)
  [[ -n "$DETECTED_IFACE" ]] && PUBLIC_IFACE="$DETECTED_IFACE"
fi

echo "=== Cleaning Up TEE VM Environment ==="
if [[ "$NO_STOP" == "true" ]]; then
  echo "Graceful shutdown already requested; waiting for the guest to power off..."
else
  echo "Force-stopping Chutes VM (if running)..."
  # --force: this is teardown's hard-kill step; `guest stop` alone now powers off gracefully.
  chutes-cvm guest stop --force 2>/dev/null || true
fi

echo "Waiting for VM processes to exit..."
for i in {1..15}; do
  if ! pgrep -f 'qemu-system|qemu-kvm|chutes-cvm' >/dev/null 2>&1; then
    echo "No VM processes found. Proceeding with bridge cleanup."
    break
  fi
  echo "VM processes still running; waiting... ($i/15)"
  sleep 1
done

./network/setup-bridge.sh --clean \
  --bridge-ip "$BRIDGE_IP" \
  --vm-ip "${VM_IP}/24" \
  --public-iface "$PUBLIC_IFACE" 2>/dev/null || true

if systemctl is-active --quiet benchmark-netlog 2>/dev/null; then
  echo "Stopping benchmark network logging service..."
  sudo systemctl stop benchmark-netlog
  echo "✓ benchmark-netlog stopped"
fi

echo "✓ Teardown complete"
