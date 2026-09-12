"""VFIO device binding, PCI cleanup, SR-IOV VF creation, and udev rules.

Host-PCI hardware logic shared by both nouns: `guest launch` (guest/passthrough.py) binds
devices for a VM, and `host vfio-wedged` (host/cli.py) probes the same subsystem. Lives at the
package root — like proc/paths — so neither noun depends on the other's package.
"""

import concurrent.futures
import os
import time

from chutes_cvm import proc

# Number of SR-IOV VFs to create per InfiniBand PF for VM passthrough
IB_VFS_PER_PF = 1


def ensure_sriov_vfs(pf_bdf: str, num_vfs: int = IB_VFS_PER_PF) -> bool:
    """Create SR-IOV VFs on a Physical Function. Returns True if successful.

    Writes to /sys/bus/pci/devices/<pf>/sriov_numvfs. PF stays bound to mlx5_core.
    """
    sriov_path = f"/sys/bus/pci/devices/{pf_bdf}/sriov_numvfs"
    if not os.path.exists(sriov_path):
        return False
    try:
        with open(sriov_path, "r") as f:
            current = int(f.read().strip())
        if current >= num_vfs:
            return True
        if current > 0:
            with open(sriov_path, "w") as f:
                f.write("0")
    except (OSError, ValueError):
        return False
    try:
        with open(sriov_path, "w") as f:
            f.write(str(num_vfs))
        return True
    except OSError:
        return False


def load_vfio_modules():
    """Load VFIO kernel modules required for PCI passthrough."""
    modules = ["vfio_pci", "vfio_iommu_type1", "vfio_virqfd"]
    for module in modules:
        try:
            proc.run(["modprobe", module], check=False, capture_output=True)
        except Exception:  # nosec B110
            pass


def bind_device_to_vfio(device_bdf: str):
    """Bind a single device to vfio-pci using driver_override method.

    If the device is already bound (e.g. mlx5_core for Mellanox IB), we must
    unbind it first; driver_override + probe alone may not take over.
    """
    driver_override_path = f"/sys/bus/pci/devices/{device_bdf}/driver_override"
    driver_link = f"/sys/bus/pci/devices/{device_bdf}/driver"
    try:
        with open(driver_override_path, "w") as f:
            f.write("vfio-pci")
        # Unbind from current driver if bound (e.g. mlx5_core for Mellanox IB)
        if os.path.islink(driver_link):
            driver_name = os.path.basename(os.path.realpath(driver_link))
            if driver_name != "vfio-pci":
                unbind_path = f"/sys/bus/pci/drivers/{driver_name}/unbind"
                if os.path.exists(unbind_path):
                    with open(unbind_path, "w") as f:
                        f.write(device_bdf)
        with open("/sys/bus/pci/drivers_probe", "w") as f:
            f.write(device_bdf)
    except Exception as e:
        print(f"  Warning: Failed to bind {device_bdf} to vfio-pci: {e}")


def _get_bound_driver(device_bdf: str) -> str | None:
    """Return the driver name currently bound to a PCI device, or None."""
    driver_link = f"/sys/bus/pci/devices/{device_bdf}/driver"
    if os.path.islink(driver_link):
        return os.path.basename(os.path.realpath(driver_link))
    return None


def _is_vfio_bound(device_bdf: str) -> bool:
    """True if the device is on vfio-pci AND exposes a vfio-dev cdev.

    QEMU's iommufd path opens /sys/bus/pci/devices/<bdf>/vfio-dev/<cdev>; a
    device that is on vfio-pci but has not yet created that cdev (still settling
    after a CC/PPCIe-mode reset) is not yet usable for passthrough, so both
    conditions must hold.
    """
    if _get_bound_driver(device_bdf) != "vfio-pci":
        return False
    try:
        return bool(os.listdir(f"/sys/bus/pci/devices/{device_bdf}/vfio-dev"))
    except OSError:
        return False


def bind_explicit_devices_to_vfio(devices: list[str], settle_timeout: float = 15.0):
    """Bind only the given BDFs to vfio-pci and verify each one took.

    Matches setup-gpus.sh semantics: explicit device list only, no bridges
    or unrelated fabric endpoints.

    The bind probe can fail silently: a GPU still re-enumerating after its
    CC/PPCIe-mode reset, dropping to D3 once power control is restored to auto,
    or re-claimed by the nvidia driver will not land on vfio-pci even though no
    exception is raised. QEMU then dies at launch with an opaque "couldn't open
    .../vfio-dev" error. We poll every device until it exposes a vfio-dev cdev
    (re-probing stragglers, which also re-unbinds a driver that re-grabbed it),
    and raise a clear, actionable error if any never bind.
    """
    load_vfio_modules()
    for device in devices:
        bind_device_to_vfio(device)

    deadline = time.time() + settle_timeout
    pending = [d for d in devices if not _is_vfio_bound(d)]
    while pending and time.time() < deadline:
        time.sleep(2)
        for device in pending:
            if not _is_vfio_bound(device):
                bind_device_to_vfio(device)  # re-probe a straggler
        pending = [d for d in pending if not _is_vfio_bound(d)]

    for device in devices:
        if device not in pending:
            print(f"    {device} → vfio-pci")

    if pending:
        details = ", ".join(
            f'{d} (driver={_get_bound_driver(d) or "none"})' for d in pending
        )
        raise RuntimeError(
            f"Failed to bind device(s) to vfio-pci after {settle_timeout:.0f}s: "
            f"{details}. A GPU that did not survive its CC/PPCIe-mode reset "
            f"(config space reads 0xffff) or was re-claimed by the nvidia driver "
            f"causes this. Check `lspci -nnks <bdf>` and `dmesg`; a host reboot "
            f"usually clears a wedged GPU. Aborting before QEMU launch."
        )


def _sysfs_write(path: str, value: str, timeout: float = 10.0) -> bool:
    """Write to a sysfs file via subprocess with a timeout.

    Direct Python file writes to sysfs can block in uninterruptible kernel
    sleep when a PCI device's config space is inaccessible.  Spawning a
    subprocess lets us enforce a timeout and continue even if the kernel
    operation hangs.
    """
    try:
        proc.run(
            ["sudo", "bash", "-c", f"echo {value} > {path}"],
            timeout=timeout,
            capture_output=True,
        )
        return True
    except proc.TimeoutExpired:
        return False
    except OSError:
        return False


def has_stale_vfio_devices(devices: list[str]) -> bool:
    """Return True if any device in the list is currently bound to vfio-pci."""
    return any(_get_bound_driver(bdf) == "vfio-pci" for bdf in devices)


def _d_state_pci_tasks() -> list[str]:
    """Return cmdlines of D-state vfio unbind or nvidia-gpu-tools tasks."""
    try:
        result = proc.run(
            ["ps", "-eo", "stat,args"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (proc.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    tasks: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("D"):
            continue
        if "nvidia-gpu-tools" in line or "vfio-pci/unbind" in line:
            tasks.append(line.strip())
    return tasks


def pci_operations_wedged() -> bool:
    """Return True if PCI sysfs or nvidia-gpu-tools ops are stuck in D state.

    When vfio unbind or gpu-tools hang in uninterruptible sleep, further SBR or
    CC-mode commands will also hang until the host is rebooted.
    """
    return bool(_d_state_pci_tasks())


def wait_pci_operations_idle(timeout_secs: float = 90.0) -> bool:
    """Wait for in-flight vfio/gpu-tools D-state tasks to finish.

    A subprocess unbind timeout does not always mean the kernel op failed —
    the sysfs write may still complete shortly after.  Returns True when no
    D-state PCI tasks remain.
    """
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if not pci_operations_wedged():
            return True
        time.sleep(2)
    return False


def unbind_non_vfio_drivers(devices: list[str]) -> list[str]:
    """Unbind devices from any non-vfio driver (e.g. nouveau, nvidia).

    GPU passthrough hosts should not have host GPU drivers loaded, but if
    nouveau or nvidia is bound (missing blacklist), the device must be freed
    before nvidia-gpu-tools can safely access config space.

    Returns list of BDFs that were unbound.
    """
    unbound = []
    for bdf in devices:
        driver = _get_bound_driver(bdf)
        if driver is None or driver == "vfio-pci":
            continue
        print(f"    {bdf} bound to {driver} — unbinding...")
        unbind_path = f"/sys/bus/pci/drivers/{driver}/unbind"
        ok = _sysfs_write(unbind_path, bdf, timeout=10.0)
        if ok:
            unbound.append(bdf)
            print(f"    {bdf} unbound from {driver}")
        else:
            print(f"    Warning: could not unbind {bdf} from {driver} (timeout)")
    return unbound


def unbind_stale_vfio_devices(
    devices: list[str],
    per_device_timeout: float = 45.0,
) -> int:
    """Unbind devices from vfio-pci and clear driver_override.

    After a QEMU session exits, devices remain bound to vfio-pci with stale
    iommufd references.  Unbinding releases those references so the next
    bind_explicit_devices_to_vfio() creates fresh state.

    This is lighter than PCI remove+rescan: the device stays in the kernel's
    PCI subsystem and no config-space teardown is needed, avoiding hangs on
    devices that are slow to respond after SBR.

    Devices may need up to ~30 seconds to become responsive after an SBR
    reset.  All unbinds are fired in parallel so every device gets the full
    timeout window; the wall-clock cost is max(device_times) instead of
    sum(device_times).

    On first boot (no previous QEMU session), devices won't be on vfio-pci
    and this function is a no-op.
    """
    stale = [bdf for bdf in devices if _get_bound_driver(bdf) == "vfio-pci"]
    if not stale:
        return 0

    def _unbind_one(bdf: str) -> tuple[str, bool]:
        unbind_path = "/sys/bus/pci/drivers/vfio-pci/unbind"
        override_path = f"/sys/bus/pci/devices/{bdf}/driver_override"
        ok = _sysfs_write(unbind_path, bdf, timeout=per_device_timeout)
        if ok:
            _sysfs_write(override_path, "", timeout=5.0)
        return bdf, ok

    print(f"    Unbinding {len(stale)} device(s) in parallel...", flush=True)
    results: dict[str, bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(stale)) as pool:
        futures = {pool.submit(_unbind_one, bdf): bdf for bdf in stale}
        for future in concurrent.futures.as_completed(futures):
            bdf, ok = future.result()
            results[bdf] = ok
            if ok:
                print(f"    {bdf} unbound")
            else:
                print(
                    f"    Warning: {bdf} unbind timed out after {per_device_timeout}s"
                )

    succeeded = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if not v)
    if succeeded:
        print(f"  Unbound {succeeded} stale vfio-pci device(s)")
    if failed:
        print(
            f"  Warning: {failed} device(s) could not be unbound (may need host reboot)"
        )
    return failed


def install_udev_rules(scripts_dir: str):
    """Install vfio-passthrough udev rules if not already present."""
    udev_rules_src = os.path.join(scripts_dir, "devices", "vfio-passthrough.rules")
    udev_rules_dst = "/etc/udev/rules.d/vfio-passthrough.rules"
    if not os.path.exists(udev_rules_src):
        raise FileNotFoundError(
            f"Udev rules file not found: {udev_rules_src}. "
            "This file should be in the scripts directory."
        )
    if not os.path.exists(udev_rules_dst):
        print("  Installing udev rules...")
        proc.check_call(
            ["sudo", "cp", udev_rules_src, "/etc/udev/rules.d/"],
            stderr=proc.STDOUT,
        )
        proc.check_call(
            ["sudo", "udevadm", "control", "--reload-rules"],
            stderr=proc.STDOUT,
        )
        proc.check_call(
            ["sudo", "udevadm", "trigger"],
            stderr=proc.STDOUT,
        )
    else:
        print("  Udev rules already present (skipping install)")
