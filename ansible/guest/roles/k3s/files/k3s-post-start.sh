#!/bin/bash
# /usr/local/bin/k3s-post-start.sh
# k3s-post-start: Run the post-start step scripts (after k3s is up) with individual tracking
set -e

# Configuration
SCRIPT_DIR="${SCRIPT_DIR:-/usr/local/bin/k3s-init-scripts}"
MARKER_DIR="${MARKER_DIR:-/var/lib/rancher/k3s/init-markers}"
# Per-script staging root for boot secrets. On tmpfs, so it clears on every boot.
# sek8s.deny-sensitive-default denies this tree outright; only a sek8s.k3s-init.<script> profile
# can read its OWN subdirectory. See ansible/guest/group_vars/all/k3s-init-secrets.yml.
STAGE_ROOT="${STAGE_ROOT:-/run/k3s-init}"

# Set by the 10-debug.conf drop-in (measured with /etc/systemd/system). Unset means false, so a
# missing drop-in can never disable the poweroff.

# Power off unless this is a debug build. Callers treat this as terminal either way; on debug it
# returns so the wrapper can exit non-zero, leaving systemd to mark the unit failed while the VM
# stays reachable.
fail_terminal() {
    if [ "${K3S_POST_START_DEBUG:-false}" = "true" ]; then
        log "DEBUG BUILD: would power off here — staying up so this can be diagnosed over SSH"
        log "DEBUG BUILD: production would have powered the VM off at this point"
        return 0
    fi
    sleep 5
    poweroff -f
}

# Which files behind the /run/chutes deny each cluster-init script may receive, staged read-only
# into /run/k3s-init/<script>/ for the duration of that script's run.
#
# NOT all of these are secrets, and the distinction matters when reasoning about a leak vs a
# tamper. /run/chutes is blanket-denied because it holds the miner seed and mTLS private keys;
# these files are denied only because they share that directory:
#   k3s-encryption-config.yaml  SECRET  — contains the secretbox key; confidentiality matters
#   validator-ss58              public  — an allowlist ADDRESS; only its integrity matters, since
#                                         altering it would allowlist an attacker's validator
#   signing-keys/helm-pubkey.gpg public  — the key chart signatures verify AGAINST; likewise
#                                         integrity-critical, confidentiality irrelevant
# The staging grant is read-only and the blanket profile denies this tree outright, so integrity
# holds for all three regardless of which are confidential.
#
# Declared here, with a matching profile in /etc/apparmor.d/sek8s.k3s-init and a matching entry in
# verify-apparmor-profiles.sh. A script absent from this map needs no profile and gets no
# exception: it stays on sek8s.deny-sensitive-default. Missing one of the three declarations is a
# loud boot failure, never a silent grant — without a profile the staged file is denied, and
# without a map entry nothing is staged at all.
declare -A INIT_SCRIPT_STAGED_FILES=(
    ["00-reencrypt-secrets.sh"]="/run/chutes/k3s-encryption-config.yaml"
    ["03-k3s-validator-auth.sh"]="/run/chutes/validator-ss58"
    ["04-helm-chart-upgrade.sh"]="/run/chutes/signing-keys/helm-pubkey.gpg"
)

# True if the named AppArmor profile is loaded in the kernel.
aa_profile_loaded() {
    grep -qE "^${1}( |\\()" /sys/kernel/security/apparmor/profiles 2>/dev/null
}

# Stage one script's declared secrets into its own directory, read-only.
#
# This wrapper runs UNCONFINED — AppArmor attaches on the execve target, and that is
# /usr/local/bin/k3s-post-start.sh, which matches no profile — so it can read /run/chutes even
# though the scripts it launches cannot.
#
# THAT IS LOAD-BEARING AND FRAGILE. It holds only because the unit says
# `ExecStart=/usr/local/bin/k3s-post-start.sh`. Changing it to `ExecStart=/bin/bash /usr/local/...`
# or moving this script under /usr/bin makes the execve target a name in @{confined_bins}, which
# confines the wrapper, denies it /run/chutes, and silently breaks staging for EVERY script below.
# A #! line does not save you: the kernel loads the interpreter afterwards, but AppArmor has
# already matched on the path passed to execve. The scripts are launched as `bash <script>`, which execs
# /usr/bin/bash by name and auto-attaches sek8s.deny-sensitive-default.
#
# The copy is what crosses the boundary, not the original: /run/chutes stays blanket-denied in
# every profile, which keeps it fail-CLOSED. Carving per-file exceptions into that deny would make
# it a denylist and silently expose any secret added later.
stage_script_files() {
    local script_name="$1"
    local files="${INIT_SCRIPT_STAGED_FILES[$script_name]:-}"
    [ -n "$files" ] || return 0

    local dir="$STAGE_ROOT/$script_name"
    rm -rf "${dir:?}"
    mkdir -p "$dir"
    # 0700/0400 root-owned. Staging exists to get around AppArmor, not DAC: each step's
    # profile denies /run/chutes outright and grants /run/k3s-init/<script>/ instead. The uid
    # never changes -- this unit is User=root and aa-exec switches the profile, not the user --
    # so root-only modes still satisfy both layers. A wider mode buys the mechanism nothing,
    # because AppArmor mediates paths and not modes, and costs a real widening: the sources are
    # 0600 root in a 0700 /run/chutes, and 0444 exposed them to everything else on the guest for
    # the life of the step -- long enough to matter, since 00-reencrypt-secrets rewrites every
    # secret and configmap through the apiserver.
    chmod 0700 "$STAGE_ROOT" "$dir"

    local f
    for f in $files; do
        if [ -r "$f" ]; then
            # install, not cp: cp is in @{confined_bins}, so exec'ing it would attach
            # sek8s.deny-sensitive-default and be denied both paths, unconfined wrapper or not.
            install -m 0400 "$f" "$dir/$(basename "$f")"
        else
            # Not fatal here: each consumer enforces its own safe-failure behaviour, and failing
            # the whole run would take down the steps that do not need this file.
            log "WARNING: $f not readable; $script_name will fail as designed"
        fi
    done
}

# Remove a script's staged secrets as soon as it exits, so a secret is present only while the one
# script that needs it is running.
unstage_script_files() {
    local script_name="$1"
    [ -n "${INIT_SCRIPT_STAGED_FILES[$script_name]:-}" ] || return 0
    rm -rf "${STAGE_ROOT:?}/$script_name"
}
LOG_FILE="${LOG_FILE:-/var/log/k3s-post-start.log}"
MAX_SCRIPT_TIMEOUT="${MAX_SCRIPT_TIMEOUT:-300}"  # 5 minutes per script
# How often to feed the systemd watchdog while a step script is running. Must stay
# well under WatchdogSec in k3s-post-start.service.
WATCHDOG_PING_INTERVAL="${WATCHDOG_PING_INTERVAL:-30}"
export MARKER_DIR  # Scripts may use this for run-once behavior

# Security-critical scripts that must succeed or the VM powers off.
# Failure of these scripts leaves the cluster in an unsafe state (e.g.
# admin credentials on disk, plaintext secrets in the DB, or the validator
# unable to authenticate to this VM).
SECURITY_CRITICAL_SCRIPTS="00-reencrypt-secrets.sh 03-k3s-validator-auth.sh 99-purge-kubeconfig.sh"

is_security_critical() {
    local name="$1"
    for sc in $SECURITY_CRITICAL_SCRIPTS; do
        [ "$name" = "$sc" ] && return 0
    done
    return 1
}

# Ensure directories exist
mkdir -p "$MARKER_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Function to notify systemd we're still alive
notify_systemd() {
    if [ -n "$NOTIFY_SOCKET" ]; then
        systemd-notify --status="$1" || true
    fi
}

# Function to send watchdog keepalive
send_watchdog() {
    if [ -n "$NOTIFY_SOCKET" ]; then
        systemd-notify WATCHDOG=1 || true
    fi
}

# Function to mark a script as failed
mark_script_failed() {
    local script_name="$1"
    local exit_code="$2"
    local marker_file="$MARKER_DIR/${script_name}.failed"
    
    echo "exit_code=$exit_code" > "$marker_file"
    echo "timestamp=$(date '+%Y-%m-%d %H:%M:%S')" >> "$marker_file"
    log "Marked script $script_name as failed with exit code $exit_code"
}

# Function to run a single script with timeout and error handling
run_script() {
    local script_path="$1"
    local script_name=$(basename "$script_path")
    
    log "Starting execution of script: $script_name"
    notify_systemd "Running $script_name"
    
    # Remove any old failure markers
    rm -f "$MARKER_DIR/${script_name}.failed"
    
    # Create a temporary log file for this script
    local script_log="/tmp/${script_name}.log"
    
    # Run the script with timeout
    local exit_code=0
    local start_time=$(date +%s)
    
    log "Executing: $script_path (timeout: ${MAX_SCRIPT_TIMEOUT}s)"
    
    # Run the step in the background and keep feeding the watchdog while it runs.
    # Blocking here without pings means the quiet window equals the script's runtime,
    # so any step slower than WatchdogSec got the whole unit SIGABRT'd mid-run — and
    # because the step never finished, its completion marker was never written and the
    # restart replayed the same kill forever. `timeout` still bounds the step; the
    # watchdog's job is to catch a wedged *wrapper*, which the ping loop cannot mask
    # (a wedge outside this loop stops the pings).
    # Stage this script's declared boot secrets (no-op for scripts that need none) and run it
    # under its matching profile. `bash "$script_path"` execs /usr/bin/bash by NAME, which
    # @{confined_bins} auto-attaches to sek8s.deny-sensitive-default; aa-exec overrides that with
    # sek8s.k3s-init.<script>, which still denies the model cache and /run/chutes but grants this
    # script's own staged directory. Scripts with no entry in the table are left on the blanket
    # profile — no change for them.
    stage_script_files "$script_name"

    local -a runner=(timeout "$MAX_SCRIPT_TIMEOUT")
    if [ -n "${INIT_SCRIPT_STAGED_FILES[$script_name]:-}" ]; then
        local profile="sek8s.k3s-init.${script_name%.sh}"
        if command -v aa-exec >/dev/null 2>&1 && aa_profile_loaded "$profile"; then
            runner+=(aa-exec -p "$profile" --)
            export STAGED_DIR="$STAGE_ROOT/$script_name"
        else
            # Run anyway rather than pre-failing: the script's own guard produces the accurate
            # error. Log loudly, because the symptom downstream is a bare "permission denied".
            log "WARNING: profile $profile unavailable; $script_name runs confined and will likely be denied"
            unset STAGED_DIR
        fi
    else
        unset STAGED_DIR
    fi
    runner+=(bash "$script_path")

    "${runner[@]}" > "$script_log" 2>&1 &
    local script_pid=$!
    local waited=0
    while kill -0 "$script_pid" 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        if [ $((waited % WATCHDOG_PING_INTERVAL)) -eq 0 ]; then
            send_watchdog
        fi
    done
    wait "$script_pid" || exit_code=$?

    # Drop the staged secrets the moment the script exits, on success or failure, so a secret is
    # readable only while its one script is running.
    unstage_script_files "$script_name"
    unset STAGED_DIR

    if [ $exit_code -eq 0 ]; then
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        
        log "Script $script_name completed successfully in ${duration}s"
        
        # Show last few lines of output for context
        if [ -s "$script_log" ]; then
            log "Last 5 lines of output from $script_name:"
            tail -5 "$script_log" | while read line; do
                log "  $script_name: $line"
            done
        fi
        
        rm -f "$script_log"
        return 0
    else
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        
        if [ $exit_code -eq 124 ]; then
            log "ERROR: Script $script_name timed out after ${MAX_SCRIPT_TIMEOUT}s"
        else
            log "ERROR: Script $script_name failed with exit code $exit_code after ${duration}s"
        fi
        
        mark_script_failed "$script_name" "$exit_code"
        
        # Show script output for debugging
        if [ -s "$script_log" ]; then
            log "Output from failed script $script_name:"
            cat "$script_log" | while read line; do
                log "  $script_name: $line"
            done
        fi
        
        rm -f "$script_log"
        return $exit_code
    fi
}

# Function to wait for k3s to be ready
wait_for_k3s() {
    local max_attempts=60
    local attempt=1
    
    log "Waiting for k3s to be ready..."
    notify_systemd "Waiting for k3s API"
    
    while [ $attempt -le $max_attempts ]; do
        # Check if k3s service is running
        if ! systemctl is-active --quiet k3s; then
            log "k3s service is not active, waiting..."
            sleep 5
            attempt=$((attempt + 5))
            continue
        fi
        
        # Check the API server is actually SERVING, not just reporting ready. /readyz can flip green
        # while the apiserver listener is still cycling after a `systemctl restart k3s` — k3s
        # notifies systemd-ready when its supervisor is up, before the embedded apiserver is
        # continuously bound on :6443. Gate on /openapi/v2 too: it is the exact path
        # `kubectl apply --validate` fetches, so a green check here predicts the init scripts' applies
        # will succeed (a plain /readyz check did not).
        if kubectl get --raw='/readyz' >/dev/null 2>&1 \
            && kubectl get --raw='/openapi/v2' >/dev/null 2>&1; then
            log "API server readiness check passed (openapi served)"
            return 0
        fi
        
        # Send watchdog keepalive
        systemd-notify WATCHDOG=1 || true
        
        if [ $((attempt % 15)) -eq 0 ]; then
            log "Still waiting for API server readiness... ($attempt/$max_attempts)"
        fi
        
        sleep 2
        attempt=$((attempt + 1))
    done
    
    log "ERROR: API server not ready after $max_attempts attempts"
    return 1
}

# Function to discover and sort scripts
get_script_list() {
    if [ ! -d "$SCRIPT_DIR" ]; then
        log "Script directory $SCRIPT_DIR does not exist"
        return 1
    fi
    
    # Find all executable shell scripts, sort them naturally
    find "$SCRIPT_DIR" -name "*.sh" -type f -executable | sort -V
}

# Main execution
main() {
    log "Starting k3s post-start setup"
    log "Script directory: $SCRIPT_DIR"
    log "Marker directory: $MARKER_DIR"
    log "Max script timeout: ${MAX_SCRIPT_TIMEOUT}s"
    
    notify_systemd "Initializing cluster scripts"
    
    # Wait for k3s to be ready first
    if ! wait_for_k3s; then
        log "FATAL: k3s is not ready, cannot proceed with initialization"
        notify_systemd "ERROR: k3s not ready"
        exit 1
    fi

    # Ensure kubeconfig has expected permissions (0600 avoids helm/kubectl "insecure" warnings)
    local kubeconfig="/etc/rancher/k3s/k3s.yaml"
    if [ -f "$kubeconfig" ]; then
        chmod 0600 "$kubeconfig"
        log "Ensured $kubeconfig has mode 0600"
    fi

    # Get list of scripts to run
    local scripts
    if ! scripts=$(get_script_list); then
        log "FATAL: Could not get script list"
        notify_systemd "ERROR: No scripts found"
        exit 1
    fi
    
    if [ -z "$scripts" ]; then
        log "No scripts found in $SCRIPT_DIR, initialization complete"
        notify_systemd "No scripts to run"
        systemd-notify --ready
        exit 0
    fi
    
    # Count scripts for progress tracking
    local total_scripts=$(echo "$scripts" | wc -l)
    local current_script=0
    local successful_scripts=0
    local failed_scripts=0
    
    log "Found $total_scripts script(s) to process"
    
    # Process each script
    while IFS= read -r script_path; do
        current_script=$((current_script + 1))
        local script_name=$(basename "$script_path")
        
        log "Processing script $current_script/$total_scripts: $script_name"
        notify_systemd "Script $current_script/$total_scripts: $script_name"
        
        send_watchdog
        
        if run_script "$script_path"; then
            successful_scripts=$((successful_scripts + 1))
            log "✓ Script $script_name completed successfully"
        else
            failed_scripts=$((failed_scripts + 1))
            if is_security_critical "$script_name"; then
                log "FATAL: script $script_name failed — powering off VM"
                echo "POST-START-FAILURE: $script_name" > /dev/kmsg 2>/dev/null || true
                fail_terminal
            fi
            log "✗ Script $script_name failed (continuing with remaining scripts)"
        fi
        
        send_watchdog
        
        # Brief pause between scripts
        sleep 2
    done <<< "$scripts"
    
    # Final summary
    log "=== Post-start Summary ==="
    log "Total scripts: $total_scripts"
    log "Successful: $successful_scripts"
    log "Failed: $failed_scripts"
    log "====================================="
    
    if [ $failed_scripts -eq 0 ]; then
        log "All scripts completed successfully"
        notify_systemd "All scripts completed successfully"
        systemd-notify --ready
        exit 0
    else
        log "FATAL: $failed_scripts script(s) failed during post-start — powering off VM"
        notify_systemd "FATAL: $failed_scripts failures"
        echo "POST-START-FAILED: $failed_scripts script(s)" > /dev/kmsg 2>/dev/null || true
        fail_terminal
        exit 1
    fi
}

# Handle signals gracefully
trap 'log "Received shutdown signal, exiting..."; exit 0' TERM INT

# Run main function
main "$@"