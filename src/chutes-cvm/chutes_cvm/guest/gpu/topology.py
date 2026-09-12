"""Topology fingerprints: the RTMR0-distinguishing shape of a host + its GPUs.

RTMR0 = f(guest ACPI, QEMU). A ``TopologyFingerprint`` captures every host-instance
fact that moves RTMR0, as the three orthogonal host axes that produce it:

  - ``cpu`` (``CpuTopology``) — guest ``-smp`` (``vcpus`` + ``sockets``) and CPU
    identity (``cpu_vendor`` + ``cpu_processor_id``). Drives the SRAT memory-map (#13)
    and the SMBIOS Type-4 Processor ID (#14).
  - ``mem_gb`` — guest RAM. A scalar, but a distinct host feature from the CPU and the
    GPUs, so it stands on its own rather than hiding inside CpuTopology.
  - ``gpu`` (``GpuTopology`` = ``NumaTopology`` | ``FlatTopology``) — the passed-through
    device layout, which drives the guest NUMA / PXB-PCIe grouping QEMU emits.

This split is what lets ONE ``GpuProfile`` (GPU-model policy only) cover several host
configurations: a B200 on a 192-CPU/2 TB Xeon and on a 288-CPU/3 TB Xeon 6 are two
fingerprints (different cpu + mem_gb, same gpu) of one profile, not two profiles.
Detection builds a live fingerprint (``host_topology_fingerprint``) that drives the launch
``-smp``/``-m``; the API owns the set of known classes and their acceptance (the measurement
generator derives the same fingerprint from each published host-profile document). A fingerprint
with ``cpu_processor_id=None`` is a placeholder (exact CPU model not captured) — the
profile is refused at launch until discover-profile.sh fills it in.

All of these are value types (frozen dataclasses): hashable and compared by value, so
they live in sets. ``NumaTopology`` never equals ``FlatTopology`` (different classes),
which is the "2-node guest-NUMA path" vs "flat fallback" discriminator.
"""

from dataclasses import dataclass


def _node_sig(nodes: tuple[int, ...]) -> str:
    """Compact signature of a per-device NUMA-node vector: ``node{n}`` when every
    device sits on one node (the common case, e.g. ``(0,0,0,0)`` -> ``node0``),
    else the raw per-device vector (e.g. ``(0,0,1,1)`` -> ``0011``)."""
    if len(set(nodes)) == 1:
        return f"node{nodes[0]}"
    return "".join(str(n) for n in nodes)


@dataclass(frozen=True)
class CpuTopology:
    """The CPU half of a fingerprint (RTMR0-determining).

    ``vcpus`` + ``sockets`` become ``-smp``; ``cpu_vendor`` fixes the SRAT memory-hole
    (#13) and ``cpu_processor_id`` (CPUID leaf-1, 8-byte hex) becomes the SMBIOS Type-4
    Processor ID (#14). Guest RAM is a separate host axis — ``TopologyFingerprint.mem_gb``
    — not carried here. Detection fills these from the live host; the measurement generator
    derives the same values from each API host-profile document.
    """

    vcpus: int
    sockets: int
    cpu_vendor: str
    # 8-byte-hex CPUID leaf-1 (SMBIOS Type-4 Processor ID). None = a PLACEHOLDER
    # fingerprint whose exact CPU model has not been captured yet: it does NOT match a
    # live host (which always has a real id), so such a profile is refused at launch
    # until discover-profile.sh fills this in — and offline generation refuses None
    # rather than emit a measurement for the generating host's CPU.
    cpu_processor_id: "str | None" = None

    @property
    def smp_topology(self) -> str:
        """QEMU ``-smp`` string. threads=1 disables guest SMT (each vCPU a core)."""
        cores_per_socket = self.vcpus // self.sockets
        return f"{self.vcpus},sockets={self.sockets},cores={cores_per_socket},threads=1"


@dataclass(frozen=True)
class NumaTopology:
    """GpuTopology, guest-NUMA path (host has exactly 2 NUMA nodes, profile enables it).

    Each field is the per-device host NUMA node, in sorted-BDF order. The
    device->NUMA layout drives QEMU's guest PXB-PCIe grouping and thus RTMR0, so the
    exact node vectors matter, not just how many devices there are. An empty tuple
    means that device class is not passed through for this profile (no NVSwitches / IB).
    """

    gpu_nodes: tuple[int, ...]
    nvswitch_nodes: tuple[int, ...] = ()
    ib_nodes: tuple[int, ...] = ()

    path = "numa"

    @property
    def device_parts(self) -> list[str]:
        """Extra variant-label parts for the passed-through non-GPU devices."""
        parts = []
        if self.nvswitch_nodes:
            parts.append("nvsw-" + _node_sig(self.nvswitch_nodes))
        if self.ib_nodes:
            parts.append("ib-" + _node_sig(self.ib_nodes))
        return parts


@dataclass(frozen=True)
class FlatTopology:
    """GpuTopology, flat fallback (host is not 2-NUMA-node, or profile disables NUMA).

    The guest is a single flat node with no PXB grouping, so RTMR0 depends only on how
    many of each device is passed through — not which host NUMA node each sits on.
    Counts default to 0 for device classes this profile does not pass through.
    """

    gpu_count: int
    nvswitch_count: int = 0
    ib_count: int = 0

    path = "flat"

    @property
    def device_parts(self) -> list[str]:
        """Extra variant-label parts (device *counts*; flat has no per-device node)."""
        parts = []
        if self.nvswitch_count:
            parts.append(f"nvsw{self.nvswitch_count}")
        if self.ib_count:
            parts.append(f"ib{self.ib_count}")
        return parts


# The device-layout half of a fingerprint. NumaTopology != FlatTopology by class, so
# they never compare equal — the guest-NUMA vs flat-fallback discriminator.
GpuTopology = NumaTopology | FlatTopology


@dataclass(frozen=True)
class TopologyFingerprint:
    """The full RTMR0-determining shape of a host: its CPU, memory, and GPU topology.

    Three orthogonal host axes that each move RTMR0: ``cpu`` (CpuTopology), ``mem_gb``
    (guest RAM — a scalar, but a distinct host feature from the CPU and GPUs), and
    ``gpu`` (GpuTopology device layout). Value type (frozen): two fingerprints are equal
    iff all three match.
    """

    cpu: CpuTopology
    mem_gb: int
    gpu: GpuTopology

    @property
    def mem(self) -> str:
        """QEMU ``-m`` string (guest RAM)."""
        return f"{self.mem_gb}G"

    @property
    def variant_label(self) -> str:
        """Deterministic, human-readable variant id: ``<path>-<vcpus>c-<mem>g[-devices]``,
        e.g. ``numa-176c-1944g`` or ``numa-124c-1128g-nvsw-node0``. The profile
        display_name + QEMU version prepend the rest of the teeMeasurements hardware
        name, so this must be unique per profile+qemu (asserted at generation time)."""
        shape = f"{self.cpu.vcpus}c-{self.mem_gb}g"
        return "-".join([self.gpu.path, shape, *self.gpu.device_parts])

    def __str__(self) -> str:
        # Human-readable form for messages/logs (the full dataclass repr is unreadable);
        # repr() still gives the exhaustive field dump for debugging.
        return self.variant_label
