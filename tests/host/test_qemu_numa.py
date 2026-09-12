"""Unit tests for QEMU NUMA topology helpers."""

from unittest.mock import patch

import pytest
from chutes_cvm.guest.qemu import (
    PcieRootPinning,
    QemuCommand,
    _append_numa_memory,
    _parse_mem_mib,
    add_volumes,
    build_base_cmd,
    safe_vm_mem_gb,
    use_numa_topology,
)


def _empty_cmd() -> QemuCommand:
    """A minimal QemuCommand for exercising a single builder in isolation."""
    return QemuCommand(
        mem="1G",
        smp_topology="1",
        cpu_args="host",
        machine="q35",
        firmware="/x",
        process_name="t",
        foreground=True,
        logfile="/l",
        pidfile="/p",
    )


# ---------------------------------------------------------------------------
# safe_vm_mem_gb — clamp guest RAM to host capacity (TDX mem is unreclaimable)
# ---------------------------------------------------------------------------


def test_safe_mem_clamps_when_request_exceeds_host():
    # 8x141=1128G requested on a ~1024G host genuinely doesn't fit.
    # Flat 64G reserve -> 1024-64 = 960G safe, and 960 < 1128 so it clamps.
    assert safe_vm_mem_gb(1128, 1024) == 960


def test_safe_mem_unchanged_when_request_fits():
    # 4x141=564G on a 1024G host fits under the 960G ceiling.
    assert safe_vm_mem_gb(564, 1024) == 564


def test_safe_mem_allows_profile_sized_guest_on_large_host():
    # Regression: profiles size guest RAM as ~(host - 64G reserve) / gpus, so a
    # B200_XEON6 guest (8x369=2952G) must be allowed on its ~3 TB host. The old
    # 12% reserve (=362G on 3017G) wrongly clamped to 2655G and rejected it; a
    # flat 64G reserve allows it (3017-64 = 2953 >= 2952).
    assert safe_vm_mem_gb(2952, 3017) >= 2952


def test_safe_mem_reserve_on_small_host():
    # 256G host - flat 64G reserve -> 192G safe.
    assert safe_vm_mem_gb(256, 256) == 192


def test_safe_mem_passthrough_when_host_unknown():
    assert safe_vm_mem_gb(1128, None) == 1128
    assert safe_vm_mem_gb(1128, 0) == 1128


def test_safe_mem_never_clamps_upward():
    assert safe_vm_mem_gb(100, 2048) == 100


def test_safe_mem_pathological_tiny_host_returns_request():
    # reserve (64G floor) exceeds host -> can't help, leave request as-is.
    assert safe_vm_mem_gb(50, 32) == 50


@pytest.mark.parametrize(
    ("mem", "expected_mib"),
    [
        ("1536G", 1536 * 1024),
        ("512M", 512),
    ],
)
def test_parse_mem_mib(mem, expected_mib):
    assert _parse_mem_mib(mem) == expected_mib


def test_parse_mem_mib_rejects_invalid():
    with pytest.raises(ValueError, match="Invalid memory size"):
        _parse_mem_mib("1.5G")


def test_use_numa_topology_requires_two_host_nodes():
    with patch("chutes_cvm.guest.qemu.host_numa_nodes", return_value=[0, 1]):
        assert use_numa_topology(True) is True
        assert use_numa_topology(False) is False


def test_use_numa_topology_falls_back_for_non_dual_node():
    with patch("chutes_cvm.guest.qemu.host_numa_nodes", return_value=[0]):
        assert use_numa_topology(True) is False


def test_build_base_cmd_numa_adds_per_node_backends(tmp_path):
    img = tmp_path / "disk.qcow2"
    img.write_bytes(b"")
    cmd = build_base_cmd(
        mem="1024G",
        smp_topology="188,sockets=2,cores=94,threads=1",
        process_name="chutes-td",
        cpu_args="host,-avx10",
        firmware="/tmp/TDVF.fd",
        img_path=str(img),
        foreground=True,
        pidfile="/tmp/pid",
        logfile="/tmp/log",
        host_nodes=[0, 1],
        kernel_path="/boot/vmlinuz",
        initrd_path="/boot/initrd.img",
        cmdline="root=UUID=x ro",
    )
    flat = " ".join(cmd.to_args())
    assert "memory-backend-ram,id=mem-node0" in flat
    assert "memory-backend-ram,id=mem-node1" in flat
    assert "host-nodes=0,policy=bind" in flat
    assert "host-nodes=1,policy=bind" in flat
    assert "-numa node,nodeid=0,memdev=mem-node0" in flat
    assert "-numa dist,src=0,dst=1,val=21" in flat
    assert "memory-backend=mem0" not in flat
    # prealloc must NOT be set under TDX: it pins a second full copy of guest
    # RAM (guest_memfd serves the real private pages), ~2x usage -> host OOM.
    assert "prealloc" not in flat


def test_build_base_cmd_pins_smbios_identity(tmp_path):
    """SMBIOS type 1/2/3 identity is pinned so per-server motherboard
    differences don't shift RTMR0 within a profile. This builder is the single
    source of truth — the offline measurement path reads it too."""
    img = tmp_path / "disk.qcow2"
    img.write_bytes(b"")
    cmd = build_base_cmd(
        mem="512G",
        smp_topology="94,sockets=1,cores=94,threads=1",
        process_name="chutes-td",
        cpu_args="host,-avx10",
        firmware="/tmp/TDVF.fd",
        img_path=str(img),
        foreground=True,
        pidfile="/tmp/pid",
        logfile="/tmp/log",
        host_nodes=[],
        kernel_path="/boot/vmlinuz",
        initrd_path="/boot/initrd.img",
        cmdline="root=UUID=x ro",
    )
    flat = " ".join(cmd.to_args())
    assert (
        "type=1,manufacturer=Chutes,product=TDX-VM,version=1.0,serial=0,"
        "uuid=00000000-0000-0000-0000-000000000000" in flat
    )
    assert "type=2,manufacturer=Chutes,product=TDX-VM,version=1.0,serial=0" in flat
    assert "type=3,manufacturer=Chutes,version=1.0,serial=0" in flat


def test_append_numa_memory_splits_remainder_on_last_node():
    cmd = _empty_cmd()
    _append_numa_memory(cmd, mem_mib=1537, host_nodes=[0, 1])
    assert "size=768M" in " ".join(cmd.objects)
    assert "size=769M" in " ".join(cmd.objects)


def test_direct_boot_emits_kernel_initrd_append_and_drops_bootindex(tmp_path):
    img = tmp_path / "disk.qcow2"
    img.write_bytes(b"")
    cmd = build_base_cmd(
        mem="512G",
        smp_topology="94,sockets=1,cores=94,threads=1",
        process_name="chutes-td",
        cpu_args="host,-avx10",
        firmware="/tmp/TDVF.fd",
        img_path=str(img),
        foreground=True,
        pidfile="/tmp/pid",
        logfile="/tmp/log",
        host_nodes=[],
        kernel_path="/boot/vmlinuz",
        initrd_path="/boot/initrd.img",
        cmdline="root=UUID=abc ro console=ttyS0",
    )
    args = cmd.to_args()
    flat = " ".join(args)
    # Direct-boot args present, cmdline pinned verbatim.
    assert "-kernel" in args and "/boot/vmlinuz" in args
    assert "-initrd" in args and "/boot/initrd.img" in args
    assert args[args.index("-append") + 1] == "root=UUID=abc ro console=ttyS0"
    # qcow2 is still attached as the (LUKS) root disk, just not the boot device.
    assert "file=" + str(img) in flat
    assert "virtio-blk-pci,drive=virtio-disk0" in flat
    assert "bootindex" not in flat


def test_config_volume_uses_explicit_virtio_blk_not_legacy_if_virtio(tmp_path):
    config = tmp_path / "config.qcow2"
    config.write_bytes(b"")
    pinning = PcieRootPinning(True)
    cmd = _empty_cmd()
    add_volumes(
        cmd,
        config_volume=str(config),
        cache_volume=None,
        storage_volume=None,
        pci_pinning=pinning,
    )
    flat = " ".join(cmd.drives + cmd.devices)
    assert "if=virtio" not in flat
    assert "virtio-config" in flat
    assert "virtio-blk-pci,drive=virtio-config,bus=pcie.0" in flat


def test_pcie_root_pinning_assigns_unique_slots():
    pinning = PcieRootPinning(True)
    assert pinning.device_suffix() == ",bus=pcie.0,addr=0x2"
    assert pinning.device_suffix() == ",bus=pcie.0,addr=0x3"
