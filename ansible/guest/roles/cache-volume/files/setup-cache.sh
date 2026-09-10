#!/bin/bash
# setup-cache.sh - Set up HF cache dir for pod use (e.g. model weights)
# Creates /var/snap/cache with 1000:1000 (tdx on VM; matches runAsUser/runAsGroup in pod specs).
# system-manager (runs as chutes) gets write access by having chutes in group tdx (GID 1000); dir is 2775.
# All operations are idempotent: safe to run on reboot with an existing cache (mkdir -p, chown, chmod).
# On failure (e.g. /var/snap not mounted) the script shuts down the VM immediately; service also has OnFailure=poweroff.target.
set -euo pipefail

LOG_TAG="setup-cache"

log() {
    echo "$1"
    logger -t "$LOG_TAG" "$1" 2>/dev/null || true
}

log_error() {
    echo "ERROR: $1" >&2
    logger -t "$LOG_TAG" -p user.err "ERROR: $1" 2>/dev/null || true
}

# UID:GID for pod/container access (must match runAsUser/runAsGroup in pod spec). On VM this is tdx.
SNAP_OWNER="1000:1000"
# system-manager's uid, pinned in the system-manager role (its primary group is tdx/1000).
# Numeric on purpose: chown by NAME would have to resolve /etc/passwd + /etc/group +
# /etc/nsswitch.conf, which sek8s.setup-cache does not grant, and widening that profile on a
# poweroff-on-failure path to buy an indirection that must never vary is a bad trade.
XDG_CACHE_OWNER="10150:1000"
SNAP_CACHE="/var/snap/cache"

# Ensure HF cache volume is mounted (without it we don't have enough room for model weights)
if ! mountpoint -q /var/snap; then
    log_error "/var/snap is not mounted, cannot set up cache"
    log_error "Shutting down immediately to prevent boot without HF cache"
    sync
    shutdown -h now
    exit 1
fi

# Create snap cache dir: 1000:1000 (tdx) so pods can use it; 2775 so group (tdx) can write; chutes is in group tdx so system-manager can write
mkdir -p "$SNAP_CACHE"
# chmod first: it repairs each directory's mode on the pre-order visit, so the walk can descend
# into pod-created entries. chown cannot repair anything, so running it first makes an
# unreadable directory abort the script (set -e) and OnFailure=poweroff.target bricks the VM.
chmod -R 2775 "$SNAP_CACHE"
chown -R "$SNAP_OWNER" "$SNAP_CACHE"

# system-manager's XDG cache. Created HERE, and deliberately not from system-manager's own
# ExecStartPre: that ran `+/bin/bash -c 'install -d ...'`, and the `+` prefix makes systemd skip
# AppArmorProfile=, so bash auto-attached sek8s.deny-sensitive-default (bash is in
# @{confined_bins}) and was denied /var/snap/cache/** — EACCES on prod only, since debug builds
# load the profile in complain mode. This script already runs as root under sek8s.setup-cache,
# which grants the cache tree plus CAP_CHOWN/FOWNER/FSETID, and is ordered after var-snap.mount
# and before system-manager. Uses mkdir/chown/chmod rather than `install` because those are the
# binaries the profile grants. Must stay AFTER the recursive chown above, which would otherwise
# reset the owner back to $SNAP_OWNER.
mkdir -p "$SNAP_CACHE/.xdg-cache"
chown "$XDG_CACHE_OWNER" "$SNAP_CACHE/.xdg-cache"
chmod 2775 "$SNAP_CACHE/.xdg-cache"

log "Cache setup complete"
exit 0
