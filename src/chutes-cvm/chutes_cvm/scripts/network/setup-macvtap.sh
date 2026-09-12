#!/bin/bash
# setup-macvtap-performance.sh - macvtap setup

set -e

# Default values
VM_IP="192.168.100.2/24"
VM_GATEWAY="192.168.100.1"
VM_DNS="8.8.8.8"
PUBLIC_IFACE="ens9f0np0"
SSH_PORT=2222
K3S_API_PORT=6443
NODE_PORTS="30000:32767"

echo "=== Network Macvtap Setup ==="
echo "Architecture: Direct macvtap -> physical interface (no bridge overhead)"
echo

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --vm-ip)
      VM_IP="$2"
      shift 2
      ;;
    --vm-gateway) 
      VM_GATEWAY="$2"; 
      shift 2 
      ;;
    --vm-dns)
      VM_DNS="$2"
      shift 2
      ;;
    --public-iface)
      PUBLIC_IFACE="$2"
      shift 2
      ;;    
      --clean)
      # Clean up
      sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$SSH_PORT" -j DNAT --to-destination "${VM_IP%/*}:22" 2>/dev/null || true
      sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$K3S_API_PORT" -j DNAT --to-destination "${VM_IP%/*}:$K3S_API_PORT" 2>/dev/null || true
      sudo iptables -t nat -D PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$NODE_PORTS" -j DNAT --to-destination "${VM_IP%/*}" 2>/dev/null || true
      sudo iptables -D FORWARD -i "$PUBLIC_IFACE" -d "${VM_IP%/*}" -j ACCEPT 2>/dev/null || true
      sudo iptables -D FORWARD -s "${VM_IP%/*}" -o "$PUBLIC_IFACE" -j ACCEPT 2>/dev/null || true
      sudo iptables -t nat -D POSTROUTING -s "${VM_IP%/*}" -o "$PUBLIC_IFACE" -j MASQUERADE 2>/dev/null || true
      
      # Remove macvtap interfaces
      for iface in $(ip link show | grep -o "vmnet-[^:@]*"); do
        sudo ip link delete "$iface" 2>/dev/null || true
      done

      echo "Network setup cleaned."
      exit 0
      ;;
    --help)
      echo "Usage: $0 [options]"
      echo "Options:"
      echo "  --vm-ip IP/MASK           VM static IP and netmask (default: $VM_IP)"
      echo "  --vm-dns IP               VM DNS server (default: $VM_DNS)"
      echo "  --public-iface IFACE      Host's public interface (default: $PUBLIC_IFACE)"
      echo "  --clean                   Remove bridge, macvtap, and iptables rules"
      echo "  --help                    Show this help"
      echo ""
      echo "Example:"
      echo "  $0 --vm-ip 192.168.100.2/24 --vm-dns 8.8.8.8 --public-iface ens9f0np0"
      echo "Output: NET_IFACE=<macvtap_interface> VM_IP=<vm_ip> VM_GATEWAY=<vm_gateway>"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# Validate required commands
for cmd in ip iptables; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: $cmd not found. Install it (e.g., sudo apt install iproute2 iptables)."
    exit 1
  fi
done


echo "1. Creating direct macvtap interface..."

# Create macvtap DIRECTLY on physical interface
MACVTAP_IFACE="vmnet-$(uuidgen | cut -c1-8)"
sudo ip link add link "$PUBLIC_IFACE" name "$MACVTAP_IFACE" type macvtap mode vepa
sudo ip link set "$MACVTAP_IFACE" up

echo "   ✓ Macvtap interface: $MACVTAP_IFACE"
echo "   ✓ Direct connection to: $PUBLIC_IFACE"
echo "   ✓ No bridge layer - maximum performance"

echo "2. Configuring host routing for VM subnet..."

# Add host IP to macvtap interface (required for gateway functionality)
sudo ip addr add "$VM_GATEWAY/24" dev "$MACVTAP_IFACE"

# Add route for VM subnet - host knows how to reach VM
VM_NETWORK="${VM_IP%/*}/32"  # Single host route, not subnet
sudo ip route add "$VM_NETWORK" dev "$MACVTAP_IFACE" 2>/dev/null || true

echo "   ✓ Host gateway IP: $VM_GATEWAY added to $MACVTAP_IFACE"
echo "   ✓ Route added: $VM_NETWORK via $MACVTAP_IFACE"

echo "3. Enabling IP forwarding..."
sudo sysctl -w net.ipv4.ip_forward=1
echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf >/dev/null

echo "4. Setting up high-performance NAT rules..."

# Port forwarding from public interface to VM
sudo iptables -t nat -A PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$SSH_PORT" -j DNAT --to-destination "${VM_IP%/*}:22"
sudo iptables -t nat -A PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$K3S_API_PORT" -j DNAT --to-destination "${VM_IP%/*}:$K3S_API_PORT"
sudo iptables -t nat -A PREROUTING -i "$PUBLIC_IFACE" -p tcp --dport "$NODE_PORTS" -j DNAT --to-destination "${VM_IP%/*}"

# Direct forwarding rules (no bridge processing)
sudo iptables -A FORWARD -i "$PUBLIC_IFACE" -d "${VM_IP%/*}" -j ACCEPT
sudo iptables -A FORWARD -s "${VM_IP%/*}" -o "$PUBLIC_IFACE" -j ACCEPT

# NAT for outgoing traffic
sudo iptables -t nat -A POSTROUTING -s "${VM_IP%/*}" -o "$PUBLIC_IFACE" -j MASQUERADE

echo "   ✓ Direct forwarding rules (bypass bridge processing)"
echo "   ✓ NAT configured for ${VM_IP%/*}"

echo "5. Verifying network configuration..."

echo "   Macvtap interface status:"
ip link show "$MACVTAP_IFACE" | sed 's/^/     /'

echo "   VM routing:"
ip route show | grep "${VM_IP%/*}" | sed 's/^/     /' || echo "     (route will appear when VM starts)"

echo
echo "=== Network Setup Complete ==="
echo
echo "Architecture: VM ←→ macvtap ←→ physical interface (direct path)"
echo "Isolation: Maintained through iptables rules"
echo
echo "For QEMU, use:"
echo "  --network-type macvtap"
echo "  --net-iface $MACVTAP_IFACE"
echo "  --vm-ip ${VM_IP%/*}"
echo "  --vm-gateway $VM_GATEWAY"
echo
echo "Macvtap interface: $MACVTAP_IFACE"
echo "VM IP: ${VM_IP%/*}"
echo "VM Gateway: $VM_GATEWAY"

exit 0