"""tdx-measure metadata for offline ACPI generation, from a QemuCommand.

The shared ``build_qemu_command`` yields a structured ``QemuCommand`` (the launch
command). The offline ACPI dumper needs a slightly different command that yields
the **same measured ACPI** without hardware. ``MeasurementMetadata`` is a view
over that ``QemuCommand`` + the GPU profile that reads its fields directly — no
command re-parsing, so it can't drift from the shared builder — and applies the
dump-side rewrites:

  - **machine**: run plain q35 (``smm=off,pic=off``); drop the tdx-guest object
    (not carried over — the dumper QEMU has no confidential-guest support).
  - **memory**: ``reserve=off`` on every backend (maps any-size guest RAM on a
    small host without allocating it) and strip host-nodes/policy binding.
  - **emulated devices**: replace the boot disk with backing-free slot-fillers so
    pcie.0 slots 0x2-0x7 populate the DSDT without real drives.
  - **passthrough**: swap each ``vfio-pci`` endpoint for a ``pci-bar-stub``
    carrying the device's BAR layout (from the profile), reproducing the per-GPU
    MMIO windows the real BARs would create.
  - **serial**: attach one so COM1 appears in the DSDT.

Reproduces a real launch's measured ``etc/acpi/tables`` byte-for-byte with no GPU
present (validated against box-028). Imports the shared VM lib from
``chutes_cvm.guest``; callers must have ``host-tools/scripts`` on ``sys.path``.
"""

import re
from dataclasses import dataclass
from functools import cached_property

from chutes_cvm.guest.command import MachineSpec, build_qemu_command
from chutes_cvm.guest.gpu.profiles import GpuProfile, PciBar
from chutes_cvm.guest.gpu.topology import TopologyFingerprint
from chutes_cvm.guest.qemu import QemuCommand

# The dumper runs plain q35 (no TDX): the ACPI tables are identical, and the
# container QEMU has no confidential-guest support.
_DUMP_MACHINE = "q35,kernel_irqchip=split,smm=off,pic=off"

# The emulated devices a launch places on pcie.0 (boot disk, net, 3 volumes,
# vsock) occupy slots 0x2-0x7. Their DSDT nodes are slot-populated markers only
# (device-type agnostic), so backing-free fillers reproduce them.
_EMULATED_SLOTS = range(0x2, 0x8)


def _bars_arg(bars: list[PciBar]) -> str:
    """Format a BAR layout as the pci-bar-stub ``bars=`` value (``;``-separated)."""
    parts = []
    for b in bars:
        size = f"{b.size_mb // 1024}G" if b.size_mb % 1024 == 0 else f"{b.size_mb}M"
        parts.append(f"{b.index}:{size}:{b.kind}")
    return ";".join(parts)


def _reserve_off(backend: str) -> str:
    """Strip host-NUMA binding and add reserve=off to a memory-backend object."""
    backend = re.sub(r",host-nodes=\d+", "", backend)
    backend = backend.replace(",policy=bind", "")
    if "reserve=" not in backend:
        backend += ",reserve=off"
    return backend


@dataclass
class MeasurementMetadata:
    """The tdx-measure ``ImageConfig`` for offline dumping a spec's ACPI.

    Build from a measurement ``MachineSpec`` + its ``GpuProfile``; ``to_dict()``
    is the metadata JSON. Reads the shared ``QemuCommand``'s structured fields —
    no re-parsing — so it stays tied to the real launch command.
    """

    spec: MachineSpec
    profile: GpuProfile
    fingerprint: TopologyFingerprint
    acpi_tables: str
    with_smbios: bool = True

    @cached_property
    def cmd(self) -> QemuCommand:
        return build_qemu_command(self.spec)

    @property
    def objects(self) -> list[str]:
        # Only the memory-backends (cmd.objects); the tdx-guest object lives in
        # cmd.tdx_guest and is simply not carried over.
        return [_reserve_off(o) for o in self.cmd.objects]

    @property
    def devices(self) -> list[str]:
        """Fillers for slots 0x2-0x7, then the passthrough topology with stubbed BARs."""
        out = [f"virtio-rng-pci,bus=pcie.0,addr={s:#x}" for s in _EMULATED_SLOTS]
        for dev in self.cmd.devices:
            if dev.startswith("virtio-blk-pci,drive=virtio-disk0"):
                continue  # boot disk — replaced by the slot-fillers above
            if dev.startswith("vfio-pci"):
                out.append(self._swap_endpoint(dev))
            else:
                out.append(dev)  # pxb-pcie / pcie-root-port
        return out

    def _swap_endpoint(self, dev: str) -> str:
        """Swap a ``vfio-pci`` endpoint for a ``pci-bar-stub`` built from the profile's
        ``passthrough`` spec for that bus kind (gpu / nvswitch / ib)."""
        bus = re.search(r"bus=([^,]+)", dev)
        if not bus:
            raise ValueError(f"vfio-pci device without a bus=: {dev!r}")
        rp = bus.group(1)
        if re.fullmatch(r"rp\d+", rp):
            kind = "gpu"
        elif rp.startswith("rp_nvsw"):
            kind = "nvswitch"
        elif rp.startswith("rp_ib"):
            kind = "ib"
        else:
            raise NotImplementedError(f"unrecognized passthrough bus {rp!r}")
        spec = self.profile.passthrough.get(kind)
        if not spec:
            raise ValueError(
                f"profile {self.profile.name!r} has no passthrough[{kind!r}] — capture "
                f"lspci -vvvnn for that device and add it (see discover-profile.sh)"
            )
        return (
            f"pci-bar-stub,bus={rp},bars={_bars_arg(spec.bars)},"
            f"vendor={spec.vendor:#06x},device={int(spec.device_id, 16):#06x},"
            f"class={spec.pci_class:#06x}"
        )

    @property
    def smbios(self) -> list[str]:
        return self.cmd.smbios if self.with_smbios else []

    def to_dict(self) -> dict:
        cmd = self.cmd
        # Flat topologies wire the guest RAM to a machine-level memory-backend
        # (`memory-backend=mem0`); NUMA wires per-node memdevs (`-numa … memdev=`)
        # instead. The dump machine must keep whichever the launch used — without the
        # flat memory-backend, QEMU falls back to allocating the full pc.ram, which a
        # small generating host can't back (the mem0 object carries reserve=off).
        dump_machine = _DUMP_MACHINE
        mem_backend = next(
            (p for p in cmd.machine.split(",") if p.startswith("memory-backend=")),
            None,
        )
        if mem_backend:
            dump_machine = f"{_DUMP_MACHINE},{mem_backend}"
        return {
            "boot_config": {
                "cpus": int(cmd.smp_topology.split(",", 1)[0]),
                "memory": cmd.mem,
                "bios": cmd.firmware,
                "acpi_tables": self.acpi_tables,
                "qemu": {
                    "machine": dump_machine,
                    "cpu": cmd.cpu_args,
                    "accel": cmd.accel,
                    "smp": cmd.smp_topology,
                    "objects": self.objects,
                    "numa": cmd.numa,
                    "smbios": self.smbios,
                    "serial": ["null"],  # adds COM1 to the DSDT
                    "devices": self.devices,
                    "fw_cfg": cmd.fw_cfg,
                    # Pin the SMBIOS Type-4 Processor ID (#14) to the production
                    # CPUID; tdx-measure patches it into the dumped SMBIOS (KVM
                    # can't override the generating host's CPUID). None => unpatched.
                    "processor_id": self.fingerprint.cpu.cpu_processor_id,
                },
            },
            "direct": {"kernel": "/dev/null", "initrd": "/dev/null", "cmdline": ""},
        }
