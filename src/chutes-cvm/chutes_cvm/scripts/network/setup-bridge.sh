#!/bin/bash
# setup-bridge-simple.sh - Simple, reliable bridge networking for VM
# Updated for true idempotency - reuses the same TAP interface

set -e

# Default values
BRIDGE_NAME="br0"
BRIDGE_IP="192.168.100.1/24"
VM_IP="192.168.100.2/24"
VM_GATEWAY="192.168.100.1"
VM_DNS="8.8.8.8"
PUBLIC_IFACE="ens9f0np0"
SSH_PORT=2222
K3S_API_PORT=6443
NODE_PORTS="30000:32767"
STATUS_PORT=8080
TAP_IFACE="vmtap0"

echo "=== Bridge Network Setup ==="
echo "Architecture: VM ← TAP ← Bridge ← NAT ← Internet"
echo

# Parse arguments (parse all first so --clean sees e.g. --public-iface when given after)
DO_CLEAN=""
DO_MULTI_QUEUE=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --bridge-ip) BRIDGE_IP="$2"; VM_GATEWAY="${BRIDGE_IP%/*}"; shift 2 ;;
    --vm-ip) VM_IP="$2"; shift 2 ;;
    --vm-dns) VM_DNS="$2"; shift 2 ;;
    --public-iface) PUBLIC_IFACE="$2"; shift 2 ;;
    --clean) DO_CLEAN=1; shift ;;
    --multi-queue) DO_MULTI_QUEUE=1; shift ;;
    --help)
      echo "Usage: $0 [options]"
      echo "Simple bridge setup - reliable and well-tested"
      echo "Options:"
      echo "  --bridge-ip IP/MASK       Bridge IP (default: $BRIDGE_IP)"  
      echo "  --vm-ip IP/MASK           VM IP (default: $VM_IP)"
      echo "  --vm-dns IP               VM DNS (default: $VM_DNS)"
      echo "  --public-iface IFACE      Public interface (default: $PUBLIC_IFACE)"
      echo "  --multi-queue             Recreate TAP with multi_queue (for virtio-net multiqueue)"
      echo "  --clean                   Remove all bridge setup"
      echo "  --help                    Show this help"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# BRIDGE_NET derived from BRIDGE_IP (single place, after args parsed)
BRIDGE_NET="$(echo "$BRIDGE_IP" | awk -F'[./]' '{printf "%s.%s.%s.0/%s\n",$1,$2,$3,$5}')"

if [[ -n "$DO_CLEAN" ]]; then
  # Remove only rules for this run's config (user must pass same IP/CIDR and --public-iface as when rules were added)
  sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$SSH_PORT" -j DNAT --to-destination "${VM_IP%/*}:22" 2>/dev/null || true
  sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$K3S_API_PORT" -j DNAT --to-destination "${VM_IP%/*}:$K3S_API_PORT" 2>/dev/null || true
  sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$NODE_PORTS" -j DNAT --to-destination "${VM_IP%/*}" 2>/dev/null || true
  sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$STATUS_PORT" -j DNAT --to-destination "${VM_IP%/*}:$STATUS_PORT" 2>/dev/null || true
  sudo iptables -D FORWARD -i "$BRIDGE_NAME" -o "$PUBLIC_IFACE" -j ACCEPT 2>/dev/null || true
  sudo iptables -D FORWARD -i "$PUBLIC_IFACE" -o "$BRIDGE_NAME" -d "${VM_IP%/*}" -j ACCEPT 2>/dev/null || true
  sudo iptables -D FORWARD -i "$PUBLIC_IFACE" -o "$BRIDGE_NAME" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  sudo iptables -t nat -D POSTROUTING -s "$BRIDGE_NET" -o "$PUBLIC_IFACE" -j MASQUERADE 2>/dev/null || true
  sudo iptables -t mangle -D FORWARD -o "$PUBLIC_IFACE" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || true
  sudo ip link delete "$TAP_IFACE" 2>/dev/null || true
  sudo ip link delete "$BRIDGE_NAME" 2>/dev/null || true
  echo "Bridge network setup cleaned."
  exit 0
fi

echo "1. Creating bridge interface..."
# Check if bridge already exists
if ip link show "$BRIDGE_NAME" >/dev/null 2>&1; then
  echo "   ✓ Bridge $BRIDGE_NAME already exists"
  # Check if it has the right IP
  if ip addr show "$BRIDGE_NAME" | grep -q "${BRIDGE_IP%/*}"; then
    echo "   ✓ Bridge IP already configured: $BRIDGE_IP"
  else
    echo "   ⚠ Adding IP to existing bridge: $BRIDGE_IP"
    sudo ip addr add "$BRIDGE_IP" dev "$BRIDGE_NAME" 2>/dev/null || true
  fi
  # Ensure bridge is up
  sudo ip link set "$BRIDGE_NAME" up
else
  # Create new bridge
  sudo ip link add name "$BRIDGE_NAME" type bridge
  sudo ip addr add "$BRIDGE_IP" dev "$BRIDGE_NAME"
  sudo ip link set "$BRIDGE_NAME" up
  echo "   ✓ Bridge created: $BRIDGE_NAME with IP $BRIDGE_IP"
fi

echo "2. Setting up TAP interface for VM..."
# --multi-queue: delete existing tap so we recreate with multi_queue (idempotent upgrade)
if [[ -n "$DO_MULTI_QUEUE" ]] && ip link show "$TAP_IFACE" >/dev/null 2>&1; then
  sudo ip link set "$TAP_IFACE" nomaster 2>/dev/null || true
  sudo ip link delete "$TAP_IFACE" 2>/dev/null || true
  echo "   ✓ Recreated TAP for multi-queue"
fi
# Check if TAP interface already exists and is properly configured
if ip link show "$TAP_IFACE" >/dev/null 2>&1; then
  echo "   ✓ TAP interface already exists: $TAP_IFACE"
  
  # Ensure it's up and connected to bridge
  sudo ip link set "$TAP_IFACE" up
  
  # Check if already connected to bridge
  if bridge link show | grep -q "$TAP_IFACE.*master $BRIDGE_NAME"; then
    echo "   ✓ TAP interface already connected to bridge"
  else
    echo "   ⚠ Connecting existing TAP interface to bridge"
    sudo ip link set "$TAP_IFACE" master "$BRIDGE_NAME"
  fi
else
  # Create new TAP interface (multi_queue for virtio-net multiqueue + vhost)
  sudo ip tuntap add dev "$TAP_IFACE" mode tap multi_queue
  sudo ip link set "$TAP_IFACE" up
  sudo ip link set "$TAP_IFACE" master "$BRIDGE_NAME"
  echo "   ✓ TAP interface created and connected: $TAP_IFACE"
fi

echo "3. Setting up routing and NAT..."
sudo sysctl -w net.ipv4.ip_forward=1

# Remove any existing rules for this run's config (idempotent: re-run with same config is safe)
sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$SSH_PORT" -j DNAT --to-destination "${VM_IP%/*}:22" 2>/dev/null || true
sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$K3S_API_PORT" -j DNAT --to-destination "${VM_IP%/*}:$K3S_API_PORT" 2>/dev/null || true
sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$NODE_PORTS" -j DNAT --to-destination "${VM_IP%/*}" 2>/dev/null || true
sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$STATUS_PORT" -j DNAT --to-destination "${VM_IP%/*}:$STATUS_PORT" 2>/dev/null || true
sudo iptables -D FORWARD -i "$PUBLIC_IFACE" -o "$BRIDGE_NAME" -d "${VM_IP%/*}" -j ACCEPT 2>/dev/null || true
sudo iptables -t nat -D POSTROUTING -s "$BRIDGE_NET" -o "$PUBLIC_IFACE" -j MASQUERADE 2>/dev/null || true
sudo iptables -t mangle -D FORWARD -o "$PUBLIC_IFACE" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || true

# Port forwarding rules (add current config)
sudo iptables -t nat -A PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$SSH_PORT" -j DNAT --to-destination "${VM_IP%/*}:22"
sudo iptables -t nat -A PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$K3S_API_PORT" -j DNAT --to-destination "${VM_IP%/*}:$K3S_API_PORT"
sudo iptables -t nat -A PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$NODE_PORTS" -j DNAT --to-destination "${VM_IP%/*}"
sudo iptables -t nat -A PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$STATUS_PORT" -j DNAT --to-destination "${VM_IP%/*}:$STATUS_PORT"

# Traffic forwarding rules
# Allow VM → internet (bridge out to public)
sudo iptables -C FORWARD -i "$BRIDGE_NAME" -o "$PUBLIC_IFACE" -j ACCEPT 2>/dev/null || \
  sudo iptables -A FORWARD -i "$BRIDGE_NAME" -o "$PUBLIC_IFACE" -j ACCEPT
# Allow NEW connections from internet → VM (so DNAT'd traffic reaches the VM)
sudo iptables -A FORWARD -i "$PUBLIC_IFACE" -o "$BRIDGE_NAME" -d "${VM_IP%/*}" -j ACCEPT
# Allow return traffic (VM replies back to remote clients)
sudo iptables -C FORWARD -i "$PUBLIC_IFACE" -o "$BRIDGE_NAME" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
  sudo iptables -A FORWARD -i "$PUBLIC_IFACE" -o "$BRIDGE_NAME" -m state --state RELATED,ESTABLISHED -j ACCEPT

# NAT for outbound traffic
sudo iptables -t nat -A POSTROUTING -s "${BRIDGE_IP%/*}/24" -o "$PUBLIC_IFACE" -j MASQUERADE

# Clamp MSS to the egress interface's PMTU so guest TLS handshakes fit a small-MTU uplink
# (e.g. a WireGuard public_interface at 1280). The guest advertises MSS from its own 1500
# NIC and can't see the real egress size; the bridge can, so it clamps here — sized to
# whatever $PUBLIC_IFACE is, no hardcoded value.
sudo iptables -t mangle -C FORWARD -o "$PUBLIC_IFACE" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || \
  sudo iptables -t mangle -A FORWARD -o "$PUBLIC_IFACE" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

echo "   ✓ NAT and forwarding rules configured"
echo
echo "=== Bridge Setup Complete ==="
echo
echo "✓ Bridge-based networking configured"
echo
echo "Network interface: $TAP_IFACE"
echo "VM IP: ${VM_IP%/*}"
echo "VM Gateway: $VM_GATEWAY"
echo "Bridge IP: ${BRIDGE_IP%/*}"
echo

exit 0