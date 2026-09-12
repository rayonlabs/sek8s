#!/usr/bin/env bash
# Exit 0 if a live chutes-td QEMU process is running on this host, 1 otherwise.
# Logic must stay aligned with src/chutes-cvm/chutes_cvm/guest/launch.py (_chutes_td_running).
set -euo pipefail

_PROCESS_NAME_CHUTES_TD="chutes-td"

_live_chutes_td_qemu_running() {
  local pid state cmdline
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    [[ -r "/proc/$pid/stat" ]] || continue
    state=$(ps -p "$pid" -o stat= 2>/dev/null || echo "")
    [[ "$state" == Z* ]] && continue
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
    if [[ "$cmdline" != *qemu-system* && "$cmdline" != *qemu-kvm* ]]; then
      continue
    fi
    [[ "$cmdline" == *"$_PROCESS_NAME_CHUTES_TD"* ]] || continue
    return 0
  done < <(
    { pgrep -f 'qemu-system' 2>/dev/null || true
      pgrep -f 'qemu-kvm' 2>/dev/null || true
    } | sort -un
  )
  return 1
}

if _live_chutes_td_qemu_running; then
  exit 0
fi
exit 1
