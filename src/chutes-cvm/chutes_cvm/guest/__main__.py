"""The low-level TDX VM boot primitive — the raw QEMU boot with GPU-passthrough sizing.

This is not a CLI command. The end-to-end orchestrator (`chutes-cvm guest launch`,
`chutes_cvm.guest.launch`) calls ``main()`` here as its final step via a Python import,
passing an assembled argv. It remains runnable as ``python -m chutes_cvm.guest`` for
low-level debugging, but miners always use `chutes-cvm guest launch`.
"""

import argparse
import os
import signal
import sys
import time

from chutes_cvm import proc
from chutes_cvm.guest.detection import (
    GUEST_CPU_ARGS,
    detect_gpu_numa_nodes,
    detect_host_mem_gb,
    detect_nvidia_gpus,
    detect_profile,
    get_gpu_bdfs,
    verify_host_qemu_supported,
)
from chutes_cvm.guest.direct_boot import direct_boot_artifacts
from chutes_cvm.guest.gpu.profiles import (  # noqa: F401 — available for introspection
    GPU_PROFILES,
)
from chutes_cvm.guest.passthrough import setup_passthrough
from chutes_cvm.guest.post_launch import apply_post_launch_tuning
from chutes_cvm.guest.qemu import (
    PcieRootPinning,
    add_volumes,
    add_vsock,
    build_base_cmd,
    build_network,
    host_numa_nodes,
    safe_vm_mem_gb,
    use_numa_topology,
)
from chutes_cvm.paths import firmware_dir

PIDFILE = "/tmp/tdx-td-pid.pid"  # nosec B108
LOGFILE = "/tmp/tdx-guest-td.log"  # nosec B108
PROCESS_NAME = "chutes-td"

DEFAULT_MEM = "100G"
DEFAULT_VCPUS = "32"

# TDVF MUST NOT be overridden by user config (MRTD depends on it).
# The filename is selected per GPU profile; see GpuProfile.firmware_filename.
_DEFAULT_FIRMWARE = "OVMF.inteltdx.fd"


def _firmware_path(filename: str = _DEFAULT_FIRMWARE) -> str:
    return str(firmware_dir() / filename)


def print_vm_status(ssh_port: int, show_ssh: bool = False):
    try:
        with open(PIDFILE) as pid_file:
            pid = int(pid_file.read())
            print(f"TDX VM running with PID: {pid}")
            if show_ssh:
                print("Login:")
                print(f"   ssh -p {ssh_port} root@<host-ip>")
    except Exception:  # nosec B110
        pass


def stop_existing_vm():
    print("Force-stopping VM (SIGTERM to QEMU)...")
    try:
        with open(PIDFILE) as pid_file:
            pid = int(pid_file.read().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(3)
        os.remove(PIDFILE)
    except FileNotFoundError:
        pass


def launch_vm(args) -> int:

    print("Starting TDX VM...")

    # Fail early if the host QEMU isn't the one baselined for its OS (moves RTMR0).
    verify_host_qemu_supported()

    mem = DEFAULT_MEM
    vcpus = DEFAULT_VCPUS
    smp_topology = f"{DEFAULT_VCPUS},sockets=1,cores={DEFAULT_VCPUS},threads=1"
    profile = None
    gpus = []

    if args.pass_gpus:
        # detect_profile resolves the GPU-model profile AND this host's full RTMR0
        # fingerprint, and raises if the live host isn't baselined (the fingerprint
        # match subsumes the old separate CPU-identity guard: device layout + vendor +
        # vcpus + mem, plus the exact CPU model once its processor_id is captured).
        profile, fingerprint = detect_profile()
        gpus = get_gpu_bdfs() or detect_nvidia_gpus()
        total_gpus = len(gpus)
        # Guest -smp / RAM come from the matched fingerprint, not the host's raw
        # capacity: they shape the guest ACPI/memory-map and therefore RTMR0, so they
        # must be the exact baselined values. We never resize to the host — but we do
        # refuse to launch if the guest RAM cannot physically fit: TDX guest memory is
        # pinned and unreclaimable, so an over-large guest OOM-kills the host instead
        # of paging. Aborting is measurement-safe — it never changes the VM.
        mem_gb = fingerprint.mem_gb
        host_gb = detect_host_mem_gb()
        safe_gb = safe_vm_mem_gb(mem_gb, host_gb) if host_gb is not None else mem_gb
        if safe_gb < mem_gb:
            print(
                f"Error: profile '{profile.name}' needs {mem_gb}G guest RAM, but only "
                f"{safe_gb}G can be safely backed on this {host_gb}G host after reserving "
                f"headroom for the host OS, TDX PAMT, page tables, and VFIO pinning. "
                f"(TDX guest memory is pinned and unreclaimable, so an over-large guest "
                f"OOM-kills the host instead of paging.) Guest RAM is fixed for measurement "
                f"determinism and is never resized, so this host cannot run '{profile.name}'.",
                file=sys.stderr,
            )
            return 1
        mem = fingerprint.mem
        vcpus = str(fingerprint.cpu.vcpus)
        smp_topology = fingerprint.cpu.smp_topology
        print(
            f"  GPU passthrough: {total_gpus}x {profile.name}"
            f" ({profile.vram_gb}GB VRAM each)"
            f" → {vcpus} vCPUs, {mem} RAM"
        )

    profile_wants_numa = profile is not None and profile.enable_numa_topology
    numa_active = use_numa_topology(profile_wants_numa)

    print(f"Launching TDX VM: {vcpus} vCPUs, {mem} RAM")
    print(f"Image: {args.image}")

    cpu_args = GUEST_CPU_ARGS

    pci_pinning = PcieRootPinning(numa_active)

    firmware_filename = profile.firmware_filename if profile else _DEFAULT_FIRMWARE
    firmware = _firmware_path(firmware_filename)
    print(f"Firmware: {firmware}")

    # Direct boot (1.4.0+): OVMF boots the image's kernel/initrd directly, dropping
    # GRUB/shim from the measured chain. These are published with the image (built
    # once, downloaded from R2) and staged next to it — the same bytes
    # `measurements generate` measures, so the boot matches the pinned RTMR1/2.
    kernel_path, initrd_path, cmdline = direct_boot_artifacts(args.image)
    print(f"Direct boot: kernel={kernel_path} cmdline={cmdline!r}")

    qemu_cmds = build_base_cmd(
        mem=mem,
        smp_topology=smp_topology,
        process_name=PROCESS_NAME,
        cpu_args=cpu_args,
        firmware=firmware,
        img_path=args.image,
        foreground=args.foreground,
        pidfile=PIDFILE,
        logfile=LOGFILE,
        host_nodes=host_numa_nodes() if numa_active else [],
        pci_pinning=pci_pinning,
        kernel_path=kernel_path,
        initrd_path=initrd_path,
        cmdline=cmdline,
    )

    build_network(
        qemu_cmds,
        network_type=args.network_type,
        net_iface=args.net_iface,
        ssh_port=args.ssh_port,
        net_queues=args.net_queues,
        pci_pinning=pci_pinning,
    )

    add_volumes(
        qemu_cmds,
        config_volume=args.config_volume,
        cache_volume=args.cache_volume,
        storage_volume=args.storage_volume,
        pci_pinning=pci_pinning,
    )

    add_vsock(qemu_cmds, pci_pinning=pci_pinning)

    if args.pass_gpus:
        setup_passthrough(qemu_cmds)

    # Guest NUMA topology (numa_active) binds memory per node via QEMU
    # memory-backends, so no numactl prefix is needed. Otherwise interleave
    # across the GPUs' host NUMA nodes (all nodes if detection finds none).
    if numa_active:
        launch_prefix = []
    else:
        numa_nodes = detect_gpu_numa_nodes(gpus) if args.pass_gpus and gpus else []
        if numa_nodes:
            interleave = ",".join(str(n) for n in numa_nodes)
            print(f"  NUMA: interleaving memory across GPU nodes {interleave}")
        else:
            interleave = "all"
        launch_prefix = ["numactl", f"--interleave={interleave}"]

    print("Launching QEMU...")
    result = proc.run(
        launch_prefix + qemu_cmds.to_args(),
        stderr=proc.STDOUT,
    )
    if result.returncode != 0:
        print(f"Error: QEMU failed (exit {result.returncode}).", file=sys.stderr)
        return result.returncode

    if not args.foreground:
        # vCPU thread pinning is gated on the profile enabling NUMA topology
        # (requires dual-socket host with PXB-PCIe grouping active). Host-wide
        # CPU power tuning is separate and operator-driven; see
        # `python -m chutes_cvm.host.tune` (chutes-cvm host tune / restore-host).
        pin_threads = (
            numa_active and profile is not None and profile.enable_post_launch_tuning
        )
        if pin_threads:
            apply_post_launch_tuning(
                pidfile=PIDFILE,
                vcpus_total=int(vcpus),
                host_nodes=host_numa_nodes(),
                pin_threads=pin_threads,
            )

    if not args.foreground:
        print(f"Log file: {LOGFILE}")
    print_vm_status(args.ssh_port, show_ssh=args.ssh)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chutes_cvm.guest",
        description="Low-level TDX VM boot primitive (driven by `chutes-cvm guest launch`).",
    )

    parser.add_argument("--image", type=str, help="Path to VM image")
    parser.add_argument("--pass-gpus", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--ssh",
        action="store_true",
        help="Show SSH login hint after launch (benchmark and debug modes)",
    )

    parser.add_argument("--config-volume", type=str)
    parser.add_argument("--cache-volume", type=str)
    parser.add_argument(
        "--storage-volume",
        type=str,
        help="Storage volume for VM storage (containerd and kubelet-pods)",
    )
    parser.add_argument("--ssh-port", type=int, default=10022)

    parser.add_argument("--network-type", choices=["tap", "user"], default="user")
    parser.add_argument("--net-iface", type=str)
    parser.add_argument(
        "--net-queues",
        type=int,
        default=4,
        help="Virtio-net multiqueue count for TAP mode (default: 4)",
    )

    args = parser.parse_args(argv)

    try:
        stop_existing_vm()
    except Exception:  # nosec B110
        pass

    if args.clean:
        return 0

    if not args.image:
        print("Error: --image is required")
        return 1

    return launch_vm(args)


if __name__ == "__main__":
    sys.exit(main())
