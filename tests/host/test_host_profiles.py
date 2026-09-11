"""Unit tests for host profile registry and setup orchestration.

Tests focus on behavioral contracts, registry integrity, and setup
orchestration logic (mocking all subprocess/OS calls).
"""

from unittest.mock import MagicMock, patch

import pytest
from chutes_cvm.host.profiles import (
    HOST_PROFILES,
    PPA,
    HostProfile,
    Ubuntu2604Profile,
    resolve_profile,
)
from chutes_cvm.host.setup import (
    _ensure_chutes_dirs,
    _get_kernel_version,
    _setup_ntp,
    setup_host,
)

# ---------------------------------------------------------------------------
# PPA dataclass
# ---------------------------------------------------------------------------


def test_ppa_uri_format():
    ppa = PPA("kobuk-team", "tdx-release", signing_key="AABBCCDD")
    assert ppa.uri == "ppa:kobuk-team/tdx-release"


def test_ppa_default_pin_priority():
    ppa = PPA("team", "name", signing_key="AABB")
    assert ppa.pin_priority == 4000


def test_ppa_custom_pin_priority():
    ppa = PPA("team", "name", signing_key="AABB", pin_priority=500)
    assert ppa.pin_priority == 500


def test_ppa_suite_defaults_to_none():
    ppa = PPA("team", "name", signing_key="AABB")
    assert ppa.suite is None


def test_ppa_suite_override():
    ppa = PPA("team", "name", signing_key="AABB", suite="oracular")
    assert ppa.suite == "oracular"


def test_ppa_signing_key_required():
    """Every PPA in every profile must declare a signing key."""
    for version, profile in HOST_PROFILES.items():
        for ppa in profile.ppas:
            assert ppa.signing_key, f"{version} PPA {ppa.name} missing signing_key"


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


def test_all_registered_profiles_are_host_profile_subclasses():
    for key, profile in HOST_PROFILES.items():
        assert isinstance(profile, HostProfile), f"{key} is not a HostProfile"


def test_registry_keys_match_profile_names():
    for key, profile in HOST_PROFILES.items():
        assert (
            key == profile.name
        ), f"Registry key '{key}' does not match profile.name '{profile.name}'"


def test_no_duplicate_codenames():
    codenames = [p.codename for p in HOST_PROFILES.values()]
    assert len(codenames) == len(set(codenames)), "Duplicate codenames in registry"


# ---------------------------------------------------------------------------
# All profiles: common contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", list(HOST_PROFILES.keys()))
def test_every_profile_has_nohibernate(version):
    """nohibernate must be in every profile's GRUB cmdline."""
    profile = HOST_PROFILES[version]
    assert "nohibernate" in profile.grub_cmdline_additions


@pytest.mark.parametrize("version", list(HOST_PROFILES.keys()))
def test_every_profile_includes_attestation_packages(version):
    """Attestation is mandatory on every host -- packages must be present."""
    profile = HOST_PROFILES[version]
    required = {"sgx-dcap-pccs", "tdx-qgs", "libsgx-dcap-default-qpl"}
    assert required.issubset(
        set(profile.packages)
    ), f"{version} missing attestation packages: {required - set(profile.packages)}"


@pytest.mark.parametrize("version", list(HOST_PROFILES.keys()))
def test_every_profile_includes_qemu(version):
    profile = HOST_PROFILES[version]
    assert "qemu-system-x86" in profile.packages


@pytest.mark.parametrize("version", list(HOST_PROFILES.keys()))
def test_describe_contains_version_and_codename(version):
    profile = HOST_PROFILES[version]
    desc = profile.describe()
    assert profile.name in desc
    assert profile.codename in desc


@pytest.mark.parametrize(
    "profile_cls",
    [Ubuntu2604Profile],
)
def test_host_profiles_do_not_include_libvirt(profile_cls):
    """libvirt is not needed — VFIO prep uses direct PCI remove+rescan."""
    profile = profile_cls()
    assert "libvirt-daemon-system" not in profile.packages
    assert "libvirt-clients" not in profile.packages


# ---------------------------------------------------------------------------
# Every profile: Intel DCAP repo required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", list(HOST_PROFILES.keys()))
def test_every_profile_has_intel_sgx_repo(version):
    """All supported profiles must source attestation from Intel's DCAP repo."""
    profile = HOST_PROFILES[version]
    intel_repos = [r for r in profile.repos if r.name == "intel-sgx"]
    assert len(intel_repos) == 1, f"{version} missing intel-sgx repo"
    assert "download.01.org" in intel_repos[0].uri
    assert intel_repos[0].components == "main"
    assert intel_repos[0].signing_key_url.endswith("intel-sgx-deb.key")


@pytest.mark.parametrize("version", list(HOST_PROFILES.keys()))
def test_every_profile_has_no_kobuk_ppas(version):
    """No profile should reference kobuk-team PPAs (unreliable, superseded by Intel DCAP)."""
    profile = HOST_PROFILES[version]
    kobuk_ppas = [p for p in profile.ppas if "kobuk" in p.team]
    assert kobuk_ppas == [], f"{version} still has kobuk PPAs: {kobuk_ppas}"


# ---------------------------------------------------------------------------
# Ubuntu 26.04 specifics
# ---------------------------------------------------------------------------


def test_2604_does_not_need_tdx_release_ppa():
    """26.04 has native TDX kernel/QEMU -- no tdx-release PPA needed."""
    profile = Ubuntu2604Profile()
    ppa_names = {ppa.name for ppa in profile.ppas}
    assert "tdx-release" not in ppa_names


def test_2604_has_intel_sgx_repo():
    """26.04 uses Intel's official SGX/DCAP repository (resolute suite)."""
    profile = Ubuntu2604Profile()
    assert len(profile.repos) >= 1
    intel_repos = [r for r in profile.repos if r.name == "intel-sgx"]
    assert len(intel_repos) == 1
    assert intel_repos[0].suite == "resolute"
    assert "download.01.org" in intel_repos[0].uri


def test_2604_pins_kernel_package():
    profile = Ubuntu2604Profile()
    assert profile.kernel_package == "linux-image-7.0.0-31-generic"


def test_2604_enables_kvm_intel_tdx():
    """26.04 requires explicit kvm_intel.tdx=1 kernel param."""
    profile = Ubuntu2604Profile()
    assert "kvm_intel.tdx=1" in profile.grub_cmdline_additions


# ---------------------------------------------------------------------------
# resolve_profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", list(HOST_PROFILES.keys()))
def test_resolve_profile_returns_correct_instance(version):
    profile = resolve_profile(version)
    assert profile is HOST_PROFILES[version]


def test_resolve_profile_rejects_unsupported_version():
    with pytest.raises(ValueError, match="Unsupported Ubuntu version"):
        resolve_profile("18.04")


def test_resolve_profile_rejects_2510():
    # 26.04 is the only supported host OS; 25.10 hosts must upgrade first.
    with pytest.raises(ValueError, match="Unsupported Ubuntu version"):
        resolve_profile("25.10")


@patch("chutes_cvm.host.profiles.detect_ubuntu_version", return_value="26.04")
def test_resolve_profile_auto_detects(mock_detect):
    profile = resolve_profile(None)
    assert isinstance(profile, Ubuntu2604Profile)
    mock_detect.assert_called_once()


@patch("chutes_cvm.host.profiles.detect_ubuntu_version", return_value="99.99")
def test_resolve_profile_auto_detect_unsupported(mock_detect):
    with pytest.raises(ValueError, match="Unsupported Ubuntu version"):
        resolve_profile(None)


# ---------------------------------------------------------------------------
# _get_kernel_version: regex parsing
# ---------------------------------------------------------------------------


def test_get_kernel_version_parses_pinned_package():
    assert _get_kernel_version("linux-image-6.17.0-35-generic") == "6.17.0-35-generic"


def test_get_kernel_version_rejects_metapackage():
    with pytest.raises(ValueError, match="must be a pinned version"):
        _get_kernel_version("linux-image-generic")


# ---------------------------------------------------------------------------
# setup_host: orchestration (all subprocess calls mocked)
# ---------------------------------------------------------------------------


@patch("chutes_cvm.host.setup._ensure_chutes_dirs")
@patch("chutes_cvm.host.setup._setup_ntp")
@patch("chutes_cvm.host.setup._add_user_to_kvm")
@patch("chutes_cvm.host.setup._grub_update_cmdline")
@patch("chutes_cvm.host.setup._grub_set_kernel")
@patch("chutes_cvm.host.setup._get_kernel_version", return_value="6.17.0-15-generic")
@patch("chutes_cvm.host.setup._run")
@patch("os.geteuid", return_value=0)
def test_setup_host_calls_all_steps(
    mock_euid,
    mock_run,
    mock_kver,
    mock_grub_kernel,
    mock_grub_cmdline,
    mock_kvm,
    mock_ntp,
    mock_dirs,
):
    profile = Ubuntu2604Profile()
    setup_host(profile)

    mock_kver.assert_called_once_with(profile.kernel_package)
    mock_grub_kernel.assert_called_once_with("6.17.0-15-generic")
    mock_grub_cmdline.assert_called_once_with(profile.grub_cmdline_additions)
    mock_kvm.assert_called_once()
    # The folded-in per-host config steps run as part of setup-host.
    mock_ntp.assert_called_once()
    mock_dirs.assert_called_once()

    install_calls = [
        c for c in mock_run.call_args_list if len(c[0]) > 0 and "install" in c[0][0]
    ]
    assert len(install_calls) > 0, "apt install should have been called"
    # base_packages (chrony/aria2/xfsprogs) are installed alongside the kernel + TDX stack.
    installed = [pkg for c in install_calls for pkg in c[0][0]]
    assert "chrony" in installed and "aria2" in installed and "xfsprogs" in installed


@patch("os.geteuid", return_value=1000)
def test_setup_host_exits_if_not_root(mock_euid):
    profile = Ubuntu2604Profile()
    with pytest.raises(SystemExit):
        setup_host(profile)


# ---------------------------------------------------------------------------
# base_packages + folded-in host config (ntp / chutes_dirs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", list(HOST_PROFILES.keys()))
def test_every_profile_base_packages_include_host_deps(version):
    """The folded-in host operational deps must be present so setup-host fully provisions."""
    profile = HOST_PROFILES[version]
    assert {"chrony", "aria2", "xfsprogs"}.issubset(set(profile.base_packages))


@patch("chutes_cvm.host.setup._run")
@patch("chutes_cvm.host.setup._write_system_file")
@patch("chutes_cvm.host.setup.proc.run", return_value=MagicMock(returncode=0))
def test_setup_ntp_masks_timesyncd_writes_conf_and_enables_chrony(
    mock_sub, mock_write, mock_run
):
    _setup_ntp()
    # systemd-timesyncd is masked (chrony owns the clock).
    assert any(
        "systemd-timesyncd" in c.args[0] and "mask" in c.args[0]
        for c in mock_sub.call_args_list
    )
    # chrony.conf is written with makestep (immediate step, not slew).
    assert mock_write.call_args.args[0] == "/etc/chrony/chrony.conf"
    assert "makestep" in mock_write.call_args.args[1]
    # chrony is enabled + started.
    assert any(
        "chrony" in c.args[0] and "enable" in c.args[0] for c in mock_run.call_args_list
    )


@patch("chutes_cvm.host.setup.proc.run", return_value=MagicMock(returncode=1))
@patch("chutes_cvm.host.setup._write_system_file")
@patch("chutes_cvm.host.setup._run")
def test_setup_ntp_tolerates_waitsync_failure(mock_run, mock_write, mock_sub):
    # A non-zero waitsync (clock not yet synced) must not raise — setup continues.
    _setup_ntp()  # returncode=1 on the tolerant subprocess calls; no exception


@patch("chutes_cvm.host.setup.os.chmod")
@patch("chutes_cvm.host.setup.os.makedirs")
def test_ensure_chutes_dirs_creates_expected(mock_makedirs, mock_chmod):
    _ensure_chutes_dirs()
    made = [c.args[0] for c in mock_makedirs.call_args_list]
    assert "/var/lib/chutes/base-images" in made
    assert "/var/lib/chutes/vm-images" in made
    # created idempotently
    assert all(c.kwargs.get("exist_ok") for c in mock_makedirs.call_args_list)
