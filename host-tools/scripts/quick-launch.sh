#!/bin/bash
# DEPRECATED compatibility shim — quick-launch.sh is now `chutes-cvm guest launch`.
#
# The launch orchestrator was ported from this script into Python
# (chutes_cvm.guest.launch). This shim is kept only so existing miner automation that
# invokes quick-launch.sh by path (e.g. a systemd unit's ExecStart) keeps working across
# the upgrade. It forwards every argument verbatim to the CLI.
#
# As a convenience, when run from a checkout and `chutes-cvm` is not yet on PATH, it
# bootstraps the CLI via the checkout's install.sh (editable) — so `git pull` + this
# wrapper gets a host going without a separate manual install step.
#
# Please update your automation to call `chutes-cvm guest launch` directly; this shim may
# be removed in a future release.
set -euo pipefail

echo "quick-launch.sh is deprecated — forwarding to 'chutes-cvm guest launch'. Update your" >&2
echo "automation (e.g. systemd ExecStart) to call 'chutes-cvm guest launch' directly." >&2

# Bootstrap the CLI from the enclosing checkout if it isn't installed yet. install.sh is the
# single source of truth for install; run from a checkout it does an editable install (no fetch),
# writes the venv + /usr/local/bin/chutes-cvm shim (uses sudo for those root targets).
if ! command -v chutes-cvm >/dev/null 2>&1; then
  _shim_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
  _install_sh="$_shim_dir/../../src/chutes-cvm/install.sh"
  if [[ -f "$_install_sh" ]]; then
    echo "chutes-cvm not found — installing it from this checkout (editable) via install.sh..." >&2
    bash "$_install_sh" --editable || {
      echo "Error: chutes-cvm install failed (see install.sh output above)." >&2
      exit 1
    }
    hash -r 2>/dev/null || true
  fi
fi

if ! command -v chutes-cvm >/dev/null 2>&1; then
  echo "Error: 'chutes-cvm' not found on PATH and no checkout install.sh alongside this shim." >&2
  echo "  Install it: bash src/chutes-cvm/install.sh   (or curl -sSL .../install.sh | bash)" >&2
  exit 1
fi

exec chutes-cvm guest launch "$@"
