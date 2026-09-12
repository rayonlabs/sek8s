"""Declarative VM spec -> QEMU command.

The single place that turns a description of a TDX guest VM (memory, CPU/NUMA,
and PCIe device layout) into the qemu-system-x86_64 command. It is pure — given a
fully-resolved ``MachineSpec`` it reads no live hardware — so both callers share
it and get a byte-identical command for the same spec:

  - the launcher (``chutes_cvm.guest``) resolves the spec from live detection/sysfs;
  - offline measurement (``guest-tools/measurement``) resolves it from a topology
    fingerprint.

It wraps the low-level builders in ``qemu.py`` (``build_base_cmd`` + the
PCI-topology state machines).
"""

from dataclasses import dataclass, field

from chutes_cvm.guest.qemu import (
    NumaPciTopologyState,
    PciTopologyState,
    QemuCommand,
    build_base_cmd,
)


@dataclass(frozen=True)
class DeviceSpec:
    """One PCIe device on its own root port.

    ``rp_id``/``chassis`` identify the root port; ``host_bdf`` is the passed-
    through device's PCI BDF (the launcher supplies the real one; offline
    measurement supplies a placeholder that the measurement layer swaps for a
    ``pci-bar-stub`` endpoint). ``numa_node`` places it under the matching PXB on
    the NUMA path (< 0 = flat). ``bar_*`` add the per-device MMIO fw_cfg hint.
    """

    rp_id: str
    chassis: int
    host_bdf: str
    numa_node: int = -1
    bar_size_mb: int | None = None
    bar_index: int | None = None


@dataclass
class MachineSpec:
    """A fully-resolved description of the RTMR0-relevant QEMU machine.

    ``host_nodes`` is the explicit guest NUMA node list — it *is* the NUMA
    decision: a guest-NUMA topology is built when it has >= 2 entries, otherwise
    the guest is flat (``[]``). The caller (host launcher or measurement adapter)
    decides how many nodes a given host uses; this lib just builds what it's
    given. ``devices`` are added to the PCI topology in order.
    """

    mem: str
    smp_topology: str
    cpu_args: str
    firmware: str
    host_nodes: list[int]
    devices: list[DeviceSpec] = field(default_factory=list)
    img_path: str = "root.qcow2"
    process_name: str = "chutes-td"
    foreground: bool = False
    pidfile: str = "/dev/null"
    logfile: str = "/dev/null"


def build_qemu_command(spec: MachineSpec) -> QemuCommand:
    """Turn a ``MachineSpec`` into a base + PCI-topology ``QemuCommand``."""
    cmd = build_base_cmd(
        mem=spec.mem,
        smp_topology=spec.smp_topology,
        process_name=spec.process_name,
        cpu_args=spec.cpu_args,
        firmware=spec.firmware,
        img_path=spec.img_path,
        foreground=spec.foreground,
        pidfile=spec.pidfile,
        logfile=spec.logfile,
        host_nodes=spec.host_nodes,
        # This path only dumps ACPI (RTMR0), which is boot-method independent and
        # excludes the kernel — so the boot chain is placeholders. The dump-side
        # metadata (platform_tables) supplies its own /dev/null direct section.
        kernel_path="/dev/null",
        initrd_path="/dev/null",
        cmdline="",
    )
    # NUMA is driven entirely by host_nodes (the adapter's decision); this lib
    # supports any node count. (The 2-node SLIT distance in _append_numa_memory
    # is the remaining piece to generalize before adapters emit > 2 nodes.)
    numa_active = len(spec.host_nodes) >= 2
    topo = NumaPciTopologyState() if numa_active else PciTopologyState()
    for d in spec.devices:
        kwargs: dict = {"rp_id": d.rp_id, "chassis": d.chassis}
        if d.bar_size_mb is not None and d.bar_index is not None:
            kwargs["bar_size_mb"] = d.bar_size_mb
            kwargs["bar_index"] = d.bar_index
        if numa_active:
            kwargs["numa_node"] = d.numa_node
        topo.add_device(cmd, host_bdf=d.host_bdf, **kwargs)
    return cmd
