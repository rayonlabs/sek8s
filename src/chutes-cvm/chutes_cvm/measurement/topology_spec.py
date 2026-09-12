"""Determine the QEMU machine spec for a supported topology, offline.

The measurement side's input-determination: given a ``GpuProfile`` and a topology
fingerprint (``chutes_cvm.guest.gpu.topology``), produce the ``MachineSpec`` that
``chutes_cvm.guest.command.build_qemu_command`` turns into the exact QEMU command a
matching host would launch — with no live hardware. The launcher resolves the
same spec from live detection, so both yield a byte-identical command for a given
topology (see the parity test).

Imports the shared VM lib from the launcher package (``chutes_cvm.guest``); callers
must have ``host-tools/scripts`` on ``sys.path`` (the measurement entrypoints and
tests/measurement/conftest.py arrange this).
"""

from chutes_cvm.guest.command import DeviceSpec, MachineSpec
from chutes_cvm.guest.detection import GUEST_CPU_ARGS
from chutes_cvm.guest.gpu.profiles import GpuProfile
from chutes_cvm.guest.gpu.topology import NumaTopology, TopologyFingerprint

# QEMU version -> guest -cpu string. Every supported release launches with the shared
# GUEST_CPU_ARGS (10.2.1 = 26.04, the only supported host OS); the mapping stays so a future
# QEMU that needs a different -cpu form can be pinned without touching the launch path.
_CPU_ARGS_BY_QEMU = {"10.2.1": GUEST_CPU_ARGS}

# Offline measurement has no real GPU to pass through, but the launch command is
# built the same way (a vfio-pci endpoint per root port). We hand every device
# this placeholder BDF so the shared builder stays identical to the launch path;
# the measurement metadata layer then swaps each vfio-pci endpoint for a
# pci-bar-stub (matched by its ``bus=<rp_id>``), which is what reproduces the
# device's MMIO windows in the measured ACPI without the hardware present.
_PLACEHOLDER_BDF = "0000:00:00.0"


def cpu_args_for_qemu_version(qemu_version: str) -> str:
    """The guest -cpu args for a QEMU version. Defaults to the shared GUEST_CPU_ARGS.
    This is the LAUNCH form (`-cpu host`); measurement uses measurement_cpu_args."""
    return _CPU_ARGS_BY_QEMU.get(qemu_version, GUEST_CPU_ARGS)


def measurement_cpu_args(fingerprint: TopologyFingerprint, qemu_version: str) -> str:
    """The -cpu string for offline MEASUREMENT generation: the launch base plus an
    explicit reconstruction of the fingerprint's production CPU identity, so any host
    (incl. non-Intel) regenerates the production RTMR0. `vendor` fixes #13 (the SRAT
    memory-hole is AMD-guest-gated); the Type-4 Processor ID (#14) is patched in
    separately by tdx-measure from ``fingerprint.cpu_processor_id`` — so BOTH must be
    set.

    Raises if the fingerprint carries no captured CPU model (cpu_processor_id=None):
    generating with the launch base alone would silently emit a measurement for the
    *generating host's* CPU (verified: an unpinned AMD host yields a different, wrong
    RTMR0), so we refuse rather than publish a plausible-but-wrong value. The field comes
    from the class's stored host profile, so the fix is a fresh registration from a host of
    that class (`chutes-cvm host submit-profile`), not a change here.
    Launch always uses cpu_args_for_qemu_version."""
    if fingerprint.cpu.cpu_vendor is None or fingerprint.cpu.cpu_processor_id is None:
        raise ValueError(
            f"fingerprint {fingerprint.variant_label!r} has no captured CPU model "
            f"(cpu_processor_id is None); offline RTMR0 would be generated for the "
            f"generating host's CPU. The stored host profile for this class predates the "
            f"field — have a host of this class re-register with a current chutes-cvm "
            f"(`chutes-cvm host submit-profile`) before generating."
        )
    return (
        f"{cpu_args_for_qemu_version(qemu_version)},vendor={fingerprint.cpu.cpu_vendor}"
    )


def build_topology_spec(
    profile: GpuProfile,
    fingerprint: TopologyFingerprint,
    *,
    cpu_args: str,
    firmware: str,
) -> MachineSpec:
    """Build the ``MachineSpec`` for ``(profile, fingerprint)`` — no live host.

    A ``NumaTopology`` reproduces the guest-NUMA / PXB-PCIe path (per-device node
    from the fingerprint's vectors); a ``FlatTopology`` reproduces the flat path
    (only device counts matter). ``mem`` and ``-smp`` come from the fingerprint's
    host shape; no ``host_bdf`` is set, so only root ports are emitted (the vfio
    endpoints are not part of the measured ACPI).
    """
    gpu_topology = fingerprint.gpu
    numa = isinstance(gpu_topology, NumaTopology)
    if isinstance(gpu_topology, NumaTopology):
        gpu_nodes: list[int] = list(gpu_topology.gpu_nodes)
        nvsw_nodes: list[int] = list(gpu_topology.nvswitch_nodes)
        ib_nodes: list[int] = list(gpu_topology.ib_nodes)
    else:  # FlatTopology — node is irrelevant, only counts matter
        gpu_nodes = [-1] * gpu_topology.gpu_count
        nvsw_nodes = [-1] * gpu_topology.nvswitch_count
        ib_nodes = [-1] * gpu_topology.ib_count

    gpu_count = len(gpu_nodes)
    devices: list[DeviceSpec] = []
    for i, node in enumerate(gpu_nodes):
        bar: dict = {}
        if profile.use_ovmf_mmio_fw_cfg:
            bar = {"bar_size_mb": profile.bar_size_mb, "bar_index": i + 1}
        devices.append(
            DeviceSpec(
                rp_id=f"rp{i + 1}",
                chassis=i + 1,
                host_bdf=_PLACEHOLDER_BDF,
                numa_node=node,
                **bar,
            )
        )
    for j, node in enumerate(nvsw_nodes):
        devices.append(
            DeviceSpec(
                rp_id=f"rp_nvsw{j + 1}",
                chassis=gpu_count + j + 1,
                host_bdf=_PLACEHOLDER_BDF,
                numa_node=node,
            )
        )
    for k, node in enumerate(ib_nodes):
        devices.append(
            DeviceSpec(
                rp_id=f"rp_ib{k + 1}",
                chassis=gpu_count + len(nvsw_nodes) + k + 1,
                host_bdf=_PLACEHOLDER_BDF,
                numa_node=node,
            )
        )

    return MachineSpec(
        mem=fingerprint.mem,
        smp_topology=fingerprint.cpu.smp_topology,
        cpu_args=cpu_args,
        firmware=firmware,
        host_nodes=[0, 1] if numa else [],
        devices=devices,
        process_name="chutes-measure",
    )
