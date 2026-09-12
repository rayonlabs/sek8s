"""Host profile registry: per-Ubuntu-version TDX host setup parameters.

Each supported Ubuntu version is a HostProfile subclass that declares PPAs,
third-party APT repos, kernel package, apt packages, and GRUB cmdline additions.
A single setup orchestrator consumes the profile — no OS-version branching in the
setup logic.  Adding a new Ubuntu version requires one subclass and one
HOST_PROFILES entry.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from chutes_cvm import proc


@dataclass
class PPA:
    """Launchpad PPA descriptor with pinning priority.

    When suite is set, the PPA sources entry uses that suite instead of the
    host codename.  This is needed when a PPA hasn't published packages for
    the running release.
    """

    team: str
    name: str
    signing_key: str
    pin_priority: int = 4000
    suite: str | None = None

    @property
    def uri(self) -> str:
        return f"ppa:{self.team}/{self.name}"


@dataclass
class APTRepo:
    """Generic APT repository (non-Launchpad).

    Used for vendor repositories such as Intel's download.01.org that are
    not Launchpad PPAs.  The signing key URL is downloaded and saved to
    /etc/apt/keyrings/ before writing a DEB822 sources entry.
    """

    name: str
    uri: str
    suite: str
    components: str
    signing_key_url: str
    pin_priority: int = 4000


class HostProfile(ABC):
    """Base class for Ubuntu-version-specific TDX host setup."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Ubuntu version string (e.g. '25.04')."""
        ...

    @property
    @abstractmethod
    def codename(self) -> str:
        """Ubuntu release codename (e.g. 'plucky')."""
        ...

    @property
    def ppas(self) -> list[PPA]:
        """PPAs to add before installing packages."""
        return []

    @property
    def repos(self) -> list[APTRepo]:
        """Third-party APT repos (non-PPA) to add before installing packages."""
        return []

    @property
    @abstractmethod
    def kernel_package(self) -> str:
        """Pinned kernel image package (e.g. 'linux-image-7.0.0-31-generic').

        Concrete version, not a metapackage, so the whole fleet runs the identical kernel.
        Fleet determinism only — the host kernel is not an RTMR0 input. Expires when the
        pocket drops the ABI; `apt-cache policy linux-image-generic` finds the current one.
        """
        ...

    @property
    @abstractmethod
    def packages(self) -> list[str]:
        """All apt packages to install (QEMU, attestation, etc.)."""
        ...

    @property
    def base_packages(self) -> list[str]:
        """Version-independent host operational deps, folded in from the ansible ntp /
        host_prerequisites roles so `setup-host` fully provisions a host: chrony (NTP —
        see _setup_ntp) plus the tools chutes-cvm operations shell out to (aria2 for image
        download, xfsprogs for volume mkfs). Install-time bootstrap deps (git, python3-venv/
        pip) are the installer's job (install.sh / the host_tools role), not setup-host's.
        """
        return ["chrony", "aria2", "python3-yaml", "xfsprogs"]

    @property
    def grub_cmdline_additions(self) -> list[str]:
        """Extra kernel parameters for GRUB_CMDLINE_LINUX_DEFAULT."""
        return ["nohibernate"]

    def describe(self) -> str:
        """Human-readable summary for logging."""
        return f"Ubuntu {self.name} ({self.codename})"


class Ubuntu2604Profile(HostProfile):
    """Ubuntu 26.04 (Resolute) — native TDX kernel and QEMU 10.2, attestation via Intel DCAP repo."""

    @property
    def name(self) -> str:
        return "26.04"

    @property
    def codename(self) -> str:
        return "resolute"

    @property
    def ppas(self) -> list[PPA]:
        return []

    @property
    def repos(self) -> list[APTRepo]:
        # Intel official SGX/DCAP attestation repository (no Launchpad equivalent).
        # Provides sgx-dcap-pccs, tdx-qgs, libsgx-dcap-default-qpl for TDX attestation.
        return [
            APTRepo(
                name="intel-sgx",
                uri="https://download.01.org/intel-sgx/sgx_repo/ubuntu/",
                suite="resolute",
                components="main",
                signing_key_url="https://download.01.org/intel-sgx/sgx_repo/ubuntu/intel-sgx-deb.key",
            ),
        ]

    @property
    def kernel_package(self) -> str:
        return "linux-image-7.0.0-31-generic"

    @property
    def packages(self) -> list[str]:
        return [
            "qemu-system-x86",
            "ovmf-inteltdx",
            "sgx-dcap-pccs",
            "tdx-qgs",
            "libsgx-dcap-default-qpl",
            "sgx-ra-service",
            "sgx-pck-id-retrieval-tool",
        ]

    @property
    def grub_cmdline_additions(self) -> list[str]:
        return ["nohibernate", "kvm_intel.tdx=1", "modprobe.blacklist=nouveau"]


HOST_PROFILES: dict[str, HostProfile] = {
    "26.04": Ubuntu2604Profile(),
}


def detect_ubuntu_version() -> str:
    """Detect the running Ubuntu version via lsb_release."""
    result = proc.run(
        ["lsb_release", "-rs"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to detect Ubuntu version via lsb_release. "
            "Is this an Ubuntu system?"
        )
    return result.stdout.strip()


def resolve_profile(version: str | None = None) -> HostProfile:
    """Resolve a HostProfile for the given (or detected) Ubuntu version.

    Raises ValueError if the version is not supported.
    """
    if version is None:
        version = detect_ubuntu_version()

    profile = HOST_PROFILES.get(version)
    if profile is None:
        raise ValueError(
            f"Unsupported Ubuntu version: {version}. "
            f"Supported: {list(HOST_PROFILES.keys())}"
        )
    return profile
