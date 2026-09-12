#!/usr/bin/env bash
set -euo pipefail

# Persistent conntrack-based network logging for benchmark VM NDA compliance.
# Captures connection-level metadata (no TLS payloads) for the bridge subnet.
#
# Environment variables (via EnvironmentFile or export):
#   NETLOG_DIR        Log directory (default: /var/log/chutes/benchmark-netlog)
#   BRIDGE_SUBNET     Filter subnet in CIDR notation (default: 192.168.100.0/24)
#   BRIDGE_IFACE      Bridge interface name (default: br0)

NETLOG_DIR="${NETLOG_DIR:-/var/log/chutes/benchmark-netlog}"
BRIDGE_SUBNET="${BRIDGE_SUBNET:-192.168.100.0/24}"

# Extract the base IP prefix for grep filtering (e.g. "192.168.100.")
SUBNET_PREFIX="${BRIDGE_SUBNET%.*}."

mkdir -p "${NETLOG_DIR}"

# Ensure the conntrack kernel module is loaded
modprobe nf_conntrack 2>/dev/null || true

LOG_FILE="${NETLOG_DIR}/netlog-$(date +%Y%m%d).log"

log_msg() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

log_msg "benchmark-netlog starting — subnet=${BRIDGE_SUBNET} logdir=${NETLOG_DIR}"
log_msg "Logging to ${LOG_FILE}"

# Rotate to a new file on midnight boundary without restart
rotate_if_needed() {
    local current_date
    current_date="$(date +%Y%m%d)"
    local expected="${NETLOG_DIR}/netlog-${current_date}.log"
    if [[ "${LOG_FILE}" != "${expected}" ]]; then
        LOG_FILE="${expected}"
        log_msg "benchmark-netlog log rotated to ${LOG_FILE}"
    fi
}

# conntrack -E streams events in real time. -o timestamp,extended adds timestamps
# and byte/packet counts on DESTROY events.
conntrack -E -o timestamp,extended 2>&1 | while IFS= read -r line; do
    rotate_if_needed
    # Filter to bridge subnet traffic only
    if echo "${line}" | grep -qF "${SUBNET_PREFIX}"; then
        echo "${line}" >> "${LOG_FILE}"
    fi
done
