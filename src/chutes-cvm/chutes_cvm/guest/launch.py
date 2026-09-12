"""End-to-end TDX VM launch orchestrator — ``chutes-cvm guest launch``.

This is the decision layer (ported from the former quick-launch.sh): parse args + config with
precedence (CLI > YAML > defaults), validate, run the host gates (TDX active, NUMA), refuse a
duplicate chutes-td, then perform each privileged step — invoking the bundled bash helper that
owns it for the ones whose logic *is* a sequence of special-tool calls (volumes via
cryptsetup/nbd, config volume, bridge via ip/iptables), or doing it in-process where it is plain
file work (the per-VM image copy + sidecar staging, as `sudo cp`/`mkdir`/`rm`) — and finally boot
via the QEMU boot primitive (``chutes_cvm.guest.__main__``). Per AGENT.md's bash-vs-Python rule,
Python owns the decisions and bash still owns the tool-sequence system mutations.

The privileged helpers create volumes with relative default names (``cache-<host>.raw`` …) and
reference sibling ``volumes/`` / ``network/`` scripts, so — exactly as quick-launch did — the
orchestration runs with the bundled scripts dir as its working directory.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from chutes_cvm import proc
from chutes_cvm.guest import image_set
from chutes_cvm.guest.config import ConfigError, LaunchConfig
from chutes_cvm.paths import SCRIPTS_DIR, default_config_path

_PROCESS_NAME_CHUTES_TD = "chutes-td"


# ── Host gates ──────────────────────────────────────────────────────────────────


def _chutes_td_running() -> bool:
    """True if a live (non-zombie) chutes-td QEMU is already running.

    Kept aligned with ansible/host/roles/chutes_tee_vm/files/is_live_chutes_td.sh: match a
    qemu-system/qemu-kvm process whose cmdline carries the chutes-td process name.
    """
    try:
        pids = proc.run(
            ["pgrep", "-f", "qemu-system|qemu-kvm"],
            capture_output=True,
            text=True,
        ).stdout.split()
    except FileNotFoundError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if "qemu-system" not in cmdline and "qemu-kvm" not in cmdline:
            continue
        if _PROCESS_NAME_CHUTES_TD in cmdline:
            return True
    return False


def _tdx_active() -> "tuple[bool, str]":
    """Return (active, source). Check sysfs first (survives dmesg rollover), then /proc/cpuinfo,
    then dmesg as a last resort — matching the former quick-launch Step 0."""
    try:
        with open("/sys/module/kvm_intel/parameters/tdx") as f:
            if f.read().strip() == "Y":
                return True, "sysfs (/sys/module/kvm_intel/parameters/tdx=Y)"
    except OSError:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            if "tdx" in f.read():
                return True, "/proc/cpuinfo"
    except OSError:
        pass
    dmesg = proc.run(["sudo", "dmesg"], capture_output=True, text=True).stdout
    if any(
        "module initialized" in ln for ln in dmesg.splitlines() if "tdx" in ln.lower()
    ):
        return True, "dmesg"
    return False, ""


def _ensure_numa_zone_reclaim() -> None:
    """Ensure vm.zone_reclaim_mode=0 (cross-node allocation for QEMU/KVM); fix if not."""
    current = proc.run(
        ["sysctl", "-n", "vm.zone_reclaim_mode"], capture_output=True, text=True
    ).stdout.strip()
    if current != "0":
        print(f"⚠ vm.zone_reclaim_mode={current or 'unknown'} — setting to 0")
        proc.run(["sudo", "sysctl", "-w", "vm.zone_reclaim_mode=0"], check=False)
    print("✓ NUMA zone reclaim disabled (vm.zone_reclaim_mode=0)")


def _launchable(config_path: str, base_image: str, force: bool) -> bool:
    """Return True if launch may proceed: the control plane confirms an image of THIS host's
    ``(version, rc)`` will attest here. Mirrors `chutes-cvm host verify`'s API check — read the
    image's (version, rc) from its manifest, capture + sign the host profile, and ask
    POST /servers/tdx/preflight. Without a launchable verdict the VM would boot and then fail
    attestation, so refuse early (return False) unless ``force`` overrides with a warning.
    """
    # Deferred: preflight pulls substrateinterface (signing) — only needed for an actual launch,
    # not `guest launch --help` or the early config path.
    from chutes_cvm.guest.preflight import (
        DEFAULT_API_BASE,
        PreflightError,
        run_preflight,
    )

    try:
        version, rc = image_set.version_and_rc(base_image)
    except (FileNotFoundError, ValueError, OSError) as exc:
        # Can't read the manifest -> can't know what we're booting. Fail closed unless forced.
        if force:
            print(
                f"⚠ could not read image version from {base_image} ({exc}); proceeding anyway "
                "(--force) — attestation may fail.",
                file=sys.stderr,
            )
            return True
        print(
            f"✗ could not read image version from {base_image}: {exc}\n"
            "  Refusing to launch. Re-run `chutes-cvm image download`, or pass --force.",
            file=sys.stderr,
        )
        return False

    label = f"{version}{' (rc)' if rc else ''}"
    api_base = os.environ.get("CHUTES_API_BASE") or DEFAULT_API_BASE
    try:
        resp = run_preflight(
            config_path=config_path,
            scripts_dir=str(SCRIPTS_DIR),
            version=version,
            rc=rc,
            api_base=api_base,
        )
        launchable = bool(resp.get("launchable"))
        fingerprint = resp.get("fingerprint", "?")
        detail = resp.get("detail", "")
    except PreflightError as exc:
        launchable, fingerprint, detail = False, "?", str(exc)

    if launchable:
        print(f"✓ {detail} (fingerprint {fingerprint})")
        return True

    problem = f"this host cannot attest {label} yet (fingerprint {fingerprint})." + (
        f" {detail}" if detail else ""
    )
    remedy = (
        "Register this host class with `chutes-cvm host submit-profile`, then retry once Chutes\n"
        "  publishes the measurement (`chutes-cvm host verify` shows readiness)."
    )
    if force:
        print(
            f"⚠ {problem}\n  Proceeding anyway (--force) — the VM will fail attestation if this "
            "image is truly unmeasured for this host.",
            file=sys.stderr,
        )
        return True
    print(
        f"✗ {problem}\n"
        "  Refusing to launch: the VM would boot but fail attestation.\n"
        f"  {remedy} Pass --force to launch anyway.",
        file=sys.stderr,
    )
    return False


def _resolve_public_iface(configured: str) -> str:
    """Return the public interface: the configured one if it exists, else the default-route dev.

    Warns (but does not fail) when a configured name is missing — a stale NIC name after an OS
    upgrade is caught here rather than producing broken iptables rules.
    """
    if configured and _iface_exists(configured):
        return configured
    detected = _default_route_iface()
    if not detected:
        raise LaunchError(
            "could not determine the public interface — auto-detection found no default "
            "route. Set network.public_interface in config.yaml or pass --public-iface."
        )
    if configured:
        print(
            f"⚠ configured public interface '{configured}' not found; auto-detected "
            f"'{detected}' from the default route (update network.public_interface to silence)."
        )
    return detected


def _iface_exists(name: str) -> bool:
    return proc.run(["ip", "link", "show", name], capture_output=True).returncode == 0


def _default_route_iface() -> str:
    """The interface of the default route (empty if none)."""
    out = proc.run(
        ["ip", "-j", "route", "show", "default"], capture_output=True, text=True
    ).stdout.strip()
    try:
        routes = json.loads(out) if out else []
    except json.JSONDecodeError:
        return ""
    return routes[0].get("dev", "") if routes else ""


class LaunchError(Exception):
    """A launch precondition failed (message is user-facing)."""


# ── Privileged steps (bash helpers own the actual system mutations) ──────────────


def _helper(*parts: str) -> str:
    return str(SCRIPTS_DIR.joinpath(*parts))


def _volume_path(vol: str) -> str:
    """Resolve a (possibly relative) volume path the way the bash helper will — relative names
    live in the scripts working directory (SCRIPTS_DIR), matching the former quick-launch cwd.
    """
    return vol if os.path.isabs(vol) else str(SCRIPTS_DIR / vol)


def _ensure_raw_volume(vol: str, size: str, label: str, kind: str) -> None:
    """Create a raw LUKS volume via volumes/create-cache.sh unless it already exists.

    ``kind`` is only for messages. qcow2 volumes are never created (only reused if present).
    """
    path = _volume_path(vol)
    if os.path.exists(path):
        print(f"✓ Using existing {kind} volume: {vol}")
        return
    if vol.endswith(".qcow2"):
        raise LaunchError(
            f"qcow2 volumes cannot be created — use .raw for a new {kind} volume "
            f"(e.g. {kind}-<hostname>.raw). Existing qcow2 volumes are reused if present."
        )
    print(f"Creating {kind} volume at: {vol} ({size})")
    _run([_helper("volumes", "create-cache.sh"), vol, size, label])


def _setup_config_volume(config: LaunchConfig, benchmark: bool) -> None:
    """Create/refresh the config volume via volumes/create-config.sh.

    Benchmark passes hostname + network positionally with empty miner creds; production passes
    every value by NAME through the environment (create-config.sh reads those), so long/optional
    fields (docker creds, operator key) stay off the command line.
    """
    vol = config.volumes.config.path
    action = "Refreshing existing" if os.path.exists(_volume_path(vol)) else "Creating"
    print(f"{action} config volume: {vol}")
    gateway = config.network.bridge_ip.split("/")[0]
    helper = _helper("volumes", "create-config.sh")
    if benchmark:
        _run(
            [
                "sudo",
                helper,
                vol,
                config.vm.hostname,
                "",
                "",
                config.network.vm_ip,
                gateway,
                config.network.dns,
            ]
        )
    else:
        _run(
            [
                "sudo",
                f"HOSTNAME={config.vm.hostname}",
                f"MINER_SS58={config.miner.ss58}",
                f"MINER_SEED={config.miner.seed}",
                f"VM_IP={config.network.vm_ip}",
                f"VM_GATEWAY={gateway}",
                f"VM_DNS={config.network.dns}",
                f"DOCKER_HUB_USER={config.docker_hub.username}",
                f"DOCKER_HUB_TOKEN={config.docker_hub.token}",
                helper,
                vol,
            ]
        )


_DIRECT_BOOT_SIDECARS = ("vmlinuz", "initrd", "cmdline")


def _prepare_vm_image(base_image: str, hostname: str, vm_image_dir: str) -> str:
    """Verify the image set and instantiate the per-VM copy; return the per-VM image path.

    The per-VM image is a full copy of the base qcow2 (not an overlay): luksRemoveKey later
    destroys the old key slot in-place on the only copy, matching the storage/cache volumes.

    Python owns the decisions/data — verify the set against its manifest and resolve the qcow2 +
    its manifest sha256 (image_set.resolve), derive the per-VM name, and pick which stale copies
    to reap. The file mutations are privileged (the image dir is root-owned under /var/lib/chutes),
    so each runs via sudo, matching the per-step-sudo pattern the rest of launch uses.
    """
    try:
        qcow2, sha256 = image_set.resolve(base_image, full=False)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        raise LaunchError(f"image set verification failed: {exc}") from exc
    print(
        f"Verified image set via manifest: {qcow2} (sha256={sha256})", file=sys.stderr
    )

    if not os.path.isdir(vm_image_dir):
        _run(["sudo", "mkdir", "-p", vm_image_dir])

    vm_image = os.path.join(vm_image_dir, f"tdx-{hostname}-{sha256[:16]}.qcow2")

    # Reap stale per-VM images (and their sidecars) from previous base versions for this host.
    for stale in sorted(
        glob.glob(os.path.join(vm_image_dir, f"tdx-{hostname}-*.qcow2"))
    ):
        if stale == vm_image:
            continue
        print(f"Removing stale VM image: {stale}", file=sys.stderr)
        stale_base = stale[: -len(".qcow2")]
        _run(
            ["sudo", "rm", "-f", stale]
            + [f"{stale_base}.{ext}" for ext in _DIRECT_BOOT_SIDECARS]
        )

    if os.path.exists(vm_image):
        print(f"Using existing VM image: {vm_image}", file=sys.stderr)
    else:
        print(f"Copying base image to per-VM image: {vm_image}", file=sys.stderr)
        _run(["sudo", "cp", qcow2, vm_image])

    # Direct-boot sidecars must travel with the per-VM copy the launcher boots (it resolves
    # <image-base>.{vmlinuz,initrd,cmdline} next to that copy). Re-sync unconditionally so a
    # reused per-VM image also refreshes. Missing base sidecars are fatal — no direct boot.
    base_no_ext, vm_no_ext = qcow2[: -len(".qcow2")], vm_image[: -len(".qcow2")]
    for ext in _DIRECT_BOOT_SIDECARS:
        src = f"{base_no_ext}.{ext}"
        if not os.path.isfile(src):
            raise LaunchError(
                f"direct-boot artifact missing next to base image: {src} — the image must ship "
                "with .vmlinuz/.initrd/.cmdline (stage-boot-artifacts, published with the qcow2)"
            )
        _run(["sudo", "cp", src, f"{vm_no_ext}.{ext}"])

    return vm_image


def _setup_bridge(config: LaunchConfig) -> str:
    """Set up TAP bridge networking via network/setup-bridge.sh; return the TAP interface name."""
    result = proc.run(
        [
            _helper("network", "setup-bridge.sh"),
            "--bridge-ip",
            config.network.bridge_ip,
            "--vm-ip",
            f"{config.network.vm_ip}/24",
            "--vm-dns",
            config.network.dns,
            "--public-iface",
            config.network.public_interface,
            "--multi-queue",
        ],
        cwd=str(SCRIPTS_DIR),
        capture_output=True,
        text=True,
    )
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise LaunchError("bridge setup failed")
    for line in result.stdout.splitlines():
        if line.startswith("Network interface:"):
            return line.split(":", 1)[1].strip()
    raise LaunchError("could not extract the TAP interface from setup-bridge output")


def _install_benchmark_netlog(config: LaunchConfig) -> None:
    """Install + (re)start the benchmark network-logging service from the bundled network/ files."""
    net = SCRIPTS_DIR / "network"
    srcs = {
        "benchmark-netlog.sh": ("/usr/local/bin/benchmark-netlog.sh", "0755"),
        "benchmark-netlog.service": (
            "/etc/systemd/system/benchmark-netlog.service",
            "0644",
        ),
        "benchmark-netlog.logrotate": (
            "/etc/logrotate.d/benchmark-netlog",
            "0644",
        ),
    }
    for name, (dst, mode) in srcs.items():
        src = net / name
        if not src.exists():
            raise LaunchError(f"benchmark netlog source missing: {src}")
        _run(["sudo", "install", "-m", mode, str(src), dst])

    env_file = "/etc/chutes/benchmark-netlog.env"
    if not os.path.exists(env_file):
        _run(["sudo", "mkdir", "-p", "/etc/chutes"])
        content = f"BRIDGE_SUBNET={config.network.bridge_ip}\nNETLOG_DIR=/var/log/chutes/benchmark-netlog\n"
        proc.run(
            ["sudo", "tee", env_file],
            input=content.encode(),
            stdout=proc.DEVNULL,
            check=True,
        )
    _run(["sudo", "systemctl", "daemon-reload"])
    proc.run(["sudo", "systemctl", "enable", "benchmark-netlog"], check=False)
    _run(["sudo", "systemctl", "restart", "benchmark-netlog"])
    print("✓ benchmark-netlog service installed and running")


def _run(cmd: "list[str]") -> None:
    """Run a privileged step from the scripts working directory; raise LaunchError on failure."""
    print(f"  $ {' '.join(cmd)}")
    if proc.run(cmd, cwd=str(SCRIPTS_DIR)).returncode != 0:
        raise LaunchError(f"command failed: {' '.join(cmd)}")


# ── Argument parsing + config precedence ─────────────────────────────────────────

# CLI value flag (argparse dest) → its (section, key) in the nested LaunchConfig. Deeper volume
# fields are handled separately below. store_true flags are handled separately too.
_CLI_TO_SECTION = {
    "hostname": ("vm", "hostname"),
    "base_image": ("vm", "base_image"),
    "vm_image_dir": ("vm", "vm_image_directory"),
    "miner_ss58": ("miner", "ss58"),
    "miner_seed": ("miner", "seed"),
    "vm_ip": ("network", "vm_ip"),
    "bridge_ip": ("network", "bridge_ip"),
    "vm_dns": ("network", "dns"),
    "public_iface": ("network", "public_interface"),
    "network_type": ("network", "type"),
    "ssh_port": ("network", "ssh_port"),
    "docker_hub_username": ("docker_hub", "username"),
    "docker_hub_token": ("docker_hub", "token"),
}

# CLI volume flags → (volumes subsection, key).
_CLI_TO_VOLUME = {
    "cache_size": ("cache", "size"),
    "cache_volume": ("cache", "path"),
    "storage_size": ("storage", "size"),
    "storage_volume": ("storage", "path"),
    "config_volume": ("config", "path"),
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chutes-cvm guest launch",
        description="End-to-end TEE VM launch: verify host, prepare volumes and network, boot.",
        epilog=(
            "Related commands (formerly flags of this orchestrator): `chutes-cvm config init` "
            "(scaffold config.yaml), `chutes-cvm image download` (fetch a base set), "
            "`chutes-cvm guest down` / `stop` (tear down)."
        ),
    )
    p.add_argument(
        "config_file", nargs="?", help="Launch config.yaml (CLI flags override it)"
    )
    p.add_argument("--config", dest="config_file", help="config.yaml path (explicit)")
    p.add_argument("--hostname")
    p.add_argument("--base-image", dest="base_image")
    p.add_argument("--vm-image-dir", dest="vm_image_dir")
    p.add_argument("--miner-ss58", dest="miner_ss58")
    p.add_argument("--miner-seed", dest="miner_seed")
    p.add_argument("--vm-ip", dest="vm_ip")
    p.add_argument("--bridge-ip", dest="bridge_ip")
    p.add_argument("--vm-dns", dest="vm_dns")
    p.add_argument("--public-iface", dest="public_iface")
    p.add_argument("--cache-size", dest="cache_size")
    p.add_argument("--cache-volume", dest="cache_volume")
    p.add_argument("--storage-size", dest="storage_size")
    p.add_argument("--storage-volume", dest="storage_volume")
    p.add_argument("--config-volume", dest="config_volume")
    p.add_argument("--ssh-port", dest="ssh_port", type=int)
    p.add_argument("--network-type", dest="network_type", choices=["tap", "user"])
    p.add_argument("--docker-hub-username", dest="docker_hub_username")
    p.add_argument("--docker-hub-token", dest="docker_hub_token")
    p.add_argument("--skip-bind", action="store_true", default=None)
    p.add_argument("--no-gpus", action="store_true", default=None)
    p.add_argument("--foreground", action="store_true", default=None)
    p.add_argument("--ephemeral", action="store_true", default=None)
    p.add_argument("--benchmark", action="store_true", default=None)
    p.add_argument("--force", action="store_true", default=None)
    return p


def _resolve_config(
    args: argparse.Namespace,
) -> "tuple[LaunchConfig, bool, bool, bool]":
    """Resolve config via the LaunchConfig model (CLI > env > YAML > defaults) and return
    (config, benchmark, pass_gpus, ephemeral). The last three are launch-runtime flags, not
    persisted config, so they stay out of the model."""
    # Docker Hub creds must be set together when given on the CLI.
    if bool(args.docker_hub_username) != bool(args.docker_hub_token):
        raise LaunchError(
            "use both --docker-hub-username and --docker-hub-token together (or neither)."
        )

    # Build nested CLI overrides (only flags the user set) — the highest-precedence source.
    overrides: dict = {}
    for dest, (section, key) in _CLI_TO_SECTION.items():
        val = getattr(args, dest)
        if val is not None:
            overrides.setdefault(section, {})[key] = val
    for dest, (sub, key) in _CLI_TO_VOLUME.items():
        val = getattr(args, dest)
        if val is not None:
            overrides.setdefault("volumes", {}).setdefault(sub, {})[key] = val
    if args.foreground:
        overrides.setdefault("runtime", {})["foreground"] = True
    if args.skip_bind:
        overrides.setdefault("devices", {})["bind_devices"] = False

    if args.config_file:
        print(f"Loading configuration from: {args.config_file}")
    try:
        model = LaunchConfig.from_file(args.config_file, **overrides)
    except ConfigError as exc:
        raise LaunchError(f"config: {exc}") from exc
    if args.config_file:
        print("✓ Configuration loaded")

    return model, bool(args.benchmark), not args.no_gpus, bool(args.ephemeral)


def _apply_derived_defaults(
    config: LaunchConfig, benchmark: bool, ephemeral: bool
) -> None:
    """Fill benchmark placeholders, the default base image, VM-image dir, and volume names."""
    if benchmark:
        config.vm.base_image = (
            config.vm.base_image or "/var/lib/chutes/base-images/tdx-guest-benchmark"
        )
        config.miner.ss58 = config.miner.ss58 or "benchmark"
        config.miner.seed = config.miner.seed or "benchmark"

    config.vm.base_image = (
        config.vm.base_image or "/var/lib/chutes/base-images/tdx-guest"
    )
    if ephemeral:
        config.vm.vm_image_directory = "/tmp/chutes-vm-images"  # nosec B108
    else:
        config.vm.vm_image_directory = (
            config.vm.vm_image_directory or "/var/lib/chutes/vm-images"
        )

    config.volumes.cache.path = (
        config.volumes.cache.path or f"cache-{config.vm.hostname}.raw"
    )
    config.volumes.storage.path = (
        config.volumes.storage.path or f"storage-{config.vm.hostname}.raw"
    )
    config.volumes.config.path = (
        config.volumes.config.path or f"config-{config.vm.hostname}.qcow2"
    )


def _validate(config: LaunchConfig, benchmark: bool) -> None:
    if config.network.type not in ("tap", "user"):
        raise LaunchError("network type must be 'tap' or 'user'")
    missing = []
    if not config.vm.hostname:
        missing.append("hostname (vm.hostname or --hostname)")
    if not benchmark:
        if not config.miner.ss58:
            missing.append("miner.ss58 (miner.ss58 or --miner-ss58)")
        if not config.miner.seed:
            missing.append("miner.seed (miner.seed or --miner-seed)")
    if missing:
        raise LaunchError(
            "missing required configuration:\n  - " + "\n  - ".join(missing)
        )


# ── Orchestration ────────────────────────────────────────────────────────────────


def main(argv: "list[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.config_file is None:
        default_cfg = default_config_path()
        if default_cfg and os.path.exists(default_cfg):
            args.config_file = default_cfg
    # Resolve the config path against the caller's cwd before we switch to the scripts dir.
    if args.config_file:
        args.config_file = os.path.abspath(args.config_file)

    try:
        config, benchmark, pass_gpus, ephemeral = _resolve_config(args)
        config.network.public_interface = _resolve_public_iface(
            config.network.public_interface
        )
        _apply_derived_defaults(config, benchmark, ephemeral)
        _validate(config, benchmark)
    except LaunchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("\n=== TEE VM Orchestration ===")
    print(f"Mode: {'benchmark' if benchmark else 'standard'}")
    print(f"Hostname: {config.vm.hostname}")
    print(f"Base image: {config.vm.base_image}")
    print(f"VM image dir: {config.vm.vm_image_directory}")
    print(f"Network: {config.network.type}\n")

    if not args.force and _chutes_td_running():
        print(
            f"Error: a TDX VM (QEMU, {_PROCESS_NAME_CHUTES_TD}) is already running.\n"
            "  Stop it first: chutes-cvm guest down  (or pass --force to override — not recommended).",
            file=sys.stderr,
        )
        return 1

    print("Step 0: Verifying host configuration...")
    active, source = _tdx_active()
    if not active:
        print(
            "✗ TDX does not appear active (checked sysfs, /proc/cpuinfo, dmesg). Enable TDX in "
            "BIOS + kernel and reboot; verify with `cat /sys/module/kvm_intel/parameters/tdx`.",
            file=sys.stderr,
        )
        return 1
    print(f"✓ TDX active (via {source})")
    _ensure_numa_zone_reclaim()

    # The gate is a launch-readiness check: does a published measurement for THIS image's
    # (version, rc) cover this host class? Benchmark VMs use dummy creds and aren't attested, so
    # they skip it; a debug (RC) image is not special-cased — its rc:true measurement must be
    # published just like a production image's, which the (version, rc) join checks directly.
    if benchmark:
        pass
    else:
        print("\nStep 1: Confirming this host can attest the image...")
        if not _launchable(
            args.config_file or default_config_path(),
            config.vm.base_image,
            args.force,
        ):
            return 1

    orig_cwd = os.getcwd()
    os.chdir(
        str(SCRIPTS_DIR)
    )  # helpers create relative volumes / call sibling scripts here
    try:
        if not benchmark:
            print("\nStep 2: Preparing cache volume...")
            _ensure_raw_volume(
                config.volumes.cache.path,
                config.volumes.cache.size,
                "tdx-cache",
                "cache",
            )

        print("\nStep 3: Preparing storage volume...")
        _ensure_raw_volume(
            config.volumes.storage.path,
            config.volumes.storage.size,
            "storage",
            "storage",
        )

        print("\nStep 4: Setting up config volume...")
        _setup_config_volume(config, benchmark)

        print("\nStep 4b: Preparing VM image (verify set + per-VM copy)...")
        vm_image = _prepare_vm_image(
            config.vm.base_image, config.vm.hostname, config.vm.vm_image_directory
        )

        net_iface = ""
        if config.network.type == "tap":
            print("\nStep 5: Setting up bridge networking...")
            net_iface = _setup_bridge(config)
            print(f"✓ Bridge configured (TAP: {net_iface})")
            if benchmark:
                print("\nStep 5b: Installing benchmark network logging...")
                _install_benchmark_netlog(config)

        rc = _boot(config, vm_image, net_iface, benchmark, pass_gpus)
    except LaunchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        os.chdir(orig_cwd)

    if rc != 0:
        print(
            "\nError: VM launch failed (the QEMU boot exited non-zero). See output above and "
            "/tmp/tdx-guest-td.log if daemonized.",
            file=sys.stderr,
        )
        return rc
    print("\n=== Chutes VM Deployed Successfully ===\n")
    return 0


def _boot(
    config: LaunchConfig,
    vm_image: str,
    net_iface: str,
    benchmark: bool,
    pass_gpus: bool,
) -> int:
    """Assemble the boot-primitive argument list and call the QEMU primitive in-process."""
    # Deferred import: the boot primitive pulls the heavy, host-specific chain
    # (detection/gpu/qemu/passthrough) that only an actual boot needs — importing it here keeps
    # `guest launch --help` and the early config/gate paths light.
    from chutes_cvm.guest.__main__ import main as launch_vm_main

    launch_args = ["--image", vm_image, "--network-type", config.network.type]
    if pass_gpus:
        launch_args.append("--pass-gpus")
    if config.network.type == "tap":
        launch_args += ["--net-iface", net_iface]
    if benchmark:
        # Benchmark: no cache volume (partner manages storage); config volume carries only
        # hostname + network; --ssh shows the login hint.
        launch_args += ["--ssh", "--config-volume", config.volumes.config.path]
        launch_args += ["--storage-volume", config.volumes.storage.path]
    else:
        launch_args += ["--config-volume", config.volumes.config.path]
        launch_args += ["--cache-volume", config.volumes.cache.path]
        launch_args += ["--storage-volume", config.volumes.storage.path]
    if config.runtime.foreground:
        launch_args.append("--foreground")

    print("\nLaunching Chutes VM...")
    return launch_vm_main(launch_args)


if __name__ == "__main__":
    sys.exit(main())
