"""build_qemu_command(spec) must equal driving the low-level builders directly."""

from chutes_cvm.guest.command import DeviceSpec, MachineSpec, build_qemu_command
from chutes_cvm.guest.qemu import NumaPciTopologyState, PciTopologyState, build_base_cmd

_FW = "OVMF.inteltdx.fd"
_SMP = "124,sockets=2,cores=62,threads=1"


def _base(*, host_nodes, mem="768G"):
    return build_base_cmd(
        mem=mem,
        smp_topology=_SMP,
        process_name="chutes-td",
        cpu_args="host,-avx10",
        firmware=_FW,
        img_path="root.qcow2",
        foreground=False,
        pidfile="/dev/null",
        logfile="/dev/null",
        host_nodes=host_nodes,
        # Match the measurement path (build_qemu_command) placeholders so the
        # comparison is over topology, not the boot chain.
        kernel_path="/dev/null",
        initrd_path="/dev/null",
        cmdline="",
    )


def test_numa_spec_matches_direct_builders():
    nodes = [0, 0, 0, 0, 1, 1, 1, 1]
    spec = MachineSpec(
        mem="768G",
        smp_topology=_SMP,
        cpu_args="host,-avx10",
        firmware=_FW,
        host_nodes=[0, 1],
        devices=[
            DeviceSpec(
                rp_id=f"rp{i + 1}",
                chassis=i + 1,
                host_bdf=f"0000:{i + 1:02x}:00.0",
                numa_node=n,
                bar_size_mb=131072,
                bar_index=i + 1,
            )
            for i, n in enumerate(nodes)
        ],
    )
    got = build_qemu_command(spec)

    manual = _base(host_nodes=[0, 1])
    topo = NumaPciTopologyState()
    for i, n in enumerate(nodes):
        topo.add_device(
            manual,
            host_bdf=f"0000:{i + 1:02x}:00.0",
            rp_id=f"rp{i + 1}",
            chassis=i + 1,
            numa_node=n,
            bar_size_mb=131072,
            bar_index=i + 1,
        )
    assert got == manual


def test_flat_spec_matches_direct_builders():
    spec = MachineSpec(
        mem="768G",
        smp_topology=_SMP,
        cpu_args="host,-avx10",
        firmware=_FW,
        host_nodes=[],
        devices=[
            DeviceSpec(
                rp_id=f"rp{i + 1}", chassis=i + 1, host_bdf=f"0000:{i + 1:02x}:00.0"
            )
            for i in range(8)
        ],
    )
    got = build_qemu_command(spec)

    manual = _base(host_nodes=[])
    topo = PciTopologyState()
    for i in range(8):
        topo.add_device(
            manual, host_bdf=f"0000:{i + 1:02x}:00.0", rp_id=f"rp{i + 1}", chassis=i + 1
        )
    assert got == manual
