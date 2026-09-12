#!/bin/bash
# 04-helm-chart-upgrade.sh: Boot-time Helm chart reconciler.
#
# Each /etc/chutes/charts/<name>.conf is the single source of truth for one chart
# (version + values + flags), git-tracked and measured into RTMR3 so it cannot be
# tampered with. On every boot a chart is reconciled via `helm upgrade` when the
# conf's content hash differs from the last-applied marker (or the release is in a
# `failed` state). Unchanged confs are skipped, so there is no helm-revision churn.
# Because the trigger is the conf hash, a VERSION bump and a values change are
# handled identically — no separate "overrides" or "updates" mechanism needed.
#
# Markers live on the persisted storage volume so "applied" survives reboots and
# image updates (/var/lib/rancher/k3s is bind-mounted from storage):
#   /var/lib/rancher/k3s/init-markers/charts/<name>
#
# Conf fields (shell-sourced):
#   RELEASE, NAMESPACE, CHART   (required)
#   VERSION                     (REQUIRED — exact chart version to pin. Never resolved to
#                                "latest" or the installed version: the measured spec must
#                                fully determine what runs, so a third party auditing the
#                                VM can reproduce it. A values-only change still pins the
#                                same VERSION explicitly.)
#   HELM_SET                    (space-separated key=value -> --set; for small overrides.
#                                Prefer a values file: /etc/chutes/charts/values/<name>.yaml
#                                is auto-detected and passed via -f. Both the conf and the
#                                values file are hashed for the change-trigger.)
#   EXTRA_FLAGS                 (extra helm CLI flags, e.g. --disable-openapi-validation)
#   REUSE_VALUES                (non-empty -> --reuse-values)
#   VERIFY                      (non-empty -> --verify --keyring <KEYRING_FILE>)
#   REPO_NAME, REPO_URL         (optional; `helm repo add` before upgrade)
#
# Determinism: EVERY chart is fatal. The measured spec must fully determine what
# runs on an attested node, so any chart that cannot be reconciled to its measured
# spec — a failed upgrade, a missing/uninstalled release, or an unreadable helm/API
# state — powers the VM off (fail closed) rather than letting it serve in a state
# that diverges from what was measured. There is no "best effort" tier: a node is
# either converged to the measured charts or it is down. Each chart gets a small
# number of in-boot retries first (failures should be rare against the local k3s
# cluster, and an unchanged spec is skipped without touching helm/network at all).
set -uo pipefail

# Initial attempt + retries before a chart is declared fatal. (Overridable only to
# keep the boot env's defaults — the boot service environment is fixed and measured.)
RECONCILE_ATTEMPTS="${RECONCILE_ATTEMPTS:-3}"
RECONCILE_RETRY_DELAY="${RECONCILE_RETRY_DELAY:-5}"

# Paths default to the production locations; overridable so the reconciler can be
# exercised in tests. Production boots with these unset, so the defaults apply.
LOG_FILE="${LOG_FILE:-/var/log/helm-chart-upgrade.log}"
CHARTS_DIR="${CHARTS_DIR:-/etc/chutes/charts}"
MARKERS_DIR="${MARKERS_DIR:-/var/lib/rancher/k3s/init-markers/charts}"
# Helm provenance keyring is fetched dynamically at boot (fetch-signing-keys, initramfs)
# into the ephemeral /run tmpfs and PGP-verified against the attested root key — not
# baked into the image. See the signing-keys role.
# Staged flat into $STAGED_DIR by the k3s-post-start wrapper, so the basename differs from the
# /run/chutes path. helm opens this file itself and inherits this script's profile via `ix`, so it
# must be a path this profile grants — /run/chutes is denied here as it is everywhere.
KEYRING_FILE="${STAGED_DIR:+$STAGED_DIR/helm-pubkey.gpg}"
KEYRING_FILE="${KEYRING_FILE:-/run/chutes/signing-keys/helm-pubkey.gpg}"
KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
export KUBECONFIG
# HELM_*_HOME are set by k3s-post-start.service; no fallbacks for determinism

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"; }

# Wait for admission controller webhook (charts whose resources go through it,
# e.g. chutes-miner-gpu, otherwise fail with "context deadline exceeded").
wait_for_admission_webhook() {
    local max_attempts=30 n=0
    log "Waiting for admission webhook readiness..."
    while [ "$n" -lt "$max_attempts" ]; do
        if curl -sfk -o /dev/null "https://127.0.0.1:8443/health" 2>/dev/null; then
            log "Admission webhook is ready"
            return 0
        fi
        sleep 2
        n=$((n + 1))
    done
    log "WARNING: Admission webhook not ready in $((max_attempts * 2))s, proceeding anyway"
}

# Reconcile one chart conf. Returns 0 on success/skip, 1 on failure.
reconcile_chart() {
    local conf="$1"
    local name marker
    name="$(basename "$conf" .conf)"
    marker="$MARKERS_DIR/$name"

    RELEASE="" NAMESPACE="" CHART="" VERSION="" HELM_SET="" EXTRA_FLAGS=""
    REUSE_VALUES="" VERIFY="" REPO_NAME="" REPO_URL=""
    # shellcheck source=/dev/null
    source "$conf"
    # A measured conf missing required fields is a broken spec, not something to
    # skip past — fail closed.
    if [ -z "$RELEASE" ] || [ -z "$NAMESPACE" ] || [ -z "$CHART" ]; then
        log "[$name] missing RELEASE/NAMESPACE/CHART in measured conf — FAILED"
        return 1
    fi
    # VERSION is mandatory: never run helm without a pinned version (no "latest"),
    # so the measured spec is fully reproducible for third-party audit.
    if [ -z "$VERSION" ]; then
        log "[$name] VERSION is required (pinned for reproducibility) — refusing to reconcile"
        return 1
    fi

    # Values come from values/<name>.yaml (preferred, readable/diffable) and/or
    # HELM_SET. Hash the conf AND the values file together so a change to either
    # triggers a reconcile.
    local want_hash cur_hash status values_file list_out
    values_file="$CHARTS_DIR/values/$name.yaml"
    want_hash="$( { cat "$conf"; [ -f "$values_file" ] && cat "$values_file"; } 2>/dev/null \
        | sha256sum | awk '{print $1}')"
    cur_hash="$(cat "$marker" 2>/dev/null || true)"

    # Distinguish "helm/API unreadable" (transient) from "release genuinely absent":
    # both used to collapse to an empty status and a silent skip. Under determinism
    # neither may pass — an unreadable state can't be confirmed converged, and a
    # measured chart that isn't installed is a divergence from the measured spec.
    if ! list_out="$(helm list -n "$NAMESPACE" -o json 2>/dev/null)"; then
        log "[$name] could not read helm release state for namespace '$NAMESPACE' — FAILED"
        return 1
    fi
    status="$(echo "$list_out" \
        | jq -r --arg r "$RELEASE" '.[] | select(.name==$r) | .status // empty' 2>/dev/null || true)"

    # Reconcile a build-time install; never create. A missing release means the node
    # would run without a measured chart — fail closed.
    if [ -z "$status" ]; then
        log "[$name] release '$RELEASE' not installed in '$NAMESPACE' — measured chart absent — FAILED"
        return 1
    fi

    if [ "$want_hash" = "$cur_hash" ] && [ "$status" != "failed" ]; then
        log "[$name] spec unchanged (status=$status) — skipping"
        return 0
    fi

    if [ -n "$REPO_NAME" ] && [ -n "$REPO_URL" ]; then
        helm repo add "$REPO_NAME" "$REPO_URL" >/dev/null 2>&1 || true
        helm repo update "$REPO_NAME" >/dev/null 2>&1 || helm repo update >/dev/null 2>&1 || true
    fi

    if [ -n "$VERIFY" ] && [ ! -f "$KEYRING_FILE" ]; then
        log "[$name] VERIFY set but keyring missing at $KEYRING_FILE — FAILED"
        return 1
    fi

    local args=(upgrade "$RELEASE" "$CHART" --namespace "$NAMESPACE" --kubeconfig="$KUBECONFIG")
    [ -n "$VERSION" ] && args+=(--version "$VERSION")
    [ -n "$REUSE_VALUES" ] && args+=(--reuse-values)
    [ -n "$VERIFY" ] && args+=(--verify --keyring "$KEYRING_FILE")
    [ -f "$values_file" ] && args+=(-f "$values_file")
    local kv
    for kv in $HELM_SET; do args+=(--set "$kv"); done
    # EXTRA_FLAGS is intentionally word-split into separate flags.
    # shellcheck disable=SC2206
    [ -n "$EXTRA_FLAGS" ] && args+=($EXTRA_FLAGS)

    log "[$name] reconciling (status=$status, version=$VERSION): helm ${args[*]}"
    if helm "${args[@]}"; then
        mkdir -p "$MARKERS_DIR"
        printf '%s\n' "$want_hash" > "$marker"
        log "[$name] reconciled; marker updated"
        return 0
    fi
    log "[$name] helm upgrade FAILED"
    return 1
}

main() {
    if [ ! -d "$CHARTS_DIR" ]; then
        log "No charts directory at $CHARTS_DIR, nothing to do"
        exit 0
    fi

    log "Refreshing Helm repo index..."
    helm repo update >/dev/null 2>&1 || true
    wait_for_admission_webhook

    local conf name attempt ok
    for conf in "$CHARTS_DIR"/*.conf; do
        [ -f "$conf" ] || continue
        name="$(basename "$conf" .conf)"
        log "--- Reconciling chart: $name ---"

        # Every chart is fatal. Retry a few times to absorb transients against the
        # local k3s cluster, then power off rather than serve in a divergent state.
        ok=0
        for ((attempt = 1; attempt <= RECONCILE_ATTEMPTS; attempt++)); do
            if reconcile_chart "$conf"; then
                ok=1
                break
            fi
            log "[$name] reconcile attempt $attempt/$RECONCILE_ATTEMPTS failed"
            [ "$attempt" -lt "$RECONCILE_ATTEMPTS" ] && sleep "$RECONCILE_RETRY_DELAY"
        done

        if [ "$ok" -ne 1 ]; then
            log "FATAL: chart $name failed to reconcile after $RECONCILE_ATTEMPTS attempts — powering off VM"
            echo "CHART-RECONCILE-FAILED: $name" > /dev/kmsg 2>/dev/null || true
            poweroff -f
            exit 1  # in case poweroff is async, never continue past a fatal failure
        fi
        log "[$name] OK"
    done

    log "Chart reconcile complete — all measured charts converged"
    exit 0
}

main "$@"
