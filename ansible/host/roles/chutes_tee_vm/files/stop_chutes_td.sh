#!/usr/bin/env bash
# Force-stop a chutes-td QEMU guest that did not exit after a graceful shutdown.
# SIGTERM first, then SIGKILL. Exit 0 if no chutes-td QEMU remains, 1 if one is
# still alive afterwards (e.g. uninterruptible D-state from stuck I/O — the host
# must be rebooted before the qcow2 image lock will clear and the VM can relaunch).
#
# Detection logic must stay aligned with is_live_chutes_td.sh and
# src/chutes-cvm/chutes_cvm/guest/launch.py (_chutes_td_running).
#
# Usage: stop_chutes_td.sh [term_wait_seconds] [kill_wait_seconds]
set -euo pipefail

_PROCESS_NAME_CHUTES_TD="chutes-td"
TERM_WAIT="${1:-15}"
KILL_WAIT="${2:-10}"

_chutes_td_pids() {
  local pid state cmdline
  { pgrep -f 'qemu-system' 2>/dev/null || true
    pgrep -f 'qemu-kvm' 2>/dev/null || true
  } | sort -un | while read -r pid; do
    [[ -z "$pid" ]] && continue
    [[ -r "/proc/$pid/stat" ]] || continue
    state=$(ps -p "$pid" -o stat= 2>/dev/null || echo "")
    [[ "$state" == Z* ]] && continue
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
    if [[ "$cmdline" != *qemu-system* && "$cmdline" != *qemu-kvm* ]]; then
      continue
    fi
    [[ "$cmdline" == *"$_PROCESS_NAME_CHUTES_TD"* ]] || continue
    echo "$pid"
  done
}

mapfile -t pids < <(_chutes_td_pids)
if [[ ${#pids[@]} -eq 0 ]]; then
  echo "No chutes-td QEMU process running."
  exit 0
fi

echo "Stuck chutes-td QEMU pid(s): ${pids[*]}"
echo "Sending SIGTERM..."
kill -TERM "${pids[@]}" 2>/dev/null || true

for ((i = 0; i < TERM_WAIT; i++)); do
  mapfile -t pids < <(_chutes_td_pids)
  [[ ${#pids[@]} -eq 0 ]] && { echo "Guest exited after SIGTERM."; exit 0; }
  sleep 1
done

mapfile -t pids < <(_chutes_td_pids)
echo "Still alive after ${TERM_WAIT}s; sending SIGKILL to: ${pids[*]}"
kill -KILL "${pids[@]}" 2>/dev/null || true

for ((i = 0; i < KILL_WAIT; i++)); do
  mapfile -t pids < <(_chutes_td_pids)
  [[ ${#pids[@]} -eq 0 ]] && { echo "Guest exited after SIGKILL."; exit 0; }
  sleep 1
done

mapfile -t pids < <(_chutes_td_pids)
echo "ERROR: chutes-td QEMU pid(s) ${pids[*]} survived SIGKILL." >&2
echo "The process is almost certainly in uninterruptible (D) state from stuck I/O;" >&2
echo "SIGKILL cannot reap it and it will keep the qcow2 image lock until reboot." >&2
echo "Reboot the host, then re-run the upgrade." >&2
for pid in "${pids[@]}"; do
  ps -p "$pid" -o pid,stat,wchan:32,cmd= 2>/dev/null || true
done
exit 1
