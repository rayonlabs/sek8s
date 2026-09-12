#!/usr/bin/env bash
# install.sh — the single source of truth for installing the chutes-cvm CLI on a host.
#
# It owns BOTH fetch and install, and picks its mode automatically:
#
#   • Run from inside a checkout (ansible / build / dev) → skip the fetch and do an EDITABLE
#     install from the enclosing checkout (a later `git pull` then updates the code, no reinstall).
#   • curl … | bash  (no checkout) → BOOTSTRAP: sparse shallow-fetch just what a host needs
#     (host-tools/ firmware/ src/chutes-cvm/), NON-EDITABLE install (the package + bundled
#     nvidia-gpu-tools copy into a persistent venv, firmware copied next to it), then discard the
#     fetched source. Fully launch-capable, nothing of the repo source left behind.
#
# Every caller (standalone curl|bash, the ansible host_tools role, the guest build) runs THIS
# script; the sparse path-list and the install steps each live here exactly once.
#
#   Standalone:  curl -sSL https://raw.githubusercontent.com/chutesai/sek8s/main/src/chutes-cvm/install.sh | bash
#   From a repo: bash src/chutes-cvm/install.sh
#   Explicit:    install.sh --dest /opt/sek8s --editable --ref main
#
# Options / env:
#   --dest DIR         checkout location for bootstrap (default: an ephemeral temp dir)
#   --editable         force editable install; --no-editable forces non-editable
#   --ref REF          branch/tag to fetch (env SEK8S_REF, default main)
#   --force            install even if a DIFFERENT chutes-cvm already resolves on PATH (which
#                      would otherwise shadow the shim); by default that is a hard error
#                      (see the pre-install path-conflict guard)
#   SEK8S_REPO         git URL (default https://github.com/chutesai/sek8s.git) — uses the host's
#                      existing git credentials (the repo is private)
#   CHUTES_CVM_VENV    venv location (default /opt/chutes-cvm/venv)
#   CHUTES_CVM_BIN     PATH dir for the shims (default /usr/local/bin)
#   CHUTES_CVM_PYPI=1  install chutes-cvm from PyPI instead of the checkout
#   CHUTES_CVM_VERSION PyPI version spec when CHUTES_CVM_PYPI=1
set -euo pipefail

REPO="${SEK8S_REPO:-https://github.com/chutesai/sek8s.git}"
REF="${SEK8S_REF:-main}"
VENV_DIR="${CHUTES_CVM_VENV:-/opt/chutes-cvm/venv}"
BIN_DIR="${CHUTES_CVM_BIN:-/usr/local/bin}"
SHIM="$BIN_DIR/chutes-cvm"
DEST=""
EDITABLE="auto"
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dest) DEST="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --editable) EDITABLE=1; shift ;;
        --no-editable) EDITABLE=0; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null | sed 's/^# \?//'; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; exit 1 ;;
    esac
done

log() { printf '==> %s\n' "$*"; }

# ── Prerequisites ──────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || {
    echo "ERROR: python3 is required. Install it and re-run." >&2; exit 1; }
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    echo "ERROR: python3 venv support missing. Install python3-venv and re-run." >&2; exit 1; fi

# sudo prefix for the root-owned install targets (venv, /usr/local/bin). The fetch runs as the
# invoking user (into a user-owned temp dir, or as root under ansible) so cleanup stays simple.
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# ── Path-conflict guard (fail closed, before mutating anything) ─────────────────
# We manage exactly one entry point: the shim at $SHIM. If a DIFFERENT chutes-cvm already
# resolves on PATH, our shim would be shadowed by it and a plain `chutes-cvm` would keep running
# the other one — an ambiguous, half-broken environment. Refuse rather than silently create that
# state; --force overrides. Empty (fresh install) or our own shim (re-install) are both fine.
EXISTING_CVM="$(command -v chutes-cvm 2>/dev/null || true)"
if [ -n "$EXISTING_CVM" ] && [ "$EXISTING_CVM" != "$SHIM" ]; then
    if [ "$FORCE" -eq 1 ]; then
        log "WARNING: a different chutes-cvm is on PATH ($EXISTING_CVM); installing anyway (--force)."
    else
        cat >&2 <<EOF
ERROR: a different chutes-cvm already resolves on PATH:
    $EXISTING_CVM
This install manages the shim at $SHIM, but the above would still win, leaving an ambiguous
environment. It is usually a dev/editable install (e.g. a repo .venv) shadowing the managed shim.
Remove or deactivate it, or re-run with --force to install anyway.
EOF
        exit 1
    fi
fi

# ── Resolve source: repo-present (this script inside a checkout) vs bootstrap fetch ────────────
SELF="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
[ -n "$SELF" ] && [ -f "$SELF" ] && SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"

CLEANUP_DEST=""
cleanup() { [ -n "$CLEANUP_DEST" ] && rm -rf "$CLEANUP_DEST" 2>/dev/null || true; }
trap cleanup EXIT

if [ "${CHUTES_CVM_PYPI:-}" = "1" ]; then
    MODE="pypi"; REPO_ROOT=""; PKG_DIR=""
    [ "$EDITABLE" = auto ] && EDITABLE=0
elif [ -z "$DEST" ] && [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    # Repo-present: this script sits at <repo>/src/chutes-cvm/install.sh.
    MODE="present"
    PKG_DIR="$SCRIPT_DIR"
    REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
    [ "$EDITABLE" = auto ] && EDITABLE=1
    log "repo-present install from $REPO_ROOT (editable=$EDITABLE)"
else
    # Bootstrap: sparse shallow-fetch just host-tools/, firmware/, src/chutes-cvm/.
    command -v git >/dev/null 2>&1 || {
        echo "ERROR: git is required to fetch the source. Install it and re-run." >&2; exit 1; }
    if [ -z "$DEST" ]; then
        DEST="$(mktemp -d "${TMPDIR:-/tmp}/sek8s-install.XXXXXX")"
        CLEANUP_DEST="$DEST"   # ephemeral — remove on exit
    fi
    [ "$EDITABLE" = auto ] && EDITABLE=0
    MODE="bootstrap"; PKG_DIR="$DEST/src/chutes-cvm"; REPO_ROOT="$DEST"
    log "sparse shallow fetch $REPO ($REF) -> $DEST"
    mkdir -p "$DEST"
    git -C "$DEST" init -q
    git -C "$DEST" remote add origin "$REPO" 2>/dev/null \
        || git -C "$DEST" remote set-url origin "$REPO"
    git -C "$DEST" config core.sparseCheckout true
    mkdir -p "$DEST/.git/info"
    printf 'host-tools/\nfirmware/\nsrc/chutes-cvm/\n' > "$DEST/.git/info/sparse-checkout"
    git -C "$DEST" fetch --depth 1 origin "$REF"
    git -C "$DEST" reset --hard -q FETCH_HEAD
fi

# ── Virtualenv + package install ───────────────────────────────────────────────
log "venv: $VENV_DIR"
$SUDO mkdir -p "$(dirname "$VENV_DIR")"

# A venv is bound to the minor version that built it. After a Python upgrade (an OS release
# upgrade, typically) the new interpreter never looks at the old site-packages, so reusing the
# venv yields a chutes-cvm on PATH that cannot import its own package. Recreate on mismatch.
PY_TAG="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ -f "$VENV_DIR/pyvenv.cfg" ]; then
    VENV_TAG="$(sed -n 's/^version[[:space:]]*=[[:space:]]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' \
                "$VENV_DIR/pyvenv.cfg")"
    if [ -n "$VENV_TAG" ] && [ "$VENV_TAG" != "$PY_TAG" ]; then
        log "existing venv is python $VENV_TAG, this is $PY_TAG — recreating"
        $SUDO rm -rf "$VENV_DIR"
    fi
fi

$SUDO python3 -m venv "$VENV_DIR"            # reuses an existing venv of the same version
$SUDO "$VENV_DIR/bin/python3" -m pip install --quiet --upgrade pip

if [ "$MODE" = "pypi" ]; then
    log "install: chutes-cvm${CHUTES_CVM_VERSION:+==$CHUTES_CVM_VERSION} (PyPI)"
    $SUDO "$VENV_DIR/bin/python3" -m pip install --quiet "chutes-cvm${CHUTES_CVM_VERSION:+==$CHUTES_CVM_VERSION}"
else
    [ -f "$PKG_DIR/pyproject.toml" ] || {
        echo "ERROR: package source not found at $PKG_DIR." >&2; exit 1; }
    if [ "$EDITABLE" = "1" ]; then
        log "install: $PKG_DIR (checkout, editable)"
        $SUDO "$VENV_DIR/bin/python3" -m pip install --quiet -e "$PKG_DIR"
    else
        log "install: $PKG_DIR (checkout, non-editable — source is disposable)"
        $SUDO "$VENV_DIR/bin/python3" -m pip install --quiet "$PKG_DIR"
    fi
fi

# ── Verify the package actually imports ────────────────────────────────────────
# The shim is written below unconditionally, so without this an install that left the package
# unimportable (version-mismatched venv, editable install whose source moved) still reports
# success and puts a broken chutes-cvm on PATH.
if ! $SUDO "$VENV_DIR/bin/python3" -c 'import chutes_cvm' 2>/dev/null; then
    echo "ERROR: chutes_cvm is not importable from $VENV_DIR after install." >&2
    echo "       Remove the venv and re-run: sudo rm -rf $VENV_DIR" >&2
    exit 1
fi

# ── Guest firmware (non-editable installs only) ────────────────────────────────
# An editable install resolves firmware from the persistent checkout. A non-editable install
# discards the source, so copy the committed firmware next to the venv and point the shim at it.
FIRMWARE_ENV_LINE=""
if [ "$EDITABLE" = "0" ] && [ "$MODE" != "pypi" ]; then
    FIRMWARE_PERSIST="$(dirname "$VENV_DIR")/firmware"
    if ls "$REPO_ROOT"/firmware/*.fd >/dev/null 2>&1; then
        log "firmware: $FIRMWARE_PERSIST (copied from checkout)"
        $SUDO mkdir -p "$FIRMWARE_PERSIST"
        $SUDO cp "$REPO_ROOT"/firmware/*.fd "$FIRMWARE_PERSIST"/
        FIRMWARE_ENV_LINE="export CHUTES_CVM_FIRMWARE_DIR=\"\${CHUTES_CVM_FIRMWARE_DIR:-$FIRMWARE_PERSIST}\""
    else
        echo "WARNING: no firmware/*.fd under $REPO_ROOT; launches will need CHUTES_CVM_FIRMWARE_DIR set." >&2
    fi
fi

# ── chutes-cvm shim → the venv's console script ────────────────────────────────
log "shim: $SHIM"
$SUDO mkdir -p "$BIN_DIR"
$SUDO tee "$SHIM" >/dev/null <<EOF
#!/bin/sh
# chutes-cvm — generated by install.sh.
${FIRMWARE_ENV_LINE}
exec "$VENV_DIR/bin/chutes-cvm" "\$@"
EOF
$SUDO chmod +x "$SHIM"

# ── nvidia-gpu-tools (bundled wheel → same venv, symlinked on PATH) ─────────────
# NVIDIA's gpu-admin-tools isn't on any index, so it ships as a wheel bundled inside the package.
# Install it into the SAME venv (it has no deps) and symlink its console script onto PATH.
GPU_TOOLS_WHEEL="$($SUDO "$VENV_DIR/bin/python3" - <<'PY'
import glob, os
from chutes_cvm.paths import gpu_tools_dir
whls = sorted(glob.glob(os.path.join(str(gpu_tools_dir()), "*.whl")))
print(whls[0] if whls else "")
PY
)"
if [ -n "$GPU_TOOLS_WHEEL" ] && [ -f "$GPU_TOOLS_WHEEL" ]; then
    log "install: nvidia-gpu-tools ($(basename "$GPU_TOOLS_WHEEL"))"
    $SUDO "$VENV_DIR/bin/python3" -m pip install --quiet --upgrade "$GPU_TOOLS_WHEEL"
    if [ -x "$VENV_DIR/bin/nvidia-gpu-tools" ]; then
        $SUDO ln -sf "$VENV_DIR/bin/nvidia-gpu-tools" "$BIN_DIR/nvidia-gpu-tools"
        log "shim: $BIN_DIR/nvidia-gpu-tools"
    fi
else
    echo "WARNING: bundled nvidia-gpu-tools wheel not found; GPU configuration will be unavailable." >&2
fi

echo "Installed chutes-cvm -> $SHIM"
echo "Try: chutes-cvm host verify"
