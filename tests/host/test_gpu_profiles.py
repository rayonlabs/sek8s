"""Unit tests for GPU profile registry (host-tools).

Tests focus on behavioral contracts and logic branches, not static values.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
import topology_fixtures as known
from chutes_cvm.guest.gpu.profiles import (
    GPU_PROFILES,
    HOST_RESERVED_CPUS,
    GpuProfile,
    resolve_profile,
)
from chutes_cvm.guest.gpu.topology import (
    CpuTopology,
    FlatTopology,
    NumaTopology,
    TopologyFingerprint,
)

# ---------------------------------------------------------------------------
# Host-shape fixtures: the RTMR0-determining host facts carried on the topology
# fingerprint (vcpus/sockets/mem_gb + CPU identity), mirroring real host classes
# (see tests/topology_fixtures.py) so the detect/fingerprint tests reproduce a
# real host's shape deterministically.
# ---------------------------------------------------------------------------
_B200_XEON_SHAPE = dict(
    vcpus=176,
    sockets=2,
    cpu_vendor="GenuineIntel",
    cpu_processor_id=None,
)
_H200_SHAPE = dict(
    vcpus=124,
    sockets=2,
    cpu_vendor="GenuineIntel",
    cpu_processor_id="f2060c00fffba91f",
)
_B300_SHAPE = dict(
    vcpus=188,
    sockets=2,
    cpu_vendor="GenuineIntel",
    cpu_processor_id=None,
)
# A realistic B200 (Xeon) fingerprint (tests/topology_fixtures.py), used as the
# stand-in "live" fingerprint detect_profile should return for a B200 host.
_B200_LIVE_FP = known.B200_XEON_FP


# ---------------------------------------------------------------------------
# matches_device_id: case-insensitive matching logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "device_id",
    ["2bb5", "2BB5", "2Bb5"],
)
def test_device_id_matching_is_case_insensitive(device_id):
    profile = GPU_PROFILES["RTX_PRO_6000"]
    assert profile.matches_device_id(device_id)


def test_device_id_rejects_other_profiles_ids():
    """Device IDs are unique per profile now (host CPU/RAM variants are
    fingerprints, not sibling profiles), so every profile must reject every
    OTHER profile's device IDs."""
    for key, profile in GPU_PROFILES.items():
        foreign_ids = [p.pci_device_id for k, p in GPU_PROFILES.items() if k != key]
        for foreign_id in foreign_ids:
            assert not profile.matches_device_id(
                foreign_id
            ), f"{key} should not match {foreign_id}"


# ---------------------------------------------------------------------------
# Registry integrity: PCI device IDs must be unique across profiles
# ---------------------------------------------------------------------------


def test_no_duplicate_pci_device_ids_across_profiles():
    """No two profiles may share a device ID.

    Host CPU/RAM variants (e.g. B200 on Xeon vs Xeon 6) are now fingerprints of
    one profile, not separate profiles, so a device ID resolves a single profile.
    Any duplication is a registration error _match_gpu_model would raise on.
    """
    by_device_id: dict[str, list[str]] = {}
    for key, profile in GPU_PROFILES.items():
        by_device_id.setdefault(profile.pci_device_id.lower(), []).append(key)

    dupes = {pid: keys for pid, keys in by_device_id.items() if len(keys) > 1}
    assert not dupes, f"device IDs claimed by multiple profiles: {dupes}"


def test_every_profile_declares_exactly_one_device_id():
    """One product per profile — the field is a string, so this guards the values.

    A profile carries far more than a BAR layout: reserved CPUs, the guest-RAM rule, NUMA
    handling, firmware, expected_gpus and the CC/PPCIe mode arguments. Two products that
    agree on all of those today can diverge later with nothing to notice, so distinct
    hardware gets a distinct profile. RTX_PRO_6000 previously covered both the Workstation
    and Server editions, and the BAR layout it carries was measured on the Server card.
    """
    for key, profile in GPU_PROFILES.items():
        assert isinstance(
            profile.pci_device_id, str
        ), f"{key}: device id must be a string"
        assert profile.pci_device_id, f"{key}: device id must not be empty"


def test_the_passthrough_stub_names_the_profile_s_own_gpu():
    """The offline stub stands in for the real card, so it must be the same device.

    It used to declare 2bb1 while the profile also matched 2bb5 and the BAR layout came
    from a 2bb5 card — the stub named a product the measurement was not taken from.
    """
    for key, profile in GPU_PROFILES.items():
        stub = profile.passthrough.get("gpu")
        if stub is None:
            continue
        assert (
            stub.device_id.lower() == profile.pci_device_id.lower()
        ), f"{key}: stub device id {stub.device_id} != profile {profile.pci_device_id}"


def test_all_registered_profiles_are_gpu_profile_subclasses():
    for key, profile in GPU_PROFILES.items():
        assert isinstance(profile, GpuProfile), f"{key} is not a GpuProfile"


# ---------------------------------------------------------------------------
# RTX Pro 6000 behavioral contracts (no NVSwitch, no PPCIe, no IB)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gpu_count", [1, 2, 4, 8])
def test_rtx_pro_6000_never_uses_ppcie(gpu_count):
    """RTX Pro 6000 has no NVSwitch fabric, so PPCIe is never applicable."""
    profile = GPU_PROFILES["RTX_PRO_6000"]
    for arg_list in profile.get_cc_mode_args(gpu_count):
        joined = " ".join(arg_list).lower()
        assert "ppcie" not in joined


@pytest.mark.parametrize("gpu_count", [1, 2, 4, 8])
def test_rtx_pro_6000_cc_mode_is_count_independent(gpu_count):
    """Unlike H200, RTX Pro 6000 CC args don't change with GPU count."""
    profile = GPU_PROFILES["RTX_PRO_6000"]
    args = profile.get_cc_mode_args(gpu_count)
    assert len(args) == 1, "Should be a single nvidia-gpu-tools invocation"
    assert "--set-cc-mode=on" in args[0]


@pytest.mark.parametrize("gpu_count", [1, 2, 4, 8])
def test_rtx_pro_6000_never_passes_through_nvswitches(gpu_count):
    profile = GPU_PROFILES["RTX_PRO_6000"]
    assert profile.should_passthrough_nvswitches(gpu_count) is False


# ---------------------------------------------------------------------------
# NUMA topology and post-launch tuning flags
# ---------------------------------------------------------------------------


def test_b200_enables_numa_topology():
    profile = GPU_PROFILES["B200"]
    assert profile.enable_numa_topology is True


def test_b200_enables_post_launch_tuning():
    profile = GPU_PROFILES["B200"]
    assert profile.enable_post_launch_tuning is True


@pytest.mark.parametrize("model_key", ["H200", "RTX_PRO_6000"])
def test_h200_and_rtx_enable_numa_topology(model_key):
    profile = GPU_PROFILES[model_key]
    assert profile.enable_numa_topology is True
    assert profile.enable_post_launch_tuning is True


def test_b300_does_not_enable_numa_topology():
    # B300 hardware topology not yet confirmed via discover-profile.sh.
    profile = GPU_PROFILES["B300"]
    assert profile.enable_numa_topology is False
    assert profile.enable_post_launch_tuning is False


# ---------------------------------------------------------------------------
# Blackwell HGX (B200 / B300): CC mode, host-side NVSwitch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_key", ["B200", "B300"])
@pytest.mark.parametrize("gpu_count", [1, 2, 4, 8])
def test_blackwell_hgx_uses_cc_mode_only(model_key, gpu_count):
    profile = GPU_PROFILES[model_key]
    args = profile.get_cc_mode_args(gpu_count)
    assert len(args) == 1
    assert "--set-cc-mode=on" in args[0]
    flat = " ".join(args[0]).lower()
    assert "ppcie" not in flat


@pytest.mark.parametrize("model_key", ["B200", "B300"])
@pytest.mark.parametrize("gpu_count", [1, 2, 4, 8])
def test_blackwell_hgx_never_passes_through_nvswitches(model_key, gpu_count):
    profile = GPU_PROFILES[model_key]
    assert profile.should_passthrough_nvswitches(gpu_count) is False


@pytest.mark.parametrize("model_key", ["B200", "B300", "RTX_PRO_6000"])
def test_cc_mode_profiles_use_cc_sbr_reset(model_key):
    profile = GPU_PROFILES[model_key]
    args = profile.get_sbr_reset_args()
    assert args == ["--reset-with-sbr", "--reset-after-cc-mode-switch"]


def test_h200_uses_ppcie_sbr_reset():
    profile = GPU_PROFILES["H200"]
    args = profile.get_sbr_reset_args()
    assert args == ["--reset-with-sbr", "--reset-after-ppcie-mode-switch"]


def test_b200_does_not_pass_through_infiniband():
    """IB passthrough removed: it added no value and varied RTMR0 per NIC loadout;
    guest networking is virtio-net (matching H200/B300)."""
    assert GPU_PROFILES["B200"].should_passthrough_infiniband is False


def test_b300_does_not_pass_through_infiniband():
    """B300 HGX: all IB-class CX7 PFs are NVSwitch bridges; guest uses virtio-net."""
    assert GPU_PROFILES["B300"].should_passthrough_infiniband is False


def test_b300_skips_ovmf_mmio_fw_cfg():
    """B300: 8×512 GiB BARs need OVMF auto-sized aggregate MMIO, not per-GPU fw_cfg."""
    assert GPU_PROFILES["B300"].use_ovmf_mmio_fw_cfg is False


@pytest.mark.parametrize("model_key", ["B200", "H200", "RTX_PRO_6000"])
def test_other_profiles_use_ovmf_mmio_fw_cfg(model_key):
    assert GPU_PROFILES[model_key].use_ovmf_mmio_fw_cfg is True


def test_b300_matches_pci_device_id_3182():
    profile = GPU_PROFILES["B300"]
    assert profile.matches_device_id("3182")
    assert profile.matches_device_id("3182".upper())


# ---------------------------------------------------------------------------
# H200 conditional logic: PPCIe vs CC depends on GPU count
# ---------------------------------------------------------------------------


def test_h200_switches_to_ppcie_at_8_gpus():
    """H200 uses PPCIe mode (NVSwitch fabric) when all 8 GPUs are present."""
    profile = GPU_PROFILES["H200"]
    args = profile.get_cc_mode_args(8)
    flat = [a for invocation in args for a in invocation]
    assert "--set-ppcie-mode=on" in flat
    assert "--set-cc-mode=off" in flat
    assert profile.should_passthrough_nvswitches(8) is True


def test_h200_uses_cc_mode_below_8_gpus():
    """H200 falls back to CC mode (no NVSwitch) for partial GPU configs."""
    profile = GPU_PROFILES["H200"]
    args = profile.get_cc_mode_args(4)
    flat = [a for invocation in args for a in invocation]
    assert "--set-cc-mode=on" in flat
    assert "--set-ppcie-mode=off" in flat
    assert profile.should_passthrough_nvswitches(4) is False


# ---------------------------------------------------------------------------
# vCPU / SMP shape lives on the TopologyFingerprint. These assert the
# -smp-determining fields of the sample topologies (tests/topology_fixtures.py).
# ---------------------------------------------------------------------------

_SAMPLE_FINGERPRINTS = (
    "H200_KR6288",
    "H200_XE9680",
    "B200_XEON_FP",
    "B200_XEON6_FP",
    "RTX_NUMA",
    "RTX_FLAT",
)


def _all_baselined_fingerprints():
    """(name, fingerprint) for the sample topologies — exercises each one's -smp shape."""
    return [(name, getattr(known, name)) for name in _SAMPLE_FINGERPRINTS]


def test_some_profiles_are_baselined():
    """Guard: the fingerprint-parametrized tests below must not silently no-op."""
    assert _all_baselined_fingerprints()


@pytest.mark.parametrize("model_key", list(GPU_PROFILES.keys()))
def test_host_reserved_cpus_is_even(model_key):
    """Reserve must be even so vcpus stays divisible across sockets."""
    profile = GPU_PROFILES[model_key]
    assert profile.host_reserved_cpus % 2 == 0


def test_host_reserved_cpus_default_and_b200_override():
    """Default reserve is HOST_RESERVED_CPUS; B200 overrides to 16."""
    assert GPU_PROFILES["H200"].host_reserved_cpus == HOST_RESERVED_CPUS
    assert GPU_PROFILES["B300"].host_reserved_cpus == HOST_RESERVED_CPUS
    assert GPU_PROFILES["B200"].host_reserved_cpus == 16


@pytest.mark.parametrize("key,fp", _all_baselined_fingerprints())
def test_baselined_fingerprint_vcpus_positive_and_matches_smp(key, fp):
    """The first -smp field must equal vcpus, and vcpus must be positive."""
    assert fp.cpu.vcpus > 0
    assert int(fp.cpu.smp_topology.split(",")[0]) == fp.cpu.vcpus


@pytest.mark.parametrize("key,fp", _all_baselined_fingerprints())
def test_baselined_fingerprint_uses_two_sockets(key, fp):
    """2-socket servers must reflect the physical socket count in -smp.

    A flat sockets=1 topology causes QEMU to emit a degenerate CPUID with only a
    thread level and 0-bit shift — no core or package levels — which triggers the
    kernel 'arch topology borken' warning on every vCPU at boot.
    """
    assert fp.cpu.sockets == 2
    assert "sockets=2" in fp.cpu.smp_topology


@pytest.mark.parametrize("key,fp", _all_baselined_fingerprints())
def test_baselined_fingerprint_vcpus_divisible_by_sockets(key, fp):
    """vcpus must divide evenly across sockets so each socket has equal cores."""
    assert (
        fp.cpu.vcpus % fp.cpu.sockets == 0
    ), f"{key}: vcpus={fp.cpu.vcpus} not divisible by sockets={fp.cpu.sockets}"


@pytest.mark.parametrize("key,fp", _all_baselined_fingerprints())
def test_baselined_fingerprint_threads_is_one(key, fp):
    """threads=1 must always be set (no guest SMT)."""
    assert "threads=1" in fp.cpu.smp_topology


# ---------------------------------------------------------------------------
# resolve_profile: resolution logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_key", list(GPU_PROFILES.keys()))
def test_resolve_profile_returns_correct_type(model_key):
    models = {"0000:41:00.0": model_key}
    profile = resolve_profile(models)
    assert profile is GPU_PROFILES[model_key]


def test_resolve_profile_with_multiple_identical_gpus():
    models = {f"0000:4{i}:00.0": "RTX_PRO_6000" for i in range(8)}
    profile = resolve_profile(models)
    assert profile is GPU_PROFILES["RTX_PRO_6000"]


def test_resolve_profile_filters_out_default_entries():
    """'default' entries (unrecognized GPUs) are ignored if a real model exists."""
    models = {
        "0000:41:00.0": "B200",
        "0000:42:00.0": "default",
        "0000:43:00.0": "default",
    }
    profile = resolve_profile(models)
    assert profile is GPU_PROFILES["B200"]


def test_resolve_profile_rejects_mixed_models():
    models = {
        "0000:41:00.0": "B200",
        "0000:42:00.0": "H200",
    }
    with pytest.raises(ValueError, match="Mixed GPU models"):
        resolve_profile(models)


def test_resolve_profile_rejects_unsupported_model():
    models = {"0000:41:00.0": "TITAN_V"}
    with pytest.raises(ValueError, match="Unsupported GPU model"):
        resolve_profile(models)


def test_resolve_profile_rejects_all_default():
    """If every GPU is 'default' (unrecognized), resolution must fail."""
    models = {"0000:41:00.0": "default", "0000:42:00.0": "default"}
    with pytest.raises(ValueError, match="No supported GPU models"):
        resolve_profile(models)


# ---------------------------------------------------------------------------
# _match_gpu_model: device ID -> profile (device IDs are unique now)
# ---------------------------------------------------------------------------


def test_match_gpu_model_resolves_by_device_id():
    from chutes_cvm.guest.detection import _match_gpu_model

    b200 = "0000:0d:00.0 3D controller [0302]: NVIDIA [B200] [10de:2901] (rev a1)"
    b300 = "0000:0d:00.0 3D controller [0302]: NVIDIA [B300] [10de:3182] (rev a1)"
    assert _match_gpu_model(b200) == "B200"
    assert _match_gpu_model(b300) == "B300"


def test_match_gpu_model_returns_none_for_unknown_device():
    from chutes_cvm.guest.detection import _match_gpu_model

    line = "0000:0d:00.0 3D controller [0302]: NVIDIA [Unknown] [10de:ffff] (rev a1)"
    assert _match_gpu_model(line) is None


# ---------------------------------------------------------------------------
# verify_host_qemu_supported: QEMU host-readiness gate (pre-resolution)
# ---------------------------------------------------------------------------


def test_detect_qemu_version_parses_upstream_version():
    from chutes_cvm.guest import detection

    fake = type(
        "R",
        (),
        {"stdout": "QEMU emulator version 10.2.1 (Debian 1:10.2.1+ds-1ubuntu3.1)\n"},
    )()
    with patch("chutes_cvm.guest.detection.proc.run", return_value=fake):
        assert detection.detect_qemu_version() == "10.2.1"


def test_verify_host_qemu_supported_passes_when_qemu_matches_os():
    from chutes_cvm.guest.detection import (
        SUPPORTED_QEMU_BY_OS,
        verify_host_qemu_supported,
    )

    os_ver, qemu_ver = next(iter(SUPPORTED_QEMU_BY_OS.items()))
    with patch("chutes_cvm.guest.detection.detect_os_version", return_value=os_ver):
        with patch(
            "chutes_cvm.guest.detection.detect_qemu_version", return_value=qemu_ver
        ):
            verify_host_qemu_supported()  # must not raise


def test_verify_host_qemu_supported_raises_when_qemu_mismatches_os():
    from chutes_cvm.guest.detection import verify_host_qemu_supported

    # 26.04 ships 10.2.1; a host on 26.04 running 10.1.0 must be flagged.
    with patch("chutes_cvm.guest.detection.detect_os_version", return_value="26.04"):
        with patch(
            "chutes_cvm.guest.detection.detect_qemu_version", return_value="10.1.0"
        ):
            with pytest.raises(
                ValueError, match=r"ships \(and we baseline\) QEMU 10\.2\.1"
            ):
                verify_host_qemu_supported()


def test_verify_host_qemu_supported_raises_on_unsupported_os():
    from chutes_cvm.guest.detection import verify_host_qemu_supported

    with patch("chutes_cvm.guest.detection.detect_os_version", return_value="24.04"):
        with patch(
            "chutes_cvm.guest.detection.detect_qemu_version", return_value="8.2.2"
        ):
            with pytest.raises(
                ValueError, match=r"OS release '24.04' is not supported"
            ):
                verify_host_qemu_supported()


def test_verify_host_qemu_supported_raises_when_qemu_undetectable():
    from chutes_cvm.guest.detection import verify_host_qemu_supported

    with patch("chutes_cvm.guest.detection.detect_qemu_version", return_value=None):
        with pytest.raises(
            ValueError, match="Could not determine the host QEMU version"
        ):
            verify_host_qemu_supported()


# ---------------------------------------------------------------------------
# detect_profile: full topology detection (returns (profile, fingerprint))
# ---------------------------------------------------------------------------


def _make_lspci_b200(bdf: str = "0000:0d:00.0") -> list[str]:
    return [f"{bdf} 3D controller [0302]: NVIDIA [B200] [10de:2901] (rev a1)"]


def _patch_detection(
    lspci_lines=None,
    numa_count=2,
    nvswitch_bdfs=None,
    ib_pf_bdfs=None,
    gpu_bdfs=None,
    fingerprint=_B200_LIVE_FP,
):
    """Return a context manager stack that patches all detection side effects.

    ``fingerprint`` is what host_topology_fingerprint() returns; the default is a
    full-shape B200 (Xeon) fingerprint (tests/topology_fixtures.py) so B200 resolution
    tests get a realistic live shape. detect_profile no longer gates on any local set.
    """
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "chutes_cvm.guest.detection.host_topology_fingerprint",
            return_value=fingerprint,
        )
    )
    stack.enter_context(
        patch("chutes_cvm.guest.detection._lspci_lines", return_value=lspci_lines or [])
    )
    stack.enter_context(
        patch(
            "chutes_cvm.guest.detection.detect_numa_node_count", return_value=numa_count
        )
    )
    stack.enter_context(
        patch(
            "chutes_cvm.guest.detection.detect_nvswitches",
            return_value=nvswitch_bdfs or [],
        )
    )
    stack.enter_context(
        patch(
            "chutes_cvm.guest.detection.detect_infiniband_pfs",
            return_value=ib_pf_bdfs or [],
        )
    )
    stack.enter_context(
        patch("chutes_cvm.guest.detection.detect_cx7_bridge_pfs", return_value=[])
    )
    bdfs = gpu_bdfs if gpu_bdfs is not None else ["0000:0d:00.0"]
    stack.enter_context(
        patch("chutes_cvm.guest.detection.get_gpu_bdfs", return_value=bdfs)
    )
    return stack


def test_detect_profile_returns_correct_profile():
    from chutes_cvm.guest.detection import detect_profile

    with _patch_detection(lspci_lines=_make_lspci_b200()):
        profile, fingerprint = detect_profile()

    assert profile is GPU_PROFILES["B200"]
    assert fingerprint == _B200_LIVE_FP


@pytest.mark.parametrize(
    "numa_count,fingerprint",
    [
        # 2 NUMA nodes -> guest-NUMA path.
        (2, known.RTX_NUMA),
        # 4 NUMA nodes -> flat fallback.
        (4, known.RTX_FLAT),
    ],
)
def test_detect_profile_accepts_baselined_rtx_topologies(numa_count, fingerprint):
    """Both RTX Pro 6000 host shapes (2-node NUMA and 4-node flat) are baselined
    and must pass the launch-time topology hard-match."""
    from chutes_cvm.guest.detection import detect_profile

    rtx_lines = [
        f"0000:{i:02x}:00.0 3D controller [0302]: NVIDIA "
        f"[RTX PRO 6000 Blackwell Server Edition] [10de:2bb5] (rev a1)"
        for i in range(8)
    ]
    bdfs = [f"0000:{i:02x}:00.0" for i in range(8)]
    with _patch_detection(
        lspci_lines=rtx_lines,
        numa_count=numa_count,
        gpu_bdfs=bdfs,
        fingerprint=fingerprint,
    ):
        profile, _ = detect_profile()

    assert profile is GPU_PROFILES["RTX_PRO_6000"]


def test_detect_profile_raises_when_nvswitches_expected_but_missing():
    from chutes_cvm.guest.detection import detect_profile

    h200_lines = [
        f"0000:{i:02x}:00.0 3D controller [0302]: NVIDIA [H200] [10de:2335] (rev a1)"
        for i in range(8)
    ]
    bdfs = [f"0000:{i:02x}:00.0" for i in range(8)]
    with _patch_detection(
        lspci_lines=h200_lines,
        nvswitch_bdfs=[],
        gpu_bdfs=bdfs,
    ):
        with pytest.raises(ValueError, match="NVSwitch"):
            detect_profile()


def test_detect_profile_raises_when_no_gpus():
    from chutes_cvm.guest.detection import detect_profile

    with _patch_detection(gpu_bdfs=[]):
        with pytest.raises(ValueError, match="No GPU devices detected"):
            detect_profile()


# ---------------------------------------------------------------------------
# host_topology_fingerprint + topology hard-match
# ---------------------------------------------------------------------------


@contextmanager
def _patch_host_shape(*, cpus, sockets, mem_gb, vendor, proc_id):
    """Pin the four host-shape detectors host_topology_fingerprint reads so the
    resulting fingerprint's shape is deterministic."""
    with patch("chutes_cvm.guest.detection.detect_host_cpus", return_value=cpus), patch(
        "chutes_cvm.guest.detection.detect_host_sockets", return_value=sockets
    ), patch(
        "chutes_cvm.guest.detection.detect_host_mem_gb", return_value=mem_gb
    ), patch(
        "chutes_cvm.guest.detection.detect_host_cpu_identity",
        return_value=(vendor, proc_id),
    ):
        yield


def test_topology_fingerprint_numa_path_includes_device_layout():
    from chutes_cvm.guest.detection import host_topology_fingerprint

    profile = GPU_PROFILES["H200"]  # enable_numa_topology = True; guest_mem = 141*8
    with _patch_host_shape(
        cpus=128,
        sockets=2,
        mem_gb=2048,
        vendor="GenuineIntel",
        proc_id="f2060c00fffba91f",
    ):
        with patch("chutes_cvm.guest.detection.detect_numa_node_count", return_value=2):
            with patch(
                "chutes_cvm.guest.detection._device_numa_layout",
                side_effect=[(0, 0, 0, 0, 1, 1, 1, 1), (1, 1, 1, 1), ()],
            ):
                fp = host_topology_fingerprint(profile, ["g"] * 8, ["n"] * 4, [])
    assert fp == known.H200_XE9680


def test_topology_fingerprint_flat_when_not_two_numa_nodes():
    from chutes_cvm.guest.detection import host_topology_fingerprint

    profile = GPU_PROFILES["H200"]
    with _patch_host_shape(
        cpus=128,
        sockets=2,
        mem_gb=2048,
        vendor="GenuineIntel",
        proc_id="f2060c00fffba91f",
    ):
        with patch("chutes_cvm.guest.detection.detect_numa_node_count", return_value=4):
            fp = host_topology_fingerprint(profile, ["g"] * 8, ["n"] * 4, [])
    assert fp == TopologyFingerprint(
        CpuTopology(**_H200_SHAPE), 1128, FlatTopology(gpu_count=8, nvswitch_count=4)
    )


def test_topology_fingerprint_flat_when_profile_disables_numa():
    # B300 never uses guest NUMA topology -> flat regardless of host node count.
    from chutes_cvm.guest.detection import host_topology_fingerprint

    profile = GPU_PROFILES["B300"]  # guest_mem = 288*8 = 2304
    with _patch_host_shape(
        cpus=192,
        sockets=2,
        mem_gb=3000,
        vendor="GenuineIntel",
        proc_id=None,
    ):
        with patch("chutes_cvm.guest.detection.detect_numa_node_count", return_value=2):
            fp = host_topology_fingerprint(profile, ["g"] * 8, [], [])
    assert fp == TopologyFingerprint(
        CpuTopology(**_B300_SHAPE), 2304, FlatTopology(gpu_count=8)
    )


def test_topology_fingerprint_includes_ib_layout_on_numa_path():
    # Two B200 hosts with the same GPU/NVSwitch layout but different IB->NUMA
    # wiring must produce different fingerprints (IB VFs are passed through and
    # attach to PXB bridges by NUMA, so they move RTMR0).
    from chutes_cvm.guest.detection import host_topology_fingerprint

    profile = GPU_PROFILES["B200"]  # vcpus = 192-16 = 176; guest_mem = 1944 @ 2008G
    gpus = ["g"] * 8
    ib = ["i0", "i1", "i2", "i3"]
    with _patch_host_shape(
        cpus=192,
        sockets=2,
        mem_gb=2008,
        vendor="GenuineIntel",
        proc_id=None,
    ):
        with patch("chutes_cvm.guest.detection.detect_numa_node_count", return_value=2):
            with patch(
                "chutes_cvm.guest.detection._device_numa_layout",
                side_effect=[(0, 0, 0, 0, 1, 1, 1, 1), (), (0, 0, 1, 1)],
            ):
                fp = host_topology_fingerprint(profile, gpus, [], ib)
    assert fp == TopologyFingerprint(
        CpuTopology(**_B200_XEON_SHAPE),
        1944,
        NumaTopology(gpu_nodes=(0, 0, 0, 0, 1, 1, 1, 1), ib_nodes=(0, 0, 1, 1)),
    )


def test_topology_fingerprint_ib_count_on_flat_path():
    # On the flat path only device counts matter; IB count is the ib_count field.
    from chutes_cvm.guest.detection import host_topology_fingerprint

    profile = GPU_PROFILES["B200"]
    with _patch_host_shape(
        cpus=192,
        sockets=2,
        mem_gb=2008,
        vendor="GenuineIntel",
        proc_id=None,
    ):
        with patch("chutes_cvm.guest.detection.detect_numa_node_count", return_value=6):
            fp = host_topology_fingerprint(profile, ["g"] * 8, [], ["i"] * 4)
    assert fp == TopologyFingerprint(
        CpuTopology(**_B200_XEON_SHAPE), 1944, FlatTopology(gpu_count=8, ib_count=4)
    )


def test_detect_profile_has_no_local_topology_gate():
    # Acceptance moved to the control plane (chutes-cvm host verify): detect_profile
    # returns the (profile, fingerprint) even for a topology not in any in-repo set — it never
    # gates locally now. The fingerprint still drives the launch -smp / -m.
    from chutes_cvm.guest.detection import detect_profile

    fp = TopologyFingerprint(
        CpuTopology(**_B200_XEON_SHAPE),
        1944,
        NumaTopology(gpu_nodes=(0, 1, 0, 1, 0, 1, 0, 1)),
    )
    with _patch_detection(lspci_lines=_make_lspci_b200(), fingerprint=fp):
        profile, fingerprint = detect_profile()
        assert profile.name == "B200"
        assert fingerprint == fp


def test_detect_profile_skips_topology_check_for_unbaselined_profile():
    # No profile gates on a local topology set anymore (acceptance is the control plane's),
    # so an arbitrary B300 fingerprint must resolve the profile, not refuse the launch.
    from chutes_cvm.guest.detection import detect_profile

    b300_lines = [
        "0000:0d:00.0 3D controller [0302]: NVIDIA [B300] [10de:3182] (rev a1)"
    ]
    with _patch_detection(
        lspci_lines=b300_lines,
        gpu_bdfs=["0000:0d:00.0"],
        fingerprint=("anything", "goes"),
    ):
        assert detect_profile()[0] is GPU_PROFILES["B300"]


@pytest.mark.parametrize("key", ["RTX_PRO_6000", "H200"])
def test_pci_bars_vram_matches_bar_size_hint(key):
    # For profiles that model the GPU endpoint, the largest BAR (VRAM) must
    # equal bar_size_mb: the fw_cfg MMIO hint and the actual VRAM BAR describe
    # the same window and must not drift apart.
    bars = GPU_PROFILES[key].passthrough["gpu"].bars
    assert bars, f"{key} should model passthrough['gpu']"
    vram = max(bars, key=lambda b: b.size_mb)
    assert vram.size_mb == GPU_PROFILES[key].bar_size_mb


@pytest.mark.parametrize("key", ["RTX_PRO_6000", "H200"])
def test_pci_bars_are_well_formed(key):
    for bar in GPU_PROFILES[key].passthrough["gpu"].bars:
        assert 0 <= bar.index <= 5
        assert bar.kind in ("m32", "m64", "p32", "p64")
        # A 64-bit BAR consumes two slots, so it lands on an even index.
        if bar.kind.endswith("64"):
            assert bar.index % 2 == 0


def test_pci_bars_default_empty_when_uncaptured():
    # Profiles without an lspci capture yet model no GPU endpoint (offline
    # measurement generation is simply unavailable for them, not broken).
    assert "gpu" not in GPU_PROFILES["B300"].passthrough
