#!/bin/bash
# /usr/local/bin/k3s-pre-start.sh
# k3s-pre-start: Generate k3s configuration before service starts.
# Runs every boot so new image versions can inject updated API server args
# (e.g. authorization webhook) without manual migration. The CA and existing
# certs on the storage volume are untouched; k3s only regenerates leaf certs
# when TLS SANs actually change.
set -e

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a /var/log/k3s-pre-start.log
}

# NOTE: A boot-time CNI/runtime wipe used to live here. It was removed because it
# did net harm: wiping CNI IPAM (/var/lib/cni/networks) out from under containerd's
# sandbox metadata — which persists on the storage volume across reboots — left the
# old sandboxes un-teardownable (CNI DEL has no IPAM record), so they piled up as
# orphaned NotReady sandboxes every boot and forced a double sandbox-create per pod.
# Kubelet graceful node shutdown (shutdownGracePeriod, configured below) is the
# correct fix: pods drain cleanly at shutdown so nothing is orphaned to begin with,
# and on boot kubelet reconciles/ GCs leftover sandboxes itself. Do not reintroduce
# a partial wipe; if a full reset is ever needed it must clear containerd sandbox
# state and CNI state together, never one without the other.

# Public IP detection configuration
INCLUDE_PUBLIC_IP="${INCLUDE_PUBLIC_IP:-true}"
PUBLIC_IP_TIMEOUT="${PUBLIC_IP_TIMEOUT:-5}"
USE_PUBLIC_IP_FOR_ADVERTISE="${USE_PUBLIC_IP_FOR_ADVERTISE:-false}"

# Function to get public IP address
get_public_ip() {
    local public_ip=""
    
    # Skip if disabled
    if [[ "$INCLUDE_PUBLIC_IP" != "true" ]]; then
        return 0
    fi
    
    local services=(
        "ifconfig.me"
        "icanhazip.com" 
        "ipecho.net/plain"
        "checkip.amazonaws.com"
    )
    
    for service in "${services[@]}"; do
        public_ip=$(curl -s --max-time "$PUBLIC_IP_TIMEOUT" "$service" 2>/dev/null | grep -oE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$' || true)
        if [[ -n "$public_ip" ]]; then
            # Log to stderr to avoid contaminating the return value
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Detected public IP from $service: $public_ip" >&2
            echo "$public_ip"
            return 0
        fi
    done
    
    # Log to stderr to avoid contaminating the return value  
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Warning: Could not detect public IP address" >&2
    return 1
}

log "Starting k3s configuration generation..."

# Get current hostname and local IP
HOSTNAME=$(hostname)
NODE_IP=$(ip -4 addr show scope global | grep -E "inet .* (eth|ens|enp)" | head -1 | awk '{print $2}' | cut -d'/' -f1)
if [ -z "$NODE_IP" ]; then
    NODE_IP=$(ip -4 addr show scope global | grep inet | awk '{print $2}' | cut -d'/' -f1 | head -n 1)
fi
log "Target hostname: $HOSTNAME, Local IP: $NODE_IP"

# Get public IP
log "Detecting public IP..."
PUBLIC_IP=$(get_public_ip)
if [[ -n "$PUBLIC_IP" ]]; then
    log "Public IP detected: $PUBLIC_IP"
    
    # Decide which IP to use for advertise-address
    if [[ "$USE_PUBLIC_IP_FOR_ADVERTISE" == "true" ]]; then
        ADVERTISE_IP="$PUBLIC_IP"
        EXTERNAL_IP="$PUBLIC_IP"
        log "Using public IP for advertise-address"
    else
        ADVERTISE_IP="$NODE_IP"
        EXTERNAL_IP="$PUBLIC_IP"
        log "Using local IP for advertise-address, public IP as external-ip"
    fi
else
    log "No public IP detected, using local IP"
    ADVERTISE_IP="$NODE_IP"
    EXTERNAL_IP="$NODE_IP"
fi

# Create k3s configuration with comprehensive TLS SANs
log "Creating k3s configuration with TLS SANs..."
mkdir -p /etc/rancher/k3s

# Build TLS SAN list
TLS_SANS=(
    "$NODE_IP"
    "$HOSTNAME"
    "localhost" 
    "127.0.0.1"
    "::1"
)

# Add public IP to TLS SANs if detected and different from local IP
if [[ -n "$PUBLIC_IP" ]] && [[ "$PUBLIC_IP" != "$NODE_IP" ]]; then
    TLS_SANS+=("$PUBLIC_IP")
    log "Added public IP to TLS SANs: $PUBLIC_IP"
fi

# Create the k3s config with all TLS SANs
cat > /etc/rancher/k3s/config.yaml << EOF
node-name: $HOSTNAME
node-ip: $NODE_IP
node-external-ip: $EXTERNAL_IP
advertise-address: $ADVERTISE_IP
tls-san:
EOF

# Add each TLS SAN to the config
for san in "${TLS_SANS[@]}"; do
    echo "  - $san" >> /etc/rancher/k3s/config.yaml
done

# Continue with the rest of the config
AUTHZ_WEBHOOK_CONFIG="/etc/admission-controller/authorization-webhook-config.yaml"
cat >> /etc/rancher/k3s/config.yaml << EOF
write-kubeconfig-mode: "0600"
disable:
  - traefik
  - servicelb
cluster-cidr: 10.42.0.0/16
service-cidr: 10.43.0.0/16
kube-controller-manager-arg:
  - "terminated-pod-gc-threshold=50"
EOF

# Build kube-apiserver-arg list.  Both encryption and the authorization webhook
# are kube-apiserver flags; they must be passed via kube-apiserver-arg, not as
# top-level k3s config keys (unknown top-level keys are silently ignored by k3s).
ENCRYPTION_CONFIG="/run/chutes/k3s-encryption-config.yaml"

# Both prod and debug now write this file from initramfs before this script runs:
# prod's setup_storage from the attestation-provided key, debug's setup_storage_debug
# from a static well-known key. No build-time /etc/chutes fallback is needed.

KUBE_API_ARGS=()

if [ -f "$ENCRYPTION_CONFIG" ]; then
    KUBE_API_ARGS+=("encryption-provider-config=${ENCRYPTION_CONFIG}")
    log "Secrets encryption enabled: $ENCRYPTION_CONFIG"
else
    log "WARNING: $ENCRYPTION_CONFIG not found — k3s will start without secrets encryption"
fi

if [ -f "$AUTHZ_WEBHOOK_CONFIG" ]; then
    KUBE_API_ARGS+=(
        "authorization-mode=Node,Webhook,RBAC"
        "authorization-webhook-config-file=${AUTHZ_WEBHOOK_CONFIG}"
        "authorization-webhook-version=v1"
        "authorization-webhook-cache-authorized-ttl=5m"
        "authorization-webhook-cache-unauthorized-ttl=2m"
    )
    log "Authorization webhook enabled: $AUTHZ_WEBHOOK_CONFIG"
else
    log "Authorization webhook config not found, using default authorization (Node,RBAC)"
fi

if [ ${#KUBE_API_ARGS[@]} -gt 0 ]; then
    echo "kube-apiserver-arg:" >> /etc/rancher/k3s/config.yaml
    for arg in "${KUBE_API_ARGS[@]}"; do
        echo "  - \"${arg}\"" >> /etc/rancher/k3s/config.yaml
    done
fi

# Kubelet graceful node shutdown. shutdownGracePeriod is a KubeletConfiguration
# field with no equivalent CLI flag, so it's dropped into a config-dir that k3s
# merges over its generated kubelet config. Written here (at runtime) because
# /etc/rancher/k3s is a storage bind mount that starts empty — an image-baked
# file under it would be shadowed. Pairs with the logind InhibitDelayMaxSec
# drop-in, which must be >= shutdownGracePeriod or pods get killed mid-drain.
KUBELET_CONF_DIR="/etc/rancher/k3s/kubelet.conf.d"
mkdir -p "$KUBELET_CONF_DIR"
cat > "$KUBELET_CONF_DIR/10-graceful-shutdown.conf" << 'KUBELET_EOF'
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
shutdownGracePeriod: 30s
shutdownGracePeriodCriticalPods: 10s
KUBELET_EOF
cat >> /etc/rancher/k3s/config.yaml << EOF
kubelet-arg:
  - "config-dir=${KUBELET_CONF_DIR}"
EOF
log "Kubelet graceful shutdown configured (config-dir=$KUBELET_CONF_DIR, grace 30s)"

# Log the configuration for debugging
log "k3s configuration created with the following settings:"
log "  node-name: $HOSTNAME"
log "  node-ip: $NODE_IP" 
log "  node-external-ip: $EXTERNAL_IP"
log "  advertise-address: $ADVERTISE_IP"
log "  TLS SANs: ${TLS_SANS[*]}"

# Final network configuration summary
log "=== Network Configuration Summary ==="
log "Hostname: $HOSTNAME"
log "Local IP: $NODE_IP"
if [[ -n "$PUBLIC_IP" ]]; then
    log "Public IP: $PUBLIC_IP"
    log "External IP: $EXTERNAL_IP"
    log "Advertise Address: $ADVERTISE_IP"
    log "Certificates will include both local and public IPs"
else
    log "Public IP: Not detected"
    log "External IP: $EXTERNAL_IP (same as local)"
    log "Advertise Address: $ADVERTISE_IP"
    log "Certificates will include only local IP"
fi
log "TLS SANs: ${TLS_SANS[*]}"
log "======================================="

log "k3s configuration generation complete - ready for k3s.service to start"