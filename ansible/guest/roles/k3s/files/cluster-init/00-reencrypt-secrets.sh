#!/bin/bash
# 00-reencrypt-secrets.sh — Re-encrypt any plaintext secrets in state.db.
#
# Runs first in the post-start sequence (00- prefix).
#
# Secrets encryption is MANDATORY as of the current VM version — there is no
# supported unencrypted state. Any condition that would leave secrets unencrypted
# is a HARD FAILURE (no marker), which in the post-start orchestrator escalates
# to a VM power-off. The completion marker is self-validating: it is trusted only
# when secrets are verified encrypted at rest, so a stale marker from an earlier
# false-success run cannot permanently skip re-encryption.
#
# Why this is needed:
#   The guest image is built with k3s running but WITHOUT secrets encryption, so
#   the baked-in state.db holds plaintext secrets and configmaps.  On first boot
#   setup-storage-bind-mounts SYNCS that build-time state.db onto the fresh
#   storage volume (it does NOT delete it), so a "fresh" VM starts k3s on the
#   build-time plaintext data — exactly like an upgrade from an unencrypted
#   image.  This script re-writes every live secret/configmap through the active
#   encryption provider, then (in the purge step) scrubs plaintext left behind in
#   dead rows and in the tombstone rows of resources that were created and
#   deleted at build time.
#
# The EncryptionConfiguration has secretbox first, identity last:
#   - Reads:  secretbox attempted first; identity fallback decrypts plaintext
#   - Writes: always use secretbox (first provider)
# So `kubectl replace` re-encrypts every secret in place without data loss.
set -euo pipefail

# All paths default to their production locations and are env-overridable so the
# script can be exercised against an isolated k3s (--data-dir in a temp dir) by
# the integration test. Production sets none of these, so defaults apply.
MARKER="${MARKER_DIR:-/var/lib/rancher/k3s/init-markers}/reencrypt-secrets.done"
LOG_FILE="${LOG_FILE:-/var/log/k3s-post-start.log}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [00-reencrypt-secrets] $1" | tee -a "$LOG_FILE"
}

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
STATE_DB="${STATE_DB:-/var/lib/rancher/k3s/server/db/state.db}"
# Staged into $STAGED_DIR by the k3s-post-start wrapper; /run/chutes is denied to this script
# by sek8s.k3s-init.00-reencrypt-secrets. The bare path only resolves when run unconfined.
ENCRYPTION_CONFIG="${ENCRYPTION_CONFIG:-${STAGED_DIR:-/run/chutes}/k3s-encryption-config.yaml}"
K3S_CONFIG="${K3S_CONFIG:-/etc/rancher/k3s/config.yaml}"

# Returns 0 if every live secret AND configmap in kine is encrypted at rest, 1 if
# any is still plaintext. Both resource types are in the EncryptionConfiguration
# and are re-encrypted below, so the marker must validate both — otherwise a
# configmap that failed to seal would still write a "done" marker that the
# self-validating check (which only ever re-reads these same rows) would honor.
# Reads state.db directly (offline), so it works even before k3s is up.
#
# Only LIVE rows are checked (kine.deleted = 0). A resource that was created and
# deleted at build time leaves a tombstone row (deleted != 0) whose value is
# empty or the pre-encryption plaintext; that row is NOT a live secret and the
# online re-encrypt loop cannot touch it (the apiserver does not list deleted
# resources), so counting it as "plaintext" here would make verification fail
# forever on a fresh volume. The tombstone's plaintext is scrubbed in the purge
# step below instead.
verify_resources_encrypted() {
    python3 - "$STATE_DB" <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
plain = [n for (n, v) in conn.execute(
    "SELECT name, value FROM kine k WHERE "
    "(name LIKE '/registry/secrets/%' OR name LIKE '/registry/configmaps/%') "
    "AND k.deleted = 0 "
    "AND id=(SELECT MAX(id) FROM kine WHERE name=k.name)")
    if b'k8s:enc:secretbox' not in bytes(v or b'')]
conn.close()
# Intentionally no detail logged — do not publicize which/whether resources are
# unencrypted. Caller logs a neutral message and acts on the exit code.
sys.exit(1 if plain else 0)
PYEOF
}

# Self-validating marker: "done" is only trusted if secrets are ACTUALLY
# encrypted at rest. A prior bug (bad key / encryption silently inactive /
# apiserver down) could set the marker without encrypting anything; a stale
# marker must not permanently skip re-encryption. If the marker is present but
# secrets are still plaintext, clear it and re-run.
if [[ -f "$MARKER" ]]; then
    if [[ ! -f "$STATE_DB" ]] || verify_resources_encrypted; then
        log "Re-encryption marker present and secrets/configmaps encrypted at rest — skipping"
        exit 0
    fi
    log "Detected inconsistent secrets-encryption state — repairing"
    rm -f "$MARKER"
fi

# Guard 1: the apiserver must be reachable. This is an ONLINE tool — the
# re-encryption below is `kubectl get | kubectl apply`, i.e. the apiserver does
# the sealing. If k3s is down, kubectl returns nothing, the rewrite loop is a
# vacuous no-op, and we would still run the destructive purge and mark done with
# plaintext secrets. Fail loudly and retry instead.
if ! kubectl get --raw='/readyz' >/dev/null 2>&1; then
    log "ERROR: apiserver not reachable — cannot re-encrypt online; will retry next boot"
    exit 1
fi

# Guard 2: secrets encryption is MANDATORY as of the current VM version — there is
# no supported unencrypted state. So each of the following is a HARD FAILURE with
# NO marker written (in post-start this escalates to a VM power-off, which is
# correct): we must never run, nor record success, with secrets unencrypted.

# The apiserver must actually be configured to encrypt. The encryption config FILE
# existing is NOT sufficient — a prior bug copied it without wiring
# encryption-provider-config into the apiserver.
if ! grep -q "encryption-provider-config" "$K3S_CONFIG" 2>/dev/null; then
    log "FATAL: no encryption-provider-config in $K3S_CONFIG — secrets encryption is required; refusing to continue"
    exit 1
fi

if [[ ! -f "$ENCRYPTION_CONFIG" ]]; then
    log "FATAL: encryption config $ENCRYPTION_CONFIG is missing — secrets encryption is required; refusing to continue"
    exit 1
fi

# A present-but-identity-only EncryptionConfiguration means nothing is sealed.
if ! grep -q "secretbox" "$ENCRYPTION_CONFIG"; then
    log "FATAL: $ENCRYPTION_CONFIG is identity-only (no secretbox) — secrets encryption is required; refusing to continue"
    exit 1
fi

log "Secrets encryption is active — re-encrypting all existing plaintext secrets and configmaps..."

log "Replacing all secrets and configmaps so live kine rows have encrypted values..."

# Process each resource individually with a fresh fetch per attempt.
#
# A bulk `kubectl get --all-namespaces -o json | kubectl apply` has a race window:
# k3s-managed objects (validator-auth, k3s-serving, node-password, etc.) are
# updated by the addon controller concurrently during post-start.  If the
# controller updates one between our bulk get and the apply, kubectl apply uses
# the stale resourceVersion from the snapshot and the API server rejects it with
# a 409 Conflict.
#
# Fix: fetch each resource immediately before applying it — the resourceVersion is
# always current at apply time.  On conflict, re-fetch and retry from scratch so
# we never replay a stale snapshot.  Resources deleted between list and get are
# skipped silently (gone = already re-encrypted or never needed).
reencrypt_resource_type() {
    local resource_type="$1"
    local max_attempts=5

    local items
    items=$(kubectl get "$resource_type" --all-namespaces \
        -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\n"}{end}' \
        2>/dev/null) || true

    local failed=0
    while IFS=$'\t' read -r ns name; do
        [[ -z "$ns" || -z "$name" ]] && continue

        local attempt=1
        local ok=0
        while [[ $attempt -le $max_attempts ]]; do
            # Fresh fetch every attempt — never reuse a snapshot from a previous attempt
            local json
            json=$(kubectl get "$resource_type" "$name" -n "$ns" -o json 2>/dev/null) || {
                # Resource was deleted between list and get — skip it
                ok=1
                break
            }

            local err
            err=$(echo "$json" \
                | python3 -c "
import json, sys
d = json.load(sys.stdin)
d['metadata'].pop('resourceVersion', None)
print(json.dumps(d))
" | kubectl apply -f - 2>&1) && { ok=1; break; }

            if echo "$err" | grep -q "Conflict\|the object has been modified"; then
                log "WARN: conflict on $resource_type $ns/$name (attempt $attempt/$max_attempts) — re-fetching"
            else
                log "WARN: error on $resource_type $ns/$name (attempt $attempt/$max_attempts): $err"
            fi
            attempt=$((attempt + 1))
            [[ $attempt -le $max_attempts ]] && sleep 1
        done

        if [[ $ok -eq 0 ]]; then
            log "ERROR: failed to re-encrypt $resource_type $ns/$name after $max_attempts attempts"
            failed=$((failed + 1))
        fi
    done <<< "$items"

    # Return a boolean status, not the raw count: a count is an exit code that wraps
    # mod 256, so e.g. exactly 256 failures would return 0 (read by the caller as
    # success) and let the destructive purge/marker proceed with plaintext present.
    if [[ $failed -gt 0 ]]; then
        log "ERROR: $failed $resource_type failed to re-encrypt"
        return 1
    fi
    return 0
}

if ! reencrypt_resource_type "secrets"; then
    log "ERROR: Re-encryption of secrets failed"
    exit 1
fi
if ! reencrypt_resource_type "configmaps"; then
    log "ERROR: Re-encryption of configmaps failed"
    exit 1
fi
log "All live records re-written through the active encryption provider"

# Guard 3: verify secrets AND configmaps are actually encrypted AT REST before the
# destructive purge or marking done. The rewrite only seals data if the apiserver is
# truly encrypting; if it silently isn't, the live rows are still plaintext and we must
# NOT purge (it only deletes dead rows) or mark (which permanently skips re-encrypt).
if [[ -f "$STATE_DB" ]] && ! verify_resources_encrypted; then
    log "ERROR: secrets/configmaps encryption state check failed after repair — not finalizing; will retry next boot"
    exit 1
fi
log "Verified: all secrets and configmaps encrypted at rest"

# Purge kine history and scrub old_value.
#
# kine is append-only: every write appends a new row, leaving the previous
# revision as a dead row.  Additionally, every live row carries an old_value
# column with the previous (potentially plaintext) value used only for etcd
# watch event payloads — Kubernetes controllers do not rely on it.
#
# Both steps run online; SQLite WAL mode allows concurrent access with k3s.
#
# The marker is written only after this purge succeeds.  A failed purge causes
# a full retry on the next boot — plaintext left in dead rows or old_value
# is a security issue and must not be silently skipped.  The kubectl replace
# retry is safe: already-encrypted values are decrypted and re-encrypted
# idempotently, producing only additional dead rows for the next purge to clean.
if [[ -f "$STATE_DB" ]]; then
    if python3 - "$STATE_DB" <<'PYEOF'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
dead = conn.execute(
    "DELETE FROM kine WHERE id NOT IN (SELECT MAX(id) FROM kine GROUP BY name)"
).rowcount
nulled = conn.execute("UPDATE kine SET old_value = NULL").rowcount
# Scrub plaintext left in the tombstone rows of build-time-deleted secrets and
# configmaps. These rows survive the dead-row purge above (a deleted key's only
# remaining row is the MAX(id) tombstone for that name) and their value may be
# the pre-encryption plaintext. verify_resources_encrypted intentionally skips
# them, so this is the only place that removes that plaintext from disk.
scrubbed = conn.execute(
    "UPDATE kine SET value = NULL WHERE deleted != 0 AND value IS NOT NULL AND "
    "(name LIKE '/registry/secrets/%' OR name LIKE '/registry/configmaps/%')"
).rowcount
conn.commit()
conn.close()
print(f"Deleted {dead} dead rows, nulled old_value on {nulled} live rows, "
      f"scrubbed {scrubbed} deleted-resource tombstones")
PYEOF
    then
        log "Kine purge complete — plaintext logically removed from state.db"
    else
        log "ERROR: Kine purge failed — not marking complete, will retry on next boot"
        exit 1
    fi
fi

touch "$MARKER"
