"""GPU passthrough for QEMU using per-SKU GpuProfile rules."""

import time

from chutes_cvm import proc
from chutes_cvm.guest.detection import (
    detect_cx7_bridge_pfs,
    detect_infiniband_pfs,
    detect_infiniband_vfs,
    detect_nvidia_gpus,
    detect_nvswitches,
    get_gpu_bdfs,
    get_gpu_models_from_lspci,
)
from chutes_cvm.guest.gpu.profiles import GpuProfile, resolve_profile
from chutes_cvm.guest.gpu.tools import ensure_gpu_tools_available
from chutes_cvm.guest.qemu import (
    NumaPciTopologyState,
    PciTopologyState,
    QemuCommand,
    read_pci_numa_node,
    use_numa_topology,
)
from chutes_cvm.paths import SCRIPTS_DIR
from chutes_cvm.vfio import (
    bind_explicit_devices_to_vfio,
    ensure_sriov_vfs,
    has_stale_vfio_devices,
    install_udev_rules,
    pci_operations_wedged,
    unbind_non_vfio_drivers,
    unbind_stale_vfio_devices,
    wait_pci_operations_idle,
)

_gpu_tools_cmd: str | None = None


GPU_TOOLS_TIMEOUT_SECS = 120


def _run_gpu_tools(*args: str):
    """Run an nvidia-gpu-tools command with sudo.

    Resolves and caches the tool path on first call via ensure_gpu_tools_available().
    Times out after GPU_TOOLS_TIMEOUT_SECS to prevent indefinite hangs when GPU
    hardware is wedged at the PCIe level.
    """
    global _gpu_tools_cmd
    if _gpu_tools_cmd is None:
        print("  Ensuring GPU admin tools are available...")
        _gpu_tools_cmd = ensure_gpu_tools_available()
    cmd = ["sudo", _gpu_tools_cmd, *args]
    try:
        proc.run(
            cmd,
            check=True,
            stderr=proc.STDOUT,
            timeout=GPU_TOOLS_TIMEOUT_SECS,
        )
    except proc.TimeoutExpired:
        raise RuntimeError(
            f"nvidia-gpu-tools timed out after {GPU_TOOLS_TIMEOUT_SECS}s "
            f"(args: {args}). GPU hardware may be wedged — a host reboot is "
            f"likely required to recover PCIe state."
        )


def _check_fabric_manager(profile: GpuProfile):
    """Raise if Fabric Manager is required by the profile but not running.

    FM must be active before CC mode SBR so GPUs properly re-initialize their
    NVLink connections to the NVSwitches after each reset. Without FM, some
    GPUs may be left mid-initialization and appear as ERR! in the guest.
    """
    if not profile.requires_fabric_manager:
        return
    try:
        result = proc.run(
            ["systemctl", "is-active", "nvidia-fabricmanager"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip() == "active":
            return
    except (proc.TimeoutExpired, OSError):
        pass
    raise RuntimeError(
        f"nvidia-fabricmanager is not running (required for {profile.name}). "
        "The NVSwitch fabric will not initialize properly without it, causing "
        "GPU ERR! states in the guest.\n"
        "Run host setup to install and start it:\n"
        "  chutes-cvm host setup"
    )


def _configure_nvswitches(
    nvswitches: list[str],
    profile: GpuProfile,
    total_gpus: int,
):
    """Configure NVSwitches before VFIO binding (PPCIe mode only)."""
    if not (profile.should_passthrough_nvswitches(total_gpus) and nvswitches):
        return
    print("  Configuring NVSwitches for PPCIe mode...")
    for nvsw in nvswitches:
        print(f"  Preparing NVSwitch {nvsw} for PPCIe")
        _run_gpu_tools(
            "--set-cc-mode=off", "--reset-after-cc-mode-switch", f"--gpu-bdf={nvsw}"
        )
        _run_gpu_tools(
            "--set-ppcie-mode=on",
            "--reset-after-ppcie-mode-switch",
            f"--gpu-bdf={nvsw}",
        )


def _configure_gpus(
    gpus: list[str],
    profile: GpuProfile,
    total_gpus: int,
):
    """Configure each GPU's CC/PPCIe mode before VFIO binding."""
    print("  Configuring GPUs...")
    for gpu in gpus:
        mode_str = profile.describe_mode(total_gpus)
        print(f"  Preparing GPU {gpu} ({profile.name}) for {mode_str}")

        for tool_args in profile.get_cc_mode_args(total_gpus):
            _run_gpu_tools(*tool_args, f"--gpu-bdf={gpu}")


def _device_config_readable(bdf: str) -> bool:
    """Return True if the device's PCI config space responds (vendor ID read)."""
    vendor_path = f"/sys/bus/pci/devices/{bdf}/vendor"
    try:
        result = proc.run(
            ["cat", vendor_path],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() != b"0xffff"
    except (proc.TimeoutExpired, OSError):
        return False


def _wait_devices_ready(devices: list[str], timeout_secs: int = 30) -> bool:
    """Poll until all devices respond to config-space reads, or timeout."""
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if all(_device_config_readable(bdf) for bdf in devices):
            return True
        time.sleep(2)
    unready = [bdf for bdf in devices if not _device_config_readable(bdf)]
    if unready:
        print(f"  Warning: devices still unresponsive after {timeout_secs}s: {unready}")
    return not unready


SBR_SETTLE_SECS = 30


def _prepare_devices(
    gpus: list[str],
    nvswitches: list[str],
    ib_devices: list[str],
    profile: GpuProfile,
):
    """Clean stale PCI state, configure CC/PPCIe modes, bind to vfio-pci, udev.

    Strategy: try lightweight unbind first (works after clean VM shutdown).
    Only escalate to SBR if unbind fails (device truly wedged from crash).
    """
    total_gpus = len(gpus)

    all_devices = list(gpus)
    if profile.should_passthrough_nvswitches(total_gpus) and nvswitches:
        all_devices.extend(nvswitches)
    if ib_devices:
        all_devices.extend(ib_devices)

    if pci_operations_wedged():
        raise RuntimeError(
            "PCI operations are wedged (uninterruptible D-state tasks from a "
            "previous vfio unbind or nvidia-gpu-tools run). SBR cannot run in "
            "this state — reboot the host, then retry `chutes-cvm guest launch`."
        )

    _check_fabric_manager(profile)

    if has_stale_vfio_devices(all_devices):
        print("  Stale vfio-pci devices detected from previous session")
        print(
            "  Unbinding stale vfio-pci devices (no SBR needed for clean shutdown)..."
        )
        unbind_failed = unbind_stale_vfio_devices(all_devices)

        if pci_operations_wedged():
            print("  Waiting for in-flight vfio unbind(s) to finish...")
            if not wait_pci_operations_idle(timeout_secs=90):
                raise RuntimeError(
                    "vfio-pci unbind wedged the PCI subsystem (D-state tasks). "
                    "SBR cannot run until the host is rebooted."
                )

        needs_sbr = unbind_failed > 0 or has_stale_vfio_devices(all_devices)
        if needs_sbr:
            if pci_operations_wedged():
                raise RuntimeError(
                    "vfio-pci unbind wedged the PCI subsystem (D-state tasks). "
                    "SBR cannot run until the host is rebooted."
                )
            sbr_args = profile.get_sbr_reset_args()
            print(
                "  Some devices could not be unbound — escalating to SBR reset "
                f'({profile.name}: {" ".join(sbr_args)})...'
            )
            _run_gpu_tools(*sbr_args)
            print(
                f"  Waiting {SBR_SETTLE_SECS}s for devices to re-initialize after SBR..."
            )
            time.sleep(SBR_SETTLE_SECS)
            print("  Verifying device responsiveness...")
            if not _wait_devices_ready(all_devices):
                raise RuntimeError(
                    "Devices unresponsive after SBR reset. "
                    "A host reboot is likely required."
                )
            print("  Retrying unbind after SBR...")
            unbind_stale_vfio_devices(all_devices)

    # Unbind from host GPU drivers (nouveau, nvidia) if present.
    # These must not hold the device during CC/PPCIe mode configuration.
    freed = unbind_non_vfio_drivers(all_devices)
    if freed:
        print(
            f"  Unbound {len(freed)} device(s) from host GPU driver "
            "(nouveau/nvidia blacklist may be missing — run host-setup)"
        )

    if not _wait_devices_ready(all_devices, timeout_secs=10):
        raise RuntimeError(
            "GPU/NVSwitch devices not responding to config-space reads. "
            "Cannot proceed with CC/PPCIe mode configuration. "
            "A host reboot may be required."
        )

    _configure_nvswitches(nvswitches, profile, total_gpus)
    _configure_gpus(gpus, profile, total_gpus)

    print("  Binding devices to vfio-pci (explicit BDF list)...")
    bind_explicit_devices_to_vfio(all_devices)

    install_udev_rules(str(SCRIPTS_DIR))


def _build_pci_topology(
    cmd: QemuCommand,
    gpus: list[str],
    nvswitches_for_vm: list[str],
    ib_devices: list[str],
    profile: GpuProfile,
):
    """Add GPU, NVSwitch, and IB devices to the QemuCommand's PCI topology."""
    numa = use_numa_topology(profile.enable_numa_topology)
    topo: "PciTopologyState | NumaPciTopologyState"
    if numa:
        print("  PCI topology: NUMA-local PXB-PCIe bridges")
        topo = NumaPciTopologyState()
    else:
        topo = PciTopologyState()

    def _add(host_bdf, rp_id, chassis, **bar):
        # On the NUMA path, resolve the device's node from sysfs here and pass it
        # as placement; add_device no longer reads sysfs, so offline measurement
        # generation can supply the node from a topology fingerprint instead.
        if numa:
            bar["numa_node"] = read_pci_numa_node(host_bdf)
        topo.add_device(cmd, host_bdf=host_bdf, rp_id=rp_id, chassis=chassis, **bar)

    print(f"  Adding {len(gpus)} GPU(s) to PCI topology...")
    if profile.use_ovmf_mmio_fw_cfg:
        mmio_note = f"fw_cfg BAR hint {profile.bar_size_mb} MB per GPU"
    else:
        mmio_note = (
            f"OVMF auto-sizes MMIO window (no fw_cfg; "
            f"~{profile.bar_size_mb} MB BAR per {profile.name} GPU)"
        )
    print(f"    MMIO: {mmio_note}")
    for i, gpu in enumerate(gpus):
        bar_kwargs: dict = {}
        if profile.use_ovmf_mmio_fw_cfg:
            bar_kwargs = {
                "bar_size_mb": profile.bar_size_mb,
                "bar_index": i + 1,
            }
            print(f"    GPU {gpu}: {profile.name}, BAR fw_cfg {profile.bar_size_mb} MB")
        else:
            print(f"    GPU {gpu}: {profile.name}")
        _add(gpu, f"rp{i + 1}", i + 1, **bar_kwargs)

    if nvswitches_for_vm:
        print(f"  Adding {len(nvswitches_for_vm)} NVSwitch(es) to PCI topology...")
    for j, nvsw in enumerate(nvswitches_for_vm):
        _add(nvsw, f"rp_nvsw{j + 1}", len(gpus) + j + 1)

    if ib_devices:
        print(f"  Adding {len(ib_devices)} InfiniBand device(s) to PCI topology...")
    for k, ib_dev in enumerate(ib_devices):
        _add(ib_dev, f"rp_ib{k + 1}", len(gpus) + len(nvswitches_for_vm) + k + 1)

    print(
        f"  Passthrough configured: {len(gpus)} GPU(s), "
        f"{len(nvswitches_for_vm)} NVSwitch(es), "
        f"{len(ib_devices)} IB device(s)"
    )


def setup_passthrough(cmd: QemuCommand):
    """Detect passthrough devices, prepare and bind them on the host, extend the QemuCommand."""
    gpus = get_gpu_bdfs()
    if not gpus:
        gpus = detect_nvidia_gpus()
    if not gpus:
        return

    gpu_models = get_gpu_models_from_lspci(gpus)
    profile = resolve_profile(gpu_models)
    total_gpus = len(gpus)

    nvswitches = (
        detect_nvswitches() if profile.should_passthrough_nvswitches(total_gpus) else []
    )

    ib_devices: list[str] = []
    if profile.should_passthrough_infiniband:
        # Exclude CX7 NVSwitch bridge PFs (SMDL=SW_MNG in VPD) — these must
        # remain on the host for Fabric Manager to manage the NVSwitch fabric.
        # Only regular CX7 NIC PFs should produce VFs for guest passthrough.
        cx7_bridge_pfs = detect_cx7_bridge_pfs()
        if cx7_bridge_pfs:
            print(
                f"  Detected {len(cx7_bridge_pfs)} CX7 NVSwitch bridge PF(s) "
                f"(host-only, excluded from passthrough): {cx7_bridge_pfs}"
            )
        ib_pfs = detect_infiniband_pfs(exclude_bdfs=cx7_bridge_pfs)
        if ib_pfs:
            print(f"  Creating SR-IOV VFs from {len(ib_pfs)} InfiniBand PF(s)...")
            for pf in ib_pfs:
                if ensure_sriov_vfs(pf):
                    print(f"    {pf} → VF(s) created")
                else:
                    print(f"    Warning: Could not create VFs on {pf}")
            ib_devices = detect_infiniband_vfs(ib_pfs)
            if not ib_devices:
                print("  Warning: No InfiniBand VFs found after creation")

    print(f"  Detected {len(gpus)} GPUs: {gpus}")
    if nvswitches:
        print(f"  Detected {len(nvswitches)} NVSwitches: {nvswitches}")
    if ib_devices:
        print(f"  Detected {len(ib_devices)} InfiniBand device(s): {ib_devices}")
    print(f"  Mode: {profile.describe_mode(total_gpus)}")

    _prepare_devices(gpus, nvswitches, ib_devices, profile)
    cmd.objects.append("iommufd,id=iommufd0")

    nvswitches_for_vm = (
        nvswitches if profile.should_passthrough_nvswitches(total_gpus) else []
    )

    _build_pci_topology(cmd, gpus, nvswitches_for_vm, ib_devices, profile)
