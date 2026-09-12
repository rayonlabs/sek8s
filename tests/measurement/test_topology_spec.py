"""The measurement spec must reproduce the live launcher's RTMR0-shaping args.

build_topology_spec (fingerprint -> MachineSpec) fed through the shared
build_qemu_command must match, byte-for-byte, what the real launch path
(build_base_cmd + passthrough._build_pci_topology) emits with its sysfs lookups
mocked to the same NUMA layout — so offline measurements can't drift from a real
launch. Both paths emit a vfio-pci endpoint per root port; the measurement path
uses a placeholder BDF (later swapped for a pci-bar-stub), so the endpoint lines
are dropped from the comparison — only their BDF differs, and neither the BDF nor
the endpoint device type is settled here.
"""

from unittest.mock import patch

import topology_fixtures as known
from chutes_cvm.guest.command import build_qemu_command
from chutes_cvm.guest.gpu.profiles import GPU_PROFILES
from chutes_cvm.guest.gpu.topology import CpuTopology, NumaTopology, TopologyFingerprint
from chutes_cvm.guest.passthrough import _build_pci_topology
from chutes_cvm.guest.qemu import build_base_cmd, use_numa_topology
from chutes_cvm.measurement.topology_spec import (
    build_topology_spec,
    cpu_args_for_qemu_version,
)

_FW = "OVMF.inteltdx.fd"

# Host-shape fields the fingerprint now carries (drive -smp / -m). Mirror each
# profile's real baselined shape so the synth and live commands agree on mem/smp.
_RTX_SHAPE = dict(
    vcpus=124, sockets=2, cpu_vendor="AuthenticAMD", cpu_processor_id=None
)


def _synth(profile, fingerprint):
    spec = build_topology_spec(
        profile, fingerprint, cpu_args="host,-avx10", firmware=_FW
    )
    return build_qemu_command(spec).to_args()


def _topology_args(cmd):
    """RTMR0-shaping args only: drop the `-device vfio-pci,...` endpoint pairs
    (both paths emit them; only the BDF differs, and the endpoint is swapped for a
    pci-bar-stub in measurement generation)."""
    out = []
    i = 0
    while i < len(cmd):
        if (
            cmd[i] == "-device"
            and i + 1 < len(cmd)
            and cmd[i + 1].startswith("vfio-pci")
        ):
            i += 2
            continue
        # Boot chain (RTMR1/2), not RTMR0-shaping: the launcher passes the real
        # kernel/initrd/cmdline, the measurement path placeholders — drop both.
        if cmd[i] in ("-kernel", "-initrd", "-append"):
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return out


def _live_cmd(
    profile, fingerprint, *, node_by_bdf, host_nodes, gpus, nvsw=None, ib=None
):
    """The command the real launch path produces, with sysfs lookups mocked.

    ``mem`` / ``-smp`` now come from the matched fingerprint's host shape (the
    launcher reads fingerprint.mem/.smp_topology), so the parity comparison uses
    the same values build_topology_spec bakes into the synth command.
    """
    with patch("chutes_cvm.guest.qemu.host_numa_nodes", return_value=host_nodes), patch(
        "chutes_cvm.guest.passthrough.read_pci_numa_node",
        side_effect=lambda b: node_by_bdf.get(b, -1),
    ):
        # Resolve nodes exactly as the launcher does: the mocked sysfs list
        # (host_nodes), gated by the profile's NUMA flag + the 2-node cap, then
        # hand build_base_cmd the explicit list (it no longer reads sysfs).
        numa_active = use_numa_topology(profile.enable_numa_topology)
        cmd = build_base_cmd(
            mem=fingerprint.mem,
            smp_topology=fingerprint.cpu.smp_topology,
            process_name="chutes-measure",
            cpu_args="host,-avx10",
            firmware=_FW,
            img_path="root.qcow2",
            foreground=False,
            pidfile="/dev/null",
            logfile="/dev/null",
            host_nodes=host_nodes if numa_active else [],
            kernel_path="/dev/null",
            initrd_path="/dev/null",
            cmdline="",
        )
        _build_pci_topology(
            cmd,
            gpus=gpus,
            nvswitches_for_vm=nvsw or [],
            ib_devices=ib or [],
            profile=profile,
        )
    return cmd.to_args()


def _bdfs(n, start=1):
    return [f"0000:{start + i:02x}:00.0" for i in range(n)]


def test_numa_4_4_matches_live_path():
    profile = GPU_PROFILES["RTX_PRO_6000"]
    fp = known.RTX_NUMA
    synth = _synth(profile, fp)

    gpus = _bdfs(8)
    live = _live_cmd(
        profile,
        fp,
        node_by_bdf=dict(zip(gpus, fp.gpu.gpu_nodes)),
        host_nodes=[0, 1],
        gpus=gpus,
    )
    assert _topology_args(synth) == _topology_args(live)
    assert any("pxb-pcie" in a for a in synth)
    # measurement emits a placeholder-BDF endpoint (never a real host device)
    assert any("vfio-pci,host=0000:00:00.0" in a for a in synth)


def test_numa_3_5_split_matches_live_path():
    profile = GPU_PROFILES["RTX_PRO_6000"]
    fp = TopologyFingerprint(
        CpuTopology(**_RTX_SHAPE), 768, NumaTopology(gpu_nodes=(0, 0, 0, 1, 1, 1, 1, 1))
    )
    synth = _synth(profile, fp)

    gpus = _bdfs(8)
    live = _live_cmd(
        profile,
        fp,
        node_by_bdf=dict(zip(gpus, fp.gpu.gpu_nodes)),
        host_nodes=[0, 1],
        gpus=gpus,
    )
    assert _topology_args(synth) == _topology_args(live)


def test_flat_topology_matches_live_path_and_has_no_pxb():
    profile = GPU_PROFILES["RTX_PRO_6000"]
    fp = known.RTX_FLAT
    synth = _synth(profile, fp)

    gpus = _bdfs(8)
    live = _live_cmd(profile, fp, node_by_bdf={}, host_nodes=[0, 1, 2, 3], gpus=gpus)
    assert _topology_args(synth) == _topology_args(live)
    assert not any("pxb-pcie" in a for a in synth)


def test_h200_numa_with_nvswitches_matches_live_path():
    profile = GPU_PROFILES["H200"]
    fp = known.H200_XE9680
    synth = _synth(profile, fp)

    gpus = _bdfs(8)
    nvsw = _bdfs(4, start=0x20)
    node_by_bdf = dict(zip(gpus, fp.gpu.gpu_nodes)) | dict(
        zip(nvsw, fp.gpu.nvswitch_nodes)
    )
    live = _live_cmd(
        profile, fp, node_by_bdf=node_by_bdf, host_nodes=[0, 1], gpus=gpus, nvsw=nvsw
    )
    assert _topology_args(synth) == _topology_args(live)
    assert any("rp_nvsw" in a for a in synth)


def test_cpu_args_for_qemu_version():
    assert cpu_args_for_qemu_version("10.2.1") == "host,-avx10"
    # Unknown/unsupported QEMU versions fall back to the same -avx10 form.
    assert cpu_args_for_qemu_version("99.9.9") == "host,-avx10"
