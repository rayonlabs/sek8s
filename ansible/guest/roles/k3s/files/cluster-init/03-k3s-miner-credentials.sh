#!/bin/bash
# Reconcile the miner-credentials secret in every namespace that consumes it.
#
# Deliberately NOT run-once (no marker) and idempotent via `kubectl apply` upsert: the desired
# secret is fully determined by the per-VM config-volume creds + this image, so it should converge
# on every boot. That makes upgrades work — an image that adds a key (e.g. the attestation proxy's
# seed in attestation-system) updates the existing secret in place. This script runs as root with
# the admin kubeconfig, so it can upsert regardless of the miner's limited RBAC; miners never have
# to delete a secret or wipe their storage volume to pick up a new key.
set -euo pipefail

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a /var/log/first-boot-miner-credentials.log
}

# Retry wrapper: this reconcile runs every boot, and the post-start orchestrator powers the VM off
# on any failed script, so a transient k8s API blip must not take the VM down.
retry_kubectl() {
    local attempts=10 delay=3 n=1
    until "$@"; do
        if [ "$n" -ge "$attempts" ]; then
            log "ERROR: kubectl failed after ${attempts} attempts: $*"
            return 1
        fi
        log "kubectl transient failure (attempt ${n}/${attempts}) — retrying in ${delay}s"
        sleep "$delay"
        n=$((n + 1))
    done
}

CREDENTIALS_DIR="/var/config"

log "Loading miner credentials..."
MINER_SS58=$(cat "$CREDENTIALS_DIR/miner-ss58")
MINER_SEED=$(cat "$CREDENTIALS_DIR/miner-seed")

# The upsert is a pipeline (client-side manifest gen | server-side apply); wrap it in a function so
# retry_kubectl can re-run the whole thing on a transient failure.
apply_miner_secret() {
    kubectl create secret generic miner-credentials \
      --from-literal=ss58="$MINER_SS58" \
      --from-literal=seed="$MINER_SEED" \
      --dry-run=client -o yaml | kubectl apply -n "$1" -f -
}

# Upsert into each consuming namespace:
#   chutes             — miner workloads / control plane sign with the hotkey.
#   attestation-system — the attestation proxy signs responses with the hotkey (rc proof-of-
#                        possession) so the validator authorizes release-candidate measurements.
for ns in chutes attestation-system; do
    log "Reconciling miner-credentials secret in ${ns}..."
    retry_kubectl apply_miner_secret "$ns"
done

log "Miner credentials reconciled."
