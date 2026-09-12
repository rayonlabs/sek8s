#!/bin/bash
# verify-apparmor-profiles.sh — Verify all sek8s AppArmor profiles are loaded
# and enforcing.  Run as a oneshot at boot (After=apparmor.service).  If any
# profile is missing or not in enforce mode, the VM shuts down via
# OnFailure=poweroff.target.
set -euo pipefail

PROFILES=(
    sek8s.system-manager
    sek8s.setup-cache
    sek8s.deny-sensitive-default
    sek8s.attestation-proxy
    sek8s.chute-log-shipper
    # One per cluster-init script that needs a boot secret. Declared explicitly, alongside the
    # profile in sek8s.k3s-init and the staging map in k3s-post-start.sh. A script missing from
    # any of the three fails loudly at boot rather than silently gaining or losing access.
    sek8s.k3s-init.00-reencrypt-secrets
    sek8s.k3s-init.03-k3s-validator-auth
    sek8s.k3s-init.04-helm-chart-upgrade
)

APPARMOR_PROFILES="/sys/kernel/security/apparmor/profiles"

if [ ! -f "$APPARMOR_PROFILES" ]; then
    echo "FATAL: AppArmor interface not available at ${APPARMOR_PROFILES}" >&2
    exit 1
fi

for profile in "${PROFILES[@]}"; do
    if grep -q "^${profile} (enforce)$" "$APPARMOR_PROFILES" 2>/dev/null; then
        echo "OK: ${profile} (enforce)"
    elif grep -q "^${profile} " "$APPARMOR_PROFILES" 2>/dev/null; then
        echo "FATAL: ${profile} is loaded but not in enforce mode" >&2
        exit 1
    else
        echo "FATAL: ${profile} is not loaded" >&2
        exit 1
    fi
done

echo "All sek8s AppArmor profiles verified"
