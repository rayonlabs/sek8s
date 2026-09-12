"""QEMU command construction for TDX VM launch.

Builds the full qemu-system-x86_64 command line including base TDX
configuration, PCI device topology, networking, volumes, and vsock.
"""

import os
import re
import sys
from dataclasses import dataclass, field


def _block_format(path: str | None) -> str:
    """Infer block format from path. Returns 'raw' or 'qcow2'. Defaults to raw."""
    if not path:
        return "raw"
    if path.lower().endswith(".qcow2"):
        return "qcow2"
    return "raw"


# TDX guest memory is pinned and unreclaimable, so the guest must leave the host
# enough RAM for the host OS, the TDX PAMT, page tables, and VFIO DMA pinning --
# otherwise the kernel OOM-kills QEMU (the whole VM) as the guest faults in pages.
#
# This is a FLAT reserve, deliberately not a percentage. It must stay aligned with
# how the GpuProfiles size guest RAM: a RAM-derived profile's guest_mem_gb leaves
# exactly this much for the host (B200: "(host_gb - 64) // gpu_count * gpu_count",
# e.g. (3022 - 64) // 8 * 8 = 2952). A percentage reserve breaks that: 12% over-
# reserved ~360 GB on a 3 TB host and wrongly rejected valid B200 launches,
# and any fraction would re-introduce the same mismatch once a host exceeds
# (reserve / fraction). The only overhead that scales with size is the TDX PAMT
# (~0.4% of guest), and 64 GB covers PAMT for guests up to ~16 TB, so a flat
# reserve is safe well beyond current hardware. Revisit deliberately (and resize
# the affected profiles) if a host ever needs more than this.
VM_MEM_RESERVE_GB = 64


def safe_vm_mem_gb(desired_gb: int, host_gb: int | None) -> int:
    """Clamp desired guest RAM (GB) to what the host can safely back.

    Returns ``desired_gb`` unchanged when host RAM is unknown or already leaves
    enough headroom; otherwise caps it at ``host_gb`` minus VM_MEM_RESERVE_GB.
    Never returns a value below 1 GB or clamps upward.
    """
    if not host_gb or host_gb <= 0:
        return desired_gb
    safe = host_gb - VM_MEM_RESERVE_GB
    if safe < 1:
        # Pathologically small host: can't help, leave the request as-is.
        return desired_gb
    return min(desired_gb, safe)


def _parse_mem_mib(mem: str) -> int:
    """Parse QEMU memory size string (e.g. '1536G', '512M') to MiB."""
    match = re.match(r"^(\d+)([GgMm])$", mem)
    if not match:
        raise ValueError(f"Invalid memory size: {mem!r}")
    value = int(match.group(1))
    unit = match.group(2).upper()
    if unit == "G":
        return value * 1024
    return value


def host_numa_nodes() -> list[int]:
    """Return sorted host NUMA node IDs from sysfs."""
    node_dir = "/sys/devices/system/node"
    nodes: list[int] = []
    try:
        for name in os.listdir(node_dir):
            if name.startswith("node") and name[4:].isdigit():
                nodes.append(int(name[4:]))
    except OSError:
        return []
    return sorted(nodes)


def use_numa_topology(enable_numa_topology: bool) -> bool:
    """True when profile requests NUMA and host has exactly 2 NUMA nodes."""
    return enable_numa_topology and len(host_numa_nodes()) == 2


class PcieRootPinning:
    """Pin emulated virtio devices to pcie.0 below PXB bridge slots (0x18+)."""

    _SLOTS = (0x2, 0x3, 0x4, 0x5, 0x6, 0x7)

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._index = 0

    def device_suffix(self) -> str:
        if not self.enabled:
            return ""
        if self._index >= len(self._SLOTS):
            raise RuntimeError("No free pcie.0 slots for emulated PCI devices")
        addr = self._SLOTS[self._index]
        self._index += 1
        return f",bus=pcie.0,addr=0x{addr:x}"


def read_pci_numa_node(bdf: str) -> int:
    """Read PCI device NUMA node from sysfs. Returns -1 if unknown."""
    path = f"/sys/bus/pci/devices/{bdf}/numa_node"
    try:
        with open(path) as f:
            node = int(f.read().strip())
    except (OSError, ValueError):
        return -1
    return node if node >= 0 else -1


def _append_numa_memory(
    cmd: "QemuCommand", mem_mib: int, host_nodes: list[int]
) -> None:
    """Add per-node memory backends and guest NUMA topology to ``cmd``.

    NB: do NOT set prealloc=on on these backends. Under TDX
    (confidential-guest-support=tdx) the guest's actual RAM is private memory
    served from guest_memfd, allocated lazily as the guest accepts pages.
    Preallocating the memory-backend pins a second full copy of pages the guest
    never uses as shared, doubling host memory consumption (~2x guest RAM) and
    OOM-killing the host during pod warmup. host-nodes/policy=bind keeps the
    lazy allocations NUMA-local, which is the only reason these backends exist.
    """
    num_nodes = len(host_nodes)
    per_node_mib = mem_mib // num_nodes
    for i, hnode in enumerate(host_nodes):
        if i == num_nodes - 1:
            node_size_mib = mem_mib - per_node_mib * (num_nodes - 1)
        else:
            node_size_mib = per_node_mib
        cmd.objects.append(
            f"memory-backend-ram,id=mem-node{i},size={node_size_mib}M,"
            f"host-nodes={hnode},policy=bind"
        )
        cmd.numa.append(f"node,nodeid={i},memdev=mem-node{i}")
        cmd.numa.append(f"cpu,node-id={i},socket-id={i}")

    if num_nodes == 2:
        cmd.numa.append("dist,src=0,dst=1,val=21")


class PciTopologyState:
    """Tracks PCIe root port allocation across GPUs, NVSwitches, and IB devices."""

    def __init__(self, start_port: int = 16, start_slot: int = 0x8):
        self.port = start_port
        self.slot = start_slot
        self.func = 0

    def add_device(
        self,
        cmd: "QemuCommand",
        host_bdf: str,
        *,
        rp_id: str,
        chassis: int,
        bar_size_mb: int | None = None,
        bar_index: int | None = None,
    ):
        """Add a vfio-pci device on a new PCIe root port.

        Args:
            cmd: QemuCommand to populate (appends a root port + vfio endpoint).
            host_bdf: host PCI BDF of the device passed through on this root port.
            rp_id: Root port identifier (e.g. 'rp1', 'rp_nvsw1').
            chassis: Chassis number for the root port.
            bar_size_mb: Optional MMIO BAR size hint (fw_cfg opt/ovmf/X-PciMmio64Mb).
            bar_index: 1-based fw_cfg index (only when bar_size_mb is set).
        """
        if self.func == 0:
            cmd.devices.append(
                f"pcie-root-port,port={self.port},chassis={chassis},id={rp_id},"
                f"bus=pcie.0,multifunction=on,addr={self.slot:#x}"
            )
        else:
            cmd.devices.append(
                f"pcie-root-port,port={self.port},chassis={chassis},id={rp_id},"
                f"bus=pcie.0,addr={self.slot:#x}.{self.func:#x}"
            )

        cmd.devices.append(
            f"vfio-pci,host={host_bdf},bus={rp_id},addr=0x0,iommufd=iommufd0"
        )

        if bar_size_mb is not None and bar_index is not None:
            cmd.fw_cfg.append(
                f"name=opt/ovmf/X-PciMmio64Mb{bar_index},string={bar_size_mb}"
            )

        self.port += 1
        self.func = (self.func + 1) % 8
        if self.func == 0:
            self.slot += 1


class NumaPciTopologyState:
    """PXB-PCIe grouped topology: one expander bridge per host NUMA node."""

    def __init__(self, start_port: int = 16):
        self.port = start_port
        self.pxb_created: dict[int, str] = {}
        self.pxb_port_idx: dict[int, int] = {}
        self.pxb_busnr = 128
        self._flat = PciTopologyState(start_port=start_port)

    def _ensure_pxb(self, cmd: "QemuCommand", numa_node: int) -> str:
        if numa_node not in self.pxb_created:
            pxb_id = f"pxb_numa{numa_node}"
            pxb_addr = f"0x{24 + numa_node:x}"
            cmd.devices.append(
                f"pxb-pcie,bus_nr={self.pxb_busnr},id={pxb_id},"
                f"numa_node={numa_node},bus=pcie.0,addr={pxb_addr}"
            )
            self.pxb_created[numa_node] = pxb_id
            self.pxb_port_idx[numa_node] = 0
            self.pxb_busnr += 32
            print(
                f"    Created PXB-PCIe for NUMA node {numa_node} (bus_nr={self.pxb_busnr - 32})"
            )
        return self.pxb_created[numa_node]

    def add_device(
        self,
        cmd: "QemuCommand",
        host_bdf: str,
        *,
        rp_id: str,
        chassis: int,
        numa_node: int,
        bar_size_mb: int | None = None,
        bar_index: int | None = None,
    ):
        """Add a vfio-pci device on a PCIe root port under the PXB for numa_node.

        numa_node is the device's host NUMA node, resolved by the caller (from
        sysfs for the launch path, from a topology fingerprint for offline
        measurement); < 0 (NUMA_NO_NODE — no affinity) falls back to flat
        placement.
        """
        if numa_node < 0:
            self._flat.add_device(
                cmd,
                host_bdf,
                rp_id=rp_id,
                chassis=chassis,
                bar_size_mb=bar_size_mb,
                bar_index=bar_index,
            )
            return

        pxb_bus = self._ensure_pxb(cmd, numa_node)
        port_idx = self.pxb_port_idx[numa_node]
        rp_addr = f"0x{port_idx + 1:x}"
        self.pxb_port_idx[numa_node] = port_idx + 1
        cmd.devices.append(
            f"pcie-root-port,port={self.port},chassis={chassis},id={rp_id},bus={pxb_bus},addr={rp_addr}"
        )
        cmd.devices.append(
            f"vfio-pci,host={host_bdf},bus={rp_id},addr=0x0,iommufd=iommufd0"
        )
        if bar_size_mb is not None and bar_index is not None:
            cmd.fw_cfg.append(
                f"name=opt/ovmf/X-PciMmio64Mb{bar_index},string={bar_size_mb}"
            )
        print(f"    {host_bdf} -> PXB NUMA node {numa_node}")
        self.port += 1


@dataclass
class QemuCommand:
    """A structured TDX-guest QEMU command.

    Builders populate the structured fields (``objects``/``numa``/``devices``/…)
    in composition order; ``to_args()`` renders them into the flat
    ``qemu-system-x86_64`` argv in the one canonical section order QEMU needs
    (objects before the -numa that reference them, drives before devices). The
    launcher renders and runs it; offline measurement reads the fields directly
    (no re-parsing) and rewrites them into tdx-measure metadata.

    ``devices`` is a single ordered list: append order sets PCIe slot assignment
    (via PcieRootPinning), so it is preserved verbatim.
    """

    mem: str
    smp_topology: str
    cpu_args: str
    machine: str
    firmware: str
    process_name: str
    foreground: bool
    logfile: str
    pidfile: str
    accel: str = "kvm"
    tdx_guest: str = (
        '{"qom-type":"tdx-guest","id":"tdx",'
        '"quote-generation-socket":{"type":"vsock","cid":"2","port":"4050"}}'
    )
    # Direct boot (1.4.0+): OVMF boots these kernel/initrd/cmdline directly,
    # dropping GRUB/shim from the measured boot chain. When set, the qcow2 stays
    # attached as the LUKS root but is no longer the boot device (no bootindex).
    # Left None for legacy GRUB boot and the offline ACPI-dump path (rtmr0 is
    # boot-method independent).
    kernel: str | None = None
    initrd: str | None = None
    append: str | None = None
    objects: list[str] = field(default_factory=list)
    numa: list[str] = field(default_factory=list)
    smbios: list[str] = field(default_factory=list)
    drives: list[str] = field(default_factory=list)
    netdevs: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    fw_cfg: list[str] = field(default_factory=list)

    def to_args(self) -> list[str]:
        """Render the flat ``qemu-system-x86_64`` argument list."""
        args = [
            "qemu-system-x86_64",
            "-accel",
            self.accel,
            "-m",
            self.mem,
            "-smp",
            self.smp_topology,
            "-name",
            f"{self.process_name},process={self.process_name},debug-threads=on",
            "-cpu",
            self.cpu_args,
            "-object",
            self.tdx_guest,
        ]
        for o in self.objects:
            args += ["-object", o]
        for n in self.numa:
            args += ["-numa", n]
        args += [
            "-machine",
            self.machine,
            "-bios",
            self.firmware,
            "-nodefaults",
            "-vga",
            "none",
        ]
        for s in self.smbios:
            args += ["-smbios", s]
        if self.foreground:
            args += ["-nographic", "-serial", "mon:stdio"]
        else:
            args += [
                "-nographic",
                "-serial",
                f"file:{self.logfile}",
                "-daemonize",
                "-pidfile",
                self.pidfile,
            ]
        if self.kernel:
            args += ["-kernel", self.kernel]
        if self.initrd:
            args += ["-initrd", self.initrd]
        if self.append is not None:
            args += ["-append", self.append]
        for d in self.drives:
            args += ["-drive", d]
        for nd in self.netdevs:
            args += ["-netdev", nd]
        for dev in self.devices:
            args += ["-device", dev]
        for fc in self.fw_cfg:
            args += ["-fw_cfg", fc]
        return args


def build_base_cmd(
    *,
    mem: str,
    smp_topology: str,
    process_name: str,
    cpu_args: str,
    firmware: str,
    img_path: str,
    foreground: bool,
    pidfile: str,
    logfile: str,
    host_nodes: list[int],
    kernel_path: str,
    initrd_path: str,
    cmdline: str,
    pci_pinning: PcieRootPinning | None = None,
) -> QemuCommand:
    """Build the base QEMU command (TDX, firmware, CPU, memory, direct boot).

    Pure: reads no live hardware. ``host_nodes`` is the explicit guest-NUMA node
    list, fully resolved by the caller — the launcher from sysfs
    (``host_numa_nodes()`` gated by ``use_numa_topology``), the measurement
    adapter from a topology fingerprint. A guest-NUMA topology is built when it
    names >= 2 nodes; ``[]`` builds a flat guest.

    Direct boot (1.4.0+, not optional): OVMF boots ``kernel_path`` / ``initrd_path``
    with ``cmdline`` directly — no GRUB. The qcow2 stays attached as the LUKS root
    but is not the boot device (no ``bootindex``). There is deliberately no GRUB
    fallback: a second boot path would produce a second, network-inconsistent set
    of measurements. The launcher passes the artifacts published with the image;
    the offline ACPI-dump path passes placeholders (RTMR0 is boot-method
    independent and the measured tables don't include the kernel).
    """
    numa_enabled = len(host_nodes) >= 2
    pinning = pci_pinning or PcieRootPinning(numa_enabled)

    if numa_enabled:
        machine = "q35,kernel_irqchip=split,confidential-guest-support=tdx"
    else:
        machine = "q35,kernel_irqchip=split,confidential-guest-support=tdx,memory-backend=mem0"

    cmd = QemuCommand(
        mem=mem,
        smp_topology=smp_topology,
        cpu_args=cpu_args,
        machine=machine,
        firmware=firmware,
        process_name=process_name,
        foreground=foreground,
        logfile=logfile,
        pidfile=pidfile,
        # Pinned SMBIOS identity so per-server motherboard differences don't
        # shift RTMR0 within a profile. Single source of truth: the offline
        # measurement path reads this same builder (build_qemu_command →
        # platform_tables), so launch and measurement can't diverge.
        smbios=[
            "type=1,manufacturer=Chutes,product=TDX-VM,version=1.0,serial=0,uuid=00000000-0000-0000-0000-000000000000",
            "type=2,manufacturer=Chutes,product=TDX-VM,version=1.0,serial=0",
            "type=3,manufacturer=Chutes,version=1.0,serial=0",
        ],
    )

    if numa_enabled:
        mem_mib = _parse_mem_mib(mem)
        _append_numa_memory(cmd, mem_mib, host_nodes)
        print(
            f"NUMA: {len(host_nodes)} guest nodes, "
            f"{mem_mib // len(host_nodes)}M each (approx), host nodes {host_nodes}"
        )
    else:
        cmd.objects.append(f"memory-backend-ram,id=mem0,size={mem}")

    # Direct boot (always): OVMF loads the kernel/initrd/cmdline itself. The qcow2
    # is still the LUKS root, just not the boot device — so no bootindex.
    cmd.kernel = kernel_path
    cmd.initrd = initrd_path
    cmd.append = cmdline

    img_fmt = _block_format(img_path)
    drive_opts = f"file={img_path},if=none,id=virtio-disk0,cache=none,aio=native,format={img_fmt}"
    if img_fmt == "qcow2":
        drive_opts += ",discard=unmap"
    elif img_fmt == "raw":
        drive_opts += ",discard=on,detect-zeroes=on"
    cmd.drives.append(drive_opts)
    dev_opts = f"virtio-blk-pci,drive=virtio-disk0{pinning.device_suffix()}"
    if img_fmt == "raw":
        dev_opts += ",num-queues=4"
    cmd.devices.append(dev_opts)

    return cmd


def build_network(
    cmd: QemuCommand,
    *,
    network_type: str,
    net_iface: str | None,
    ssh_port: int,
    net_queues: int = 4,
    pci_pinning: PcieRootPinning | None = None,
):
    """Add networking configuration to the QemuCommand."""
    pinning = pci_pinning or PcieRootPinning(False)
    if network_type == "tap":
        if not net_iface:
            print("ERROR: --network-type tap requires --net-iface")
            sys.exit(1)
        vectors = 2 * net_queues + 2
        print(
            f"Networking: TAP mode (iface={net_iface}, queues={net_queues}, vhost=on)"
        )
        cmd.netdevs.append(
            f"tap,id=n0,ifname={net_iface},script=no,downscript=no,vhost=on,queues={net_queues}"
        )
        cmd.devices.append(
            f"virtio-net-pci,netdev=n0,mac=52:54:00:12:34:56,mq=on,vectors={vectors},mrg_rxbuf=on"
            f"{pinning.device_suffix()}"
        )
    else:
        print("Networking: Canonical user-mode networking")
        cmd.devices.append(f"virtio-net-pci,netdev=nic0_td{pinning.device_suffix()}")
        cmd.netdevs.append(f"user,id=nic0_td,hostfwd=tcp::{ssh_port}-:22")


def add_volumes(
    cmd: QemuCommand,
    *,
    config_volume: str | None,
    cache_volume: str | None,
    storage_volume: str | None,
    pci_pinning: PcieRootPinning | None = None,
):
    """Add config, cache, and storage volumes to the QemuCommand."""
    pinning = pci_pinning or PcieRootPinning(False)
    if config_volume:
        cmd.drives.append(
            f"file={config_volume},if=none,id=virtio-config,cache=none,format=qcow2,readonly=on"
        )
        cmd.devices.append(
            f"virtio-blk-pci,drive=virtio-config{pinning.device_suffix()}"
        )
    for vol_path, vol_id in [
        (cache_volume, "virtio-cache"),
        (storage_volume, "virtio-storage"),
    ]:
        if not vol_path:
            continue
        vol_fmt = _block_format(vol_path)
        drive_opts = f"file={vol_path},if=none,id={vol_id},cache=none,aio=native,format={vol_fmt}"
        if vol_fmt == "raw":
            drive_opts += ",discard=on,detect-zeroes=on"
        cmd.drives.append(drive_opts)
        dev_opts = f"virtio-blk-pci,drive={vol_id}{pinning.device_suffix()}"
        if vol_fmt == "raw":
            dev_opts += ",num-queues=4"
        cmd.devices.append(dev_opts)


def add_vsock(cmd: QemuCommand, *, pci_pinning: PcieRootPinning | None = None):
    """Add vhost-vsock device to the QemuCommand."""
    pinning = pci_pinning or PcieRootPinning(False)
    cmd.devices.append(f"vhost-vsock-pci,guest-cid=3{pinning.device_suffix()}")
