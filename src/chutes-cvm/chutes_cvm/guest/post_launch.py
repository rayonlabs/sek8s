"""Post-launch QEMU vCPU/IOthread pinning to host NUMA-local CPUs.

Host-wide CPU power tuning (governor, C-states) is a separate, operator-driven
concern handled by ``chutes_cvm.host.tune`` -- it is intentionally decoupled from
the VM lifecycle. Thread pinning here is scoped to QEMU's own threads and
reverts automatically when the process exits.
"""

from __future__ import annotations

import os
import re
import time

from chutes_cvm import proc


def expand_cpulist(raw: str) -> list[int]:
    """Expand a sysfs cpulist string (e.g. '0-47,96-143') to CPU IDs."""
    cpus: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return [cpu for cpu in cpus if cpu != 0]


def host_node_cpus(host_node: int) -> list[int]:
    """Return logical CPUs on a host NUMA node, excluding CPU 0."""
    path = f"/sys/devices/system/node/node{host_node}/cpulist"
    try:
        with open(path) as f:
            return expand_cpulist(f.read())
    except OSError:
        return []


def _run_root(cmd: list[str], **kwargs) -> proc.CompletedProcess:
    if os.geteuid() == 0:
        return proc.run(cmd, **kwargs)
    return proc.run(["sudo", *cmd], **kwargs)


def _read_pidfile(pidfile: str) -> int | None:
    try:
        with open(pidfile) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def find_qemu_pid(*, pidfile: str) -> int | None:
    """Resolve QEMU PID from the pidfile written at launch.

    Returns None if the pidfile is missing, unreadable, or the process is
    no longer running. Intentionally does not fall back to pgrep to avoid
    accidentally pinning threads of an unrelated process.
    """
    pid = _read_pidfile(pidfile)
    if pid is not None and os.path.exists(f"/proc/{pid}"):
        return pid
    return None


def _vcpu_thread_ids(pid: int) -> list[tuple[int, int]]:
    """Return (vcpu_index, thread_id) pairs sorted by vcpu index."""
    result = proc.run(
        ["ps", "-T", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    threads: list[tuple[int, int]] = []
    for line in result.stdout.splitlines():
        match = re.search(r"^\s*\d+\s+(\d+).*\bCPU (\d+)/KVM\b", line)
        if match:
            threads.append((int(match.group(2)), int(match.group(1))))
    threads.sort(key=lambda item: item[0])
    return threads


def _vcpu_thread_ids_wchan_fallback(pid: int) -> list[int]:
    """Fallback: threads with wchan '0' (KVM vCPU run loop)."""
    tids: list[int] = []
    task_dir = f"/proc/{pid}/task"
    try:
        for tid_name in os.listdir(task_dir):
            wchan_path = f"{task_dir}/{tid_name}/wchan"
            try:
                with open(wchan_path) as f:
                    if f.read().strip() == "0":
                        tids.append(int(tid_name))
            except OSError:
                continue
    except OSError:
        return []
    return sorted(tids)


def _iothread_ids(pid: int) -> list[int]:
    result = proc.run(
        ["ps", "-T", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    tids: list[int] = []
    for line in result.stdout.splitlines():
        match = re.search(r"^\s*\d+\s+(\d+).*iothread", line)
        if match:
            tids.append(int(match.group(1)))
    return tids


def pin_qemu_threads(
    pid: int,
    *,
    vcpus_total: int,
    host_nodes: list[int],
) -> None:
    """Pin QEMU vCPU (and IOthread) threads to host NUMA-local CPUs."""
    if not host_nodes:
        print("  Warning: no host NUMA nodes for thread pinning")
        return

    num_guest_nodes = len(host_nodes)
    vcpus_per_node = vcpus_total // num_guest_nodes
    node_cpus = {node: host_node_cpus(node) for node in host_nodes}

    vcpu_threads = _vcpu_thread_ids(pid)
    if not vcpu_threads:
        print("  Falling back to wchan-based vCPU detection...")
        fallback_tids = _vcpu_thread_ids_wchan_fallback(pid)
        vcpu_threads = list(enumerate(fallback_tids))

    pinned = 0
    for vcpu_id, tid in vcpu_threads:
        guest_node = vcpu_id // vcpus_per_node
        if guest_node >= num_guest_nodes:
            guest_node = num_guest_nodes - 1
        host_node = host_nodes[guest_node]
        cpus = node_cpus.get(host_node, [])
        if not cpus:
            continue
        target_cpu = cpus[vcpu_id % len(cpus)]
        result = _run_root(
            ["taskset", "-pc", str(target_cpu), str(tid)],
            stdout=proc.DEVNULL,
            stderr=proc.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            pinned += 1

    print(
        f"  Pinned {pinned} vCPU thread(s) across {num_guest_nodes} NUMA nodes "
        f"({vcpus_per_node} vCPUs/node, avoiding CPU0)"
    )

    iothread_tids = _iothread_ids(pid)
    if not iothread_tids:
        return

    node0_cpus = node_cpus.get(host_nodes[0], [])
    if not node0_cpus:
        return

    iot_start = max(0, len(node0_cpus) - len(iothread_tids) - 1)
    iot_pinned = 0
    for iot_idx, tid in enumerate(iothread_tids):
        target_cpu = node0_cpus[iot_start + iot_idx]
        result = _run_root(
            ["taskset", "-pc", str(target_cpu), str(tid)],
            stdout=proc.DEVNULL,
            stderr=proc.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            iot_pinned += 1
    print(f"  Pinned {iot_pinned} IOthread(s) to dedicated pCPUs")


def apply_post_launch_tuning(
    *,
    pidfile: str,
    vcpus_total: int,
    host_nodes: list[int],
    pin_threads: bool,
) -> None:
    """Pin QEMU vCPU/IOthread threads to host NUMA-local CPUs after launch.

    Scoped to QEMU's own threads and reverts automatically when the process
    exits. Host-wide CPU power tuning is handled separately by
    ``chutes_cvm.host.tune`` and is not tied to the VM lifecycle.
    """
    if not pin_threads:
        return

    print("Pinning vCPU threads to host NUMA nodes...")
    time.sleep(3)
    pid = find_qemu_pid(pidfile=pidfile)
    if pid is None:
        print("  Warning: could not find QEMU PID for thread pinning")
        return
    pin_qemu_threads(pid, vcpus_total=vcpus_total, host_nodes=host_nodes)
