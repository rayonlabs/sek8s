"""TDX host setup orchestration.

Consumes a HostProfile and executes the setup steps: PPAs, packages,
kernel selection, GRUB configuration, and kvm group membership.
All OS-version differences are encoded in the profile — this module
contains no version-specific branching.
"""

import argparse
import glob
import os
import re
import sys

from chutes_cvm import proc
from chutes_cvm.host.profiles import PPA, APTRepo, HostProfile, resolve_profile

# Fabric Manager version must match the NVIDIA driver version in the guest
# image.  FM communicates with GPU firmware shared between host and guest;
# version mismatches cause NVLink initialization failures and Xid errors.
# Keep in sync with nvidia_version / nvidia_pkg_version in
# ansible/guest/playbooks/group_vars/all.yml.
FM_DRIVER_BRANCH = "595"
FM_PKG_VERSION = "595.71.05-0ubuntu0.26.04.1"


def _run(cmd: list[str], **kwargs):
    """Run a command, printing it first. Raises on failure."""
    print(f"  $ {' '.join(cmd)}")
    proc.run(cmd, check=True, **kwargs)


def _add_repo(repo: APTRepo):
    """Add a generic APT repository with DEB822 sources and pinning.

    Downloads the signing key from repo.signing_key_url, writes a
    sources entry to /etc/apt/sources.list.d/, and creates a pin file.
    """
    print(f"  Adding repo: {repo.name} ({repo.uri})")
    keyring_path = f"/etc/apt/keyrings/{repo.name}.asc"
    sources_file = f"/etc/apt/sources.list.d/{repo.name}.sources"

    _run(["sudo", "mkdir", "-p", "/etc/apt/keyrings"])
    _run(["sudo", "curl", "-fsSL", "-o", keyring_path, repo.signing_key_url])

    sources_content = (
        f"Types: deb\n"
        f"URIs: {repo.uri}\n"
        f"Suites: {repo.suite}\n"
        f"Components: {repo.components}\n"
        f"Signed-By: {keyring_path}\n"
    )
    _write_system_file(sources_file, sources_content)

    # Pin the repo so its packages take priority over Ubuntu archive
    pin_file = f"/etc/apt/preferences.d/{repo.name}-pin-{repo.pin_priority}"
    pin_content = (
        f"Package: *\n"
        f"Pin: origin download.01.org\n"
        f"Pin-Priority: {repo.pin_priority}\n"
    )
    _write_system_file(pin_file, pin_content)


def _add_ppa(ppa: PPA, codename: str):
    """Add an APT PPA and pin its packages at the configured priority.

    When ppa.suite is set, the sources entry is written manually instead of
    using add-apt-repository (which would auto-detect the wrong suite).
    """
    suite = ppa.suite or codename

    if ppa.suite:
        print(f"  Adding PPA: {ppa.uri} (pinned to suite {suite})")
        _add_ppa_manual(ppa, suite)
    else:
        print(f"  Adding PPA: {ppa.uri}")
        _run(["sudo", "add-apt-repository", "-y", ppa.uri])

    distro_id = f"LP-PPA-{ppa.team}-{ppa.name}"
    pin_file = (
        f"/etc/apt/preferences.d/kobuk-tdx-{ppa.team}-{ppa.name}-pin-{ppa.pin_priority}"
    )
    pin_content = (
        f"Package: *\n"
        f"Pin: release o={distro_id}\n"
        f"Pin-Priority: {ppa.pin_priority}\n"
    )
    _write_system_file(pin_file, pin_content)

    unattended_file = f"/etc/apt/apt.conf.d/99unattended-upgrades-kobuk-{ppa.name}"
    unattended_content = (
        f"Unattended-Upgrade::Allowed-Origins {{\n"
        f'  "{distro_id}:{suite}";\n'
        f"}};\n"
        f'Unattended-Upgrade::Allow-downgrade "true";\n'
    )
    _write_system_file(unattended_file, unattended_content)


def _add_ppa_manual(ppa: PPA, suite: str):
    """Write a DEB822 sources entry for a PPA with a specific suite.

    Used when add-apt-repository would pick the wrong suite (e.g. the PPA
    hasn't published packages for the running release).  Removes any stale
    sources entries for this PPA first (e.g. from a prior add-apt-repository
    that auto-detected the wrong suite).
    """
    sources_file = f"/etc/apt/sources.list.d/kobuk-{ppa.team}-{ppa.name}.sources"
    keyring_path = f"/etc/apt/keyrings/kobuk-{ppa.team}-{ppa.name}.asc"

    _remove_stale_ppa_sources(ppa)

    _run(["sudo", "mkdir", "-p", "/etc/apt/keyrings"])
    _fetch_signing_key(ppa.signing_key, keyring_path)

    sources_content = (
        f"Types: deb\n"
        f"URIs: https://ppa.launchpadcontent.net/{ppa.team}/{ppa.name}/ubuntu/\n"
        f"Suites: {suite}\n"
        f"Components: main\n"
        f"Signed-By: {keyring_path}\n"
    )
    _write_system_file(sources_file, sources_content)


def _fetch_signing_key(fingerprint: str, dest: str):
    """Download a GPG signing key from keyserver.ubuntu.com.

    Saves the ASCII-armored key directly -- modern apt (2.4+, i.e. Ubuntu
    24.04+) accepts armored keys in Signed-By without dearmoring.
    """
    url = f"https://keyserver.ubuntu.com/pks/lookup" f"?op=get&search=0x{fingerprint}"
    print(f"  Fetching signing key {fingerprint[:16]}...")
    proc.run(
        ["sudo", "curl", "-fsSL", "-o", dest, url],
        check=True,
    )


def _remove_stale_ppa_sources(ppa: PPA):
    """Remove any existing apt sources entries for this PPA.

    Cleans up entries left by add-apt-repository or prior manual installs
    so that our suite-pinned entry is the only one.
    """
    patterns = [
        f"/etc/apt/sources.list.d/*{ppa.team}*{ppa.name}*",
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            print(f"  Removing stale PPA source: {path}")
            _run(["sudo", "rm", "-f", path])


def _write_system_file(path: str, content: str):
    """Write content to a root-owned system file via tee."""
    _run(
        ["sudo", "tee", path],
        input=content.encode(),
        stdout=proc.DEVNULL,
    )


def _get_kernel_version(kernel_package: str) -> str:
    """Extract the kernel version string from a pinned package name.

    Expects a concrete package like ``linux-image-6.17.0-35-generic``.
    Raises if the name doesn't match the expected pattern.
    """
    match = re.match(r"linux-image-(\d+\.\d+\.\d+-\d+-\S+)", kernel_package)
    if not match:
        raise ValueError(
            f"kernel_package must be a pinned version "
            f"(e.g. 'linux-image-6.17.0-35-generic'), got '{kernel_package}'"
        )
    return match.group(1)


def _grub_set_kernel(kernel_version: str):
    """Set the given kernel as the default boot entry via grub-editenv.

    Uses awk + cut on /boot/grub/grub.cfg to locate the kernel entry and
    saves it via grub-editenv. Newer Ubuntu sometimes omits the Advanced options
    submenu (flat kernel list); then MID is empty and saved_entry is just KID.
    """
    print(f"  Setting default kernel: {kernel_version}")
    grub_cfg = "/boot/grub/grub.cfg"

    # MID: awk '/Advanced options for Ubuntu/{print $(NF-1)}' | cut -d\' -f2
    mid_raw = proc.run(
        ["awk", "/Advanced options for Ubuntu/{print $(NF-1)}", grub_cfg],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    first_mid_line = mid_raw.split("\n", 1)[0] if mid_raw else ""
    if first_mid_line:
        mid = proc.run(
            ["cut", "-d'", "-f2"],
            input=first_mid_line,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    else:
        mid = ""

    # KID: awk "/with Linux $KERNELVER/"'{print $(NF-1)}' | cut -d\' -f2 | head -n1
    kid_raw = proc.run(
        ["awk", f"/with Linux {kernel_version}/{{print $(NF-1)}}", grub_cfg],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    first_kid_line = kid_raw.split("\n", 1)[0] if kid_raw else ""
    if not first_kid_line:
        raise RuntimeError(
            f"Could not find kernel {kernel_version} in grub.cfg menu entries"
        )
    kid = proc.run(
        ["cut", "-d'", "-f2"],
        input=first_kid_line,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not kid:
        raise RuntimeError(f"Could not parse grub menu id for kernel {kernel_version}")

    saved_entry = f"{mid}>{kid}" if mid else kid

    grub_default_cfg = "/etc/default/grub.d/99-tdx-kernel.cfg"
    _write_system_file(
        grub_default_cfg,
        "GRUB_DEFAULT=saved\nGRUB_SAVEDEFAULT=true\n",
    )
    _run(
        [
            "sudo",
            "grub-editenv",
            "/boot/grub/grubenv",
            "set",
            f"saved_entry={saved_entry}",
        ],
    )
    _run(["sudo", "update-grub"])


def _grub_update_cmdline(additions: list[str]):
    """Append parameters to GRUB_CMDLINE_LINUX if not already present."""
    grub_file = "/etc/default/grub"
    with open(grub_file, "r") as f:
        content = f.read()

    modified = False
    for param in additions:
        if param in content:
            print(f"  GRUB cmdline already contains '{param}', skipping")
            continue
        print(f"  Adding '{param}' to GRUB cmdline")
        content = re.sub(
            r'(GRUB_CMDLINE_LINUX="[^"]*)',
            rf"\1 {param}",
            content,
        )
        modified = True

    if modified:
        _write_system_file(grub_file, content)
        _run(["sudo", "update-grub"])
        # --no-nvram prevents grub-install from updating EFI boot order
        # (important for MAAS-managed machines that expect PXE boot)
        _run(["sudo", "grub-install", "--no-nvram"])


_BLACKWELL_HGX_GPU_IDS = ("10de:2901", "10de:3182")  # B200, B300


def _detect_blackwell_hgx_gpus() -> bool:
    """Return True if any B200/B300 GPUs are present on this host."""
    try:
        output = proc.run(
            ["lspci", "-Dnn"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return any(
            gpu_id in line
            for line in output.stdout.splitlines()
            for gpu_id in _BLACKWELL_HGX_GPU_IDS
        )
    except (proc.TimeoutExpired, OSError):
        return False


def _setup_host_fabric_manager():
    """Install and configure Fabric Manager for B200/B300 HGX hosts.

    Blackwell NVSwitches are not visible as PCIe devices — they are managed by
    Fabric Manager through ConnectX-7 bridge PFs on the host.  This function
    is a no-op on non-Blackwell HGX hosts (GPU detection is cheap).

    Packages installed:
    - nvidia-fabricmanager: manages the NVSwitch fabric
    - nvlsm: NVLink Subnet Manager (required for B200/B300)
    - libibumad3 + infiniband-diags: InfiniBand management libraries
    - DOCA OFED / ib_umad: kernel module for CX7 bridge communication

    Configuration applied:
    - /etc/modules-load.d/ib_umad.conf: autoload ib_umad at boot
    - fabricmanager.cfg: PARTITION_RAIL_POLICY=symmetric (required for CC mode)
    - nvidia-fabricmanager.service: enabled and started immediately
    """
    if not _detect_blackwell_hgx_gpus():
        print("  No B200/B300 GPUs detected — skipping host Fabric Manager setup")
        return

    print("  B200/B300 GPUs detected — installing host Fabric Manager stack...")

    # ib_umad must be loaded before fabricmanager starts so it can open
    # the InfiniBand management datagram interface to the CX7 bridge PFs.
    ib_umad_conf = "/etc/modules-load.d/ib_umad.conf"
    ib_umad_content = (
        "# Required for B200/B300 Fabric Manager CX7 bridge communication\nib_umad\n"
    )
    already_current = False
    if os.path.exists(ib_umad_conf):
        with open(ib_umad_conf) as f:
            if f.read() == ib_umad_content:
                print(f"  {ib_umad_conf} already up to date")
                already_current = True
    if not already_current:
        _write_system_file(ib_umad_conf, ib_umad_content)
        print(f"  ✓ {ib_umad_conf} written")

    # Load ib_umad now (idempotent — modprobe is a no-op if already loaded).
    proc.run(["sudo", "modprobe", "ib_umad"], check=False)

    # Install host-side FM stack.  The CUDA apt repo is expected to be
    # configured already (same repo used by nvidia-gpu-tools setup).
    #
    # Use the version-pinned package (nvidia-fabricmanager-NNN) to avoid
    # conflicts with other FM versions and ensure FM matches the guest driver.
    # The unversioned nvidia-fabricmanager metapackage pulls the latest version
    # which can conflict with an already-installed pinned version and may not
    # match the guest driver.
    fm_pkg = f"nvidia-fabricmanager-{FM_DRIVER_BRANCH}"
    fm_pkg_pinned = f"{fm_pkg}={FM_PKG_VERSION}"

    # Abort if a mismatched FM version is installed — apt will fail with a
    # Conflicts error otherwise.  We don't auto-remove to keep setup safe
    # for clean hosts; the operator must remove the old package manually.
    try:
        dpkg_out = proc.run(
            ["dpkg-query", "-W", "-f", "${db:Status-Abbrev} ${Package}\n"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        stale_fm = [
            line.split()[1]
            for line in dpkg_out.stdout.splitlines()
            if line.startswith("ii")
            and line.split()[1].startswith("nvidia-fabricmanager")
            and line.split()[1] != fm_pkg
        ]
        if stale_fm:
            raise RuntimeError(
                f"Mismatched Fabric Manager package(s) installed: {', '.join(stale_fm)}\n"
                f"Required: {fm_pkg_pinned}\n"
                f"Please remove the old package(s) first:\n"
                f"  sudo apt remove -y {' '.join(stale_fm)}\n"
                f"Then re-run setup."
            )
    except (proc.TimeoutExpired, OSError, IndexError):
        pass

    # Pin to the exact version — matches how the Ansible guest role pins
    # nvidia packages via /etc/apt/preferences.d/nvidia-version-pin.
    _run(
        [
            "apt",
            "install",
            "--yes",
            "--allow-downgrades",
            fm_pkg_pinned,
            "nvlsm",
            "libibumad3",
            "infiniband-diags",
        ]
    )
    print(f"  ✓ Fabric Manager {FM_PKG_VERSION} installed")

    # Patch fabricmanager.cfg: PARTITION_RAIL_POLICY must be symmetric for
    # Blackwell MPT CC mode (greedy is the default, which disables CC).
    # FM 595+ requires the string "symmetric" (older versions used numeric "1").
    fm_cfg = "/usr/share/nvidia/nvswitch/fabricmanager.cfg"
    if os.path.exists(fm_cfg):
        with open(fm_cfg) as f:
            original = f.read()
        updated = re.sub(
            r"^PARTITION_RAIL_POLICY\s*=.*$",
            "PARTITION_RAIL_POLICY=symmetric",
            original,
            flags=re.MULTILINE,
        )
        if "PARTITION_RAIL_POLICY" not in original:
            updated = original.rstrip() + "\nPARTITION_RAIL_POLICY=symmetric\n"
        if updated != original:
            _write_system_file(fm_cfg, updated)
            print(f"  ✓ {fm_cfg}: PARTITION_RAIL_POLICY set to symmetric")
        else:
            print(f"  {fm_cfg}: PARTITION_RAIL_POLICY already configured")
    else:
        print(
            f"  Warning: {fm_cfg} not found after install — FM may not be configured correctly"
        )

    # Check if already running before enable/start to avoid unnecessary restarts.
    already_running = (
        proc.run(
            ["systemctl", "is-active", "--quiet", "nvidia-fabricmanager"],
            check=False,
        ).returncode
        == 0
    )

    _run(["sudo", "systemctl", "enable", "nvidia-fabricmanager"])
    if already_running:
        print(
            "  nvidia-fabricmanager.service already running — restarting to pick up config changes"
        )
        _run(["sudo", "systemctl", "restart", "nvidia-fabricmanager"])
    else:
        _run(["sudo", "systemctl", "start", "nvidia-fabricmanager"])
    print("  ✓ nvidia-fabricmanager.service enabled and running")


def _blacklist_gpu_drivers():
    """Blacklist nouveau and nvidia kernel modules on the host.

    GPUs on passthrough hosts must not be claimed by any driver — they are
    bound to vfio-pci at VM launch time.  An auto-loaded GPU driver is the most
    common cause of GPU hangs / vfio bind races during CC/PPCIe mode config.
    This must include `nova_core`, the in-tree Rust NVIDIA driver present on
    recent kernels (Ubuntu 25.10/26.04): it also matches the GPU's PCI ID and
    will grab a card after the CC-mode reset re-enumerates it, leaving the
    device on nova_core instead of vfio-pci.
    """
    blacklist_path = "/etc/modprobe.d/blacklist-gpu-host.conf"
    blacklist_content = (
        "# GPU drivers must not load on VFIO passthrough hosts.\n"
        "# GPUs are configured via nvidia-gpu-tools and bound to vfio-pci at launch.\n"
        "blacklist nouveau\n"
        "blacklist nova_core\n"
        "blacklist nvidiafb\n"
        "blacklist nvidia\n"
        "blacklist nvidia_drm\n"
        "blacklist nvidia_modeset\n"
        "blacklist nvidia_uvm\n"
        "options nouveau modeset=0\n"
    )
    already_current = False
    if os.path.exists(blacklist_path):
        with open(blacklist_path, "r") as f:
            if f.read() == blacklist_content:
                print(f"  {blacklist_path} already up to date")
                already_current = True

    if not already_current:
        _write_system_file(blacklist_path, blacklist_content)
        _run(["sudo", "update-initramfs", "-u"])
        print(f"  ✓ GPU driver blacklist installed ({blacklist_path})")

    # Unload any auto-loaded GPU driver immediately (no reboot required) so it
    # releases the GPUs before launch. Only modules with no host-side dependents
    # are unloaded here; the nvidia stack is left alone (fabric manager may have
    # loaded it explicitly). rmmod is best-effort — if the module is still bound
    # to devices the per-device unbind at launch handles it.
    result = proc.run(
        ["lsmod"],
        capture_output=True,
        text=True,
    )
    loaded = {line.split()[0] for line in result.stdout.splitlines()[1:]}
    for mod in ("nouveau", "nova_core", "nvidiafb"):
        if mod in loaded:
            print(f"  Unloading {mod} module (currently loaded)...")
            proc.run(["sudo", "rmmod", mod], check=False)
            print(f"  ✓ {mod} unloaded")


def _configure_qcnl(conf_path: str = "/etc/sgx_default_qcnl.conf"):
    """Ensure QCNL accepts PCCS's self-signed TLS certificate.

    PCCS runs locally with a self-signed cert.  The default QCNL config ships
    with use_secure_cert=true which causes CURL error 60 on every quote
    request.  We patch it to false so QGS can reach the local PCCS.

    The file is a JSON5-ish format (allows comments and trailing commas) so
    we use a regex patch rather than json.loads to avoid stripping comments.
    """
    if not os.path.exists(conf_path):
        print(f"  {conf_path} not found — QCNL not installed yet, skipping")
        return

    with open(conf_path) as f:
        original = f.read()

    updated = re.sub(
        r'"use_secure_cert"\s*:\s*true',
        '"use_secure_cert": false',
        original,
    )

    if updated == original:
        print(f"  {conf_path} already has use_secure_cert=false")
        return

    print(f"  Patching {conf_path}: use_secure_cert → false")
    _write_system_file(conf_path, updated)


def _configure_qgs_vsock(conf_path: str = "/etc/qgs.conf"):
    """Ensure QGS uses vsock (port 4050) rather than a Unix domain socket.

    The default shipped config has the port line commented out, which causes
    QGS to bind a Unix socket that the VM cannot reach.  We uncomment/set
    'port = 4050' so QGS listens on vsock and restarts the service if the
    file was changed.
    """
    if not os.path.exists(conf_path):
        print(f"  {conf_path} not found — QGS not installed yet, skipping")
        return

    with open(conf_path) as f:
        original = f.read()

    updated = re.sub(r"^#?\s*port\s*=.*$", "port = 4050", original, flags=re.MULTILINE)

    if updated == original:
        print(f"  {conf_path} already set to vsock port 4050")
        return

    print(f"  Configuring {conf_path}: enabling vsock port 4050")
    _write_system_file(conf_path, updated)
    _run(["sudo", "systemctl", "restart", "qgsd"])


def _add_user_to_kvm():
    """Add the invoking (non-root) user to the kvm group."""
    user = os.environ.get("SUDO_USER") or os.environ.get("USER")
    if not user or user == "root":
        print("  Skipping kvm group (running as root with no SUDO_USER)")
        return
    print(f"  Adding {user} to kvm group")
    _run(["sudo", "usermod", "-aG", "kvm", user])


def _ensure_pccs_node_modules(pccs_dir: str = "/opt/intel/sgx-dcap-pccs"):
    """Run npm install in the PCCS directory when node_modules are absent.

    sgx-dcap-pccs Debian post-install only calls npm install during interactive
    debconf prompts.  With DEBIAN_FRONTEND=noninteractive the step is skipped,
    leaving node_modules/ empty and the service unable to start.
    """
    node_modules = os.path.join(pccs_dir, "node_modules")
    if os.path.isdir(node_modules):
        print(f"  {pccs_dir}/node_modules already present, skipping npm install")
        return

    package_json = os.path.join(pccs_dir, "package.json")
    if not os.path.exists(package_json):
        print(
            f"  {pccs_dir}/package.json not found — sgx-dcap-pccs not installed, skipping"
        )
        return

    print(f"  node_modules missing in {pccs_dir}, running npm install...")
    _run(["npm", "install", "--prefer-offline"], cwd=pccs_dir)
    print("  ✓ PCCS node_modules installed")


_CHRONY_CONF = """\
# NTP sources
pool ntp.ubuntu.com iburst
pool 0.ubuntu.pool.ntp.org iburst
pool 1.ubuntu.pool.ntp.org iburst

keyfile /etc/chrony/chrony.keys
driftfile /var/lib/chrony/chrony.drift
logdir /var/log/chrony

# Step the clock (rather than slew) if the offset exceeds 1 second during any clock
# update, so a BMC RTC set minutes ahead is corrected immediately on first boot.
makestep 1 -1

# Keep the hardware clock in sync with the system clock.
rtcsync
"""


def _setup_ntp():
    """Configure chrony to STEP the clock immediately, replacing systemd-timesyncd.

    timesyncd only slews small offsets; a BMC RTC set minutes ahead would take a long time to
    correct, and a VM inherits the host clock at launch (QEMU RTC) — a skewed clock yields
    boot-time mTLS certs with a wrong notBefore. chrony's ``makestep`` steps immediately so the
    host clock is right before any launch. chrony itself installs via ``profile.base_packages``.
    (Folded in from the ansible ``ntp`` role.)
    """
    print("\nStep: Configuring chrony (NTP, immediate clock step)...")
    # timesyncd only slews; mask it so chrony owns the clock. Tolerant — it may be absent/masked.
    for action in ("stop", "disable", "mask"):
        proc.run(["systemctl", action, "systemd-timesyncd"], check=False)
    _write_system_file("/etc/chrony/chrony.conf", _CHRONY_CONF)
    _run(["systemctl", "enable", "--now", "chrony"])
    # Force an immediate step, then best-effort wait for the first sync (never fatal).
    proc.run(["chronyc", "makestep"], check=False)
    waited = proc.run(["chronyc", "waitsync", "60", "1", "0", "1"], check=False)
    if waited.returncode != 0:
        print(
            "  ⚠ chrony did not confirm sync within 60s — continuing "
            "(verify later with `chronyc tracking`)."
        )


def _ensure_chutes_dirs():
    """Create the /var/lib/chutes directories chutes-cvm operations expect (base image sets and
    per-VM images). Folded in from the ansible ``chutes_dirs`` role. The per-VM dir matches
    LaunchConfig.vm.vm_image_directory's default (guest/launch.py)."""
    print("\nStep: Ensuring /var/lib/chutes directories...")
    for d in ("/var/lib/chutes/base-images", "/var/lib/chutes/vm-images"):
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o755)  # nosec B103
        print(f"  {d}")


def setup_host(profile: HostProfile, noninteractive: bool = False):
    """Execute TDX host setup using the given profile.

    Must be run as root (or via sudo). This is the complete per-host configuration — a host is
    launch-ready after it (modulo the CLI install itself, PCCS secrets, and a reboot, which the
    ansible layer owns). Steps:
    1. Add PPAs with apt pinning
    2. apt update
    3. Install kernel + base_packages (chrony/aria2/xfsprogs) + packages
    3b. Configure chrony (NTP, immediate clock step)
    4. Set kernel as default boot target
    5. Update GRUB cmdline
    6. Configure QGS for vsock (port 4050)
    7. Configure QCNL to accept local PCCS self-signed cert
    8. Add user to kvm group
    9. Ensure /var/lib/chutes directories

    When noninteractive=True (e.g. called by Ansible via --noninteractive),
    DEBIAN_FRONTEND=noninteractive is set so apt never blocks on prompts.
    An explicit npm install is then run for sgx-dcap-pccs since the package's
    debconf post-install hook is skipped in non-interactive mode.
    In interactive mode (default for direct human use) apt prompts normally,
    so sgx-dcap-pccs configures itself during install and npm runs as part of
    its own post-install flow.
    """
    print(f"\n{'=' * 60}")
    print(f"  TDX Host Setup: {profile.describe()}")
    print(f"{'=' * 60}\n")

    if os.geteuid() != 0:
        print("Error: this script must be run as root (sudo).", file=sys.stderr)
        sys.exit(1)

    if not re.match(r"linux-image-\d+\.\d+\.\d+-\d+-\S+", profile.kernel_package):
        print(
            f"Error: kernel_package must be a pinned version "
            f"(e.g. 'linux-image-6.17.0-35-generic'), "
            f"got '{profile.kernel_package}'.\n"
            f"Unpinned metapackages cause RTMR0 measurement drift.",
            file=sys.stderr,
        )
        sys.exit(1)

    if noninteractive:
        os.environ["DEBIAN_FRONTEND"] = "noninteractive"

    # 1. PPAs
    if profile.ppas:
        print("Step 1: Adding APT PPAs...")
        _run(["apt", "update"])
        _run(["apt", "install", "--yes", "software-properties-common", "gawk"])
        for ppa in profile.ppas:
            _add_ppa(ppa, profile.codename)
    else:
        print("Step 1: No PPAs needed for this profile")

    # 1b. Generic APT repos
    for repo in profile.repos:
        _add_repo(repo)

    # 2. apt update
    print("\nStep 2: Updating package index...")
    _run(["apt", "update"])

    # 3. Install kernel + packages (base_packages = the folded-in host deps: chrony/aria2/xfsprogs)
    print(f"\nStep 3: Installing kernel ({profile.kernel_package}) and packages...")
    all_packages = [profile.kernel_package] + profile.base_packages + profile.packages
    _run(["apt", "install", "--yes", "--allow-downgrades"] + all_packages)

    kernel_version = _get_kernel_version(profile.kernel_package)
    print(f"  Kernel version resolved: {kernel_version}")

    # linux-modules-extra is a separate package that may not be pulled in
    # automatically; not all kernel builds ship it (e.g. 25.10 generic).
    modules_extra = f"linux-modules-extra-{kernel_version}"
    print(f"  Ensuring {modules_extra} is installed...")
    result = proc.run(
        ["apt", "install", "--yes", "--allow-downgrades", modules_extra],
    )
    if result.returncode != 0:
        print(f"  {modules_extra} not available (may be built into the kernel package)")

    if noninteractive:
        # sgx-dcap-pccs post-install only runs npm install during interactive
        # debconf prompts; in non-interactive mode we must do it ourselves.
        _ensure_pccs_node_modules()

    # 3b. NTP: chrony (installed above) steps the clock immediately so no VM inherits a skew.
    _setup_ntp()

    # 4. Set kernel as default boot
    print(f"\nStep 4: Setting kernel {kernel_version} as default boot target...")
    _grub_set_kernel(kernel_version)

    # 5. GRUB cmdline
    print("\nStep 5: Updating GRUB cmdline...")
    _grub_update_cmdline(profile.grub_cmdline_additions)

    # 5b. Blacklist GPU drivers on host (GPUs are for VFIO passthrough only)
    print("\nStep 5b: Blacklisting nouveau/nvidia drivers on host...")
    _blacklist_gpu_drivers()

    # 5c. Host Fabric Manager for B200/B300 (no-op on other SKUs)
    print("\nStep 5c: Configuring host Fabric Manager (B200/B300)...")
    _setup_host_fabric_manager()

    # 6. QGS vsock mode
    print("\nStep 6: Configuring QGS for vsock (port 4050)...")
    _configure_qgs_vsock()

    # 7. QCNL self-signed cert
    print("\nStep 7: Configuring QCNL to accept local PCCS self-signed cert...")
    _configure_qcnl()

    # 8. kvm group
    print("\nStep 8: Configuring kvm group...")
    _add_user_to_kvm()

    # 9. Host directories chutes-cvm operations expect (base image sets / per-VM overlays).
    _ensure_chutes_dirs()

    print(f"\n{'=' * 60}")
    print("  TDX host setup complete. Reboot to load the new kernel.")
    print(f"{'=' * 60}\n")


def main(argv: "list[str] | None" = None) -> int:
    """CLI entry for host setup: `chutes-cvm host setup` (or `python -m chutes_cvm.host.setup`).

    Detects the Ubuntu version, resolves the matching host profile, and executes the
    setup steps (PPAs, kernel, packages, GRUB, kvm group). Was the setup-tdx-host script.
    """
    parser = argparse.ArgumentParser(
        prog="chutes-cvm host setup",
        description="Set up TDX host for confidential GPU computing",
    )
    parser.add_argument(
        "--noninteractive",
        action="store_true",
        help=(
            "Suppress apt/debconf prompts (sets DEBIAN_FRONTEND=noninteractive). "
            "Use when an external tool (e.g. Ansible) handles post-install "
            "configuration such as PCCS setup."
        ),
    )
    args = parser.parse_args(argv)

    try:
        profile = resolve_profile()
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    setup_host(profile, noninteractive=args.noninteractive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
