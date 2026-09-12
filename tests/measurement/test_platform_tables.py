"""platform_tables rewrites a measurement MachineSpec into tdx-measure metadata.

These assert the structural rewrites (machine, memory, emulated-device fillers,
vfio->pci-bar-stub swap, serial) that make an offline dump reproduce a real
launch's measured ACPI. The byte-exact acceptance (== box-028) runs in the
tdx-measure container, not here.
"""

import pytest
import topology_fixtures as known
from chutes_cvm.guest.gpu.profiles import GPU_PROFILES
from chutes_cvm.guest.gpu.topology import CpuTopology, NumaTopology, TopologyFingerprint
from chutes_cvm.measurement.platform_tables import MeasurementMetadata
from chutes_cvm.measurement.topology_spec import build_topology_spec

_FW = "/opt/ovmf/OVMF.fd"

# Host-shape fields the fingerprint carries (drive -smp / -m / #14 processor_id).
_H200_SHAPE = dict(
    vcpus=124,
    sockets=2,
    cpu_vendor="GenuineIntel",
    cpu_processor_id="f2060c00fffba91f",
)


def _md(model, fingerprint, **kw):
    profile = GPU_PROFILES[model]
    spec = build_topology_spec(
        profile, fingerprint, cpu_args="host,-avx10", firmware=_FW
    )
    return MeasurementMetadata(
        spec, profile, fingerprint, acpi_tables="/out/acpi.bin", **kw
    ).to_dict()


def _rtx_numa():
    return _md("RTX_PRO_6000", known.RTX_NUMA)


def test_machine_is_rewritten_to_non_tdx():
    q = _rtx_numa()["boot_config"]["qemu"]
    assert q["machine"] == "q35,kernel_irqchip=split,smm=off,pic=off"
    assert not any("tdx-guest" in o for o in q["objects"])


def test_memory_backends_reserve_off_and_unbound():
    q = _rtx_numa()["boot_config"]["qemu"]
    backends = [o for o in q["objects"] if o.startswith("memory-backend-ram")]
    assert backends
    for o in backends:
        assert "reserve=off" in o  # maps multi-TB RAM on a small host
        assert "host-nodes=" not in o and "policy=bind" not in o


def test_emulated_slots_filled_and_boot_disk_dropped():
    q = _rtx_numa()["boot_config"]["qemu"]
    fillers = [d for d in q["devices"] if d.startswith("virtio-rng-pci")]
    slots = {d.split("addr=")[1] for d in fillers}
    assert slots == {f"0x{s:x}" for s in range(2, 8)}  # 0x2-0x7 populated
    assert not any("virtio-disk0" in d for d in q["devices"])  # boot disk gone


def test_vfio_swapped_for_pci_bar_stub_with_profile_bars():
    q = _rtx_numa()["boot_config"]["qemu"]
    assert not any(d.startswith("vfio-pci") for d in q["devices"])
    stubs = [d for d in q["devices"] if d.startswith("pci-bar-stub")]
    assert len(stubs) == 8  # one per GPU
    # each stub carries the RTX BAR layout and stays on its root port
    for i, stub in enumerate(
        sorted(stubs, key=lambda s: int(s.split("bus=rp")[1].split(",")[0])), 1
    ):
        assert f"bus=rp{i}," in stub
        assert "bars=0:64M:p64;2:128G:p64;4:32M:p64" in stub
        assert "vendor=0x10de" in stub


def test_serial_attached_for_com1():
    q = _rtx_numa()["boot_config"]["qemu"]
    assert q["serial"] == ["null"]


def test_smbios_can_be_dropped():
    with_it = _md("RTX_PRO_6000", known.RTX_NUMA, with_smbios=True)
    without = _md("RTX_PRO_6000", known.RTX_NUMA, with_smbios=False)
    assert with_it["boot_config"]["qemu"]["smbios"]
    assert without["boot_config"]["qemu"]["smbios"] == []


def test_flat_topology_generates():
    q = _md("RTX_PRO_6000", known.RTX_FLAT)["boot_config"]["qemu"]
    assert not any("pxb-pcie" in d for d in q["devices"])
    assert sum(d.startswith("pci-bar-stub") for d in q["devices"]) == 8


def test_boot_config_scalars():
    bc = _rtx_numa()["boot_config"]
    assert bc["cpus"] == 124
    assert bc["memory"] == "768G"
    assert bc["acpi_tables"] == "/out/acpi.bin"


def test_nvswitch_endpoint_modeled():
    # NVSwitch is a passthrough device too; its BARs shape the DSDT. H200 models it,
    # so each switch endpoint becomes a pci-bar-stub with the captured layout.
    fp = TopologyFingerprint(
        CpuTopology(**_H200_SHAPE),
        1128,
        NumaTopology(gpu_nodes=(0, 0, 0, 0, 1, 1, 1, 1), nvswitch_nodes=(0, 1, 0, 1)),
    )
    q = _md("H200", fp)["boot_config"]["qemu"]
    nvsw = [
        d for d in q["devices"] if d.startswith("pci-bar-stub") and "bus=rp_nvsw" in d
    ]
    assert len(nvsw) == 4  # one per NVSwitch
    for stub in nvsw:
        assert "bars=0:32M:m64" in stub
        assert "device=0x22a3" in stub and "class=0x0680" in stub


def test_unmodeled_passthrough_raises():
    # A bus kind with no passthrough[...] entry fails loudly (ValueError); an
    # unrecognized bus is NotImplementedError — never a silent wrong measurement.
    profile = GPU_PROFILES["H200"]
    fp = TopologyFingerprint(
        CpuTopology(**_H200_SHAPE), 1128, NumaTopology(gpu_nodes=(0,) * 8)
    )
    md = MeasurementMetadata(
        build_topology_spec(profile, fp, cpu_args="host", firmware=_FW),
        profile,
        fp,
        acpi_tables="/out/a.bin",
    )
    with pytest.raises(ValueError, match=r"passthrough\['ib'\]"):
        md._swap_endpoint("vfio-pci,host=0000:01:00.0,bus=rp_ib1")
    with pytest.raises(NotImplementedError, match="unrecognized passthrough bus"):
        md._swap_endpoint("vfio-pci,host=0000:01:00.0,bus=rp_weird")
