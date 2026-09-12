"""Tests for the Python launch orchestrator (chutes_cvm.guest.launch).

Covers the decision layer ported from quick-launch.sh — CLI override plumbing, derived defaults,
validation, the duplicate-VM guard, the TDX gate, and launch-vm argument assembly. Config
precedence itself (CLI > env > YAML > defaults) lives in the LaunchConfig model and is tested in
test_config.py. All privileged steps (volumes/network/boot) and host probes are mocked here.
"""

from unittest.mock import patch

import pytest
from chutes_cvm.guest import launch
from chutes_cvm.guest.config import LaunchConfig
from chutes_cvm.guest.launch import (
    LaunchError,
    _apply_derived_defaults,
    _boot,
    _build_parser,
    _resolve_config,
    _validate,
)

P = "chutes_cvm.guest.launch"


# Convenience: build a LaunchConfig from flat kwargs (mapped to the model's nested fields).
_FLAT_TO_PATH = {
    "hostname": "vm.hostname",
    "base_image": "vm.base_image",
    "vm_image_dir": "vm.vm_image_directory",
    "miner_ss58": "miner.ss58",
    "miner_seed": "miner.seed",
    "vm_ip": "network.vm_ip",
    "network_type": "network.type",
    "cache_volume": "volumes.cache.path",
    "storage_volume": "volumes.storage.path",
    "config_volume": "volumes.config.path",
    "foreground": "runtime.foreground",
}


def _cfg(**over) -> LaunchConfig:
    """A LaunchConfig with model defaults, overlaid with `over` given as flat kwargs."""
    config = LaunchConfig()
    for key, val in over.items():
        obj_path, _, field = _FLAT_TO_PATH[key].rpartition(".")
        obj = config
        for part in obj_path.split("."):
            obj = getattr(obj, part)
        setattr(obj, field, val)
    return config


# ── CLI override plumbing into the model ─────────────────────────────────────────


def test_resolve_config_applies_cli_overrides():
    args = _build_parser().parse_args(["--hostname", "h", "--skip-bind", "--no-gpus"])
    cfg, benchmark, pass_gpus, ephemeral = _resolve_config(args)
    assert cfg.vm.hostname == "h"
    assert cfg.devices.bind_devices is False  # --skip-bind → bind_devices False
    assert pass_gpus is False
    assert benchmark is False and ephemeral is False


def test_no_gpus_and_foreground_flags():
    args = _build_parser().parse_args(["--no-gpus", "--foreground", "--benchmark"])
    cfg, benchmark, pass_gpus, ephemeral = _resolve_config(args)
    assert pass_gpus is False
    assert cfg.runtime.foreground is True
    assert benchmark is True


def test_docker_creds_must_be_paired():
    args = _build_parser().parse_args(["--docker-hub-username", "u"])
    with pytest.raises(LaunchError, match="together"):
        _resolve_config(args)


# ── derived defaults ─────────────────────────────────────────────────────────────


def test_derived_volume_names_from_hostname():
    cfg = _cfg(hostname="box1")
    _apply_derived_defaults(cfg, benchmark=False, ephemeral=False)
    assert cfg.volumes.cache.path == "cache-box1.raw"
    assert cfg.volumes.storage.path == "storage-box1.raw"
    assert cfg.volumes.config.path == "config-box1.qcow2"
    assert cfg.vm.base_image.endswith("tdx-guest")
    assert cfg.vm.vm_image_directory == "/var/lib/chutes/vm-images"


def test_ephemeral_uses_tmp_image_dir():
    cfg = _cfg(hostname="b")
    _apply_derived_defaults(cfg, benchmark=False, ephemeral=True)
    assert cfg.vm.vm_image_directory == "/tmp/chutes-vm-images"


def test_benchmark_fills_placeholders_and_image():
    cfg = _cfg(hostname="b")
    _apply_derived_defaults(cfg, benchmark=True, ephemeral=False)
    assert cfg.vm.base_image.endswith("tdx-guest-benchmark")
    assert cfg.miner.ss58 == "benchmark"
    assert cfg.miner.seed == "benchmark"


# ── validation ───────────────────────────────────────────────────────────────────


def test_validate_requires_creds_in_standard_mode():
    with pytest.raises(LaunchError, match="miner.ss58"):
        _validate(_cfg(hostname="h"), benchmark=False)


def test_validate_requires_hostname():
    with pytest.raises(LaunchError, match="hostname"):
        _validate(_cfg(), benchmark=True)


def test_validate_benchmark_only_needs_hostname():
    _validate(_cfg(hostname="h"), benchmark=True)  # no creds required — must not raise


def test_validate_rejects_bad_network_type():
    cfg = _cfg(hostname="h", miner_ss58="x", miner_seed="y", network_type="bad")
    with pytest.raises(LaunchError, match="network type"):
        _validate(cfg, benchmark=False)


# ── launch-vm argument assembly ──────────────────────────────────────────────────


def test_boot_standard_args():
    cfg = _cfg(
        config_volume="c.qcow2",
        cache_volume="ca.raw",
        storage_volume="s.raw",
        network_type="tap",
        foreground=True,
    )
    with patch("chutes_cvm.guest.__main__.main", return_value=0) as lv:
        rc = _boot(cfg, "/img.qcow2", "tap0", benchmark=False, pass_gpus=True)
    assert rc == 0
    a = lv.call_args.args[0]
    assert a[:2] == ["--image", "/img.qcow2"]
    assert "--pass-gpus" in a
    assert a[a.index("--net-iface") + 1] == "tap0"
    assert "--cache-volume" in a and "--foreground" in a
    assert "--ssh" not in a


def test_boot_benchmark_omits_cache_adds_ssh():
    cfg = _cfg(config_volume="c", storage_volume="s", network_type="tap")
    with patch("chutes_cvm.guest.__main__.main", return_value=0) as lv:
        _boot(cfg, "/img", "tap0", benchmark=True, pass_gpus=False)
    a = lv.call_args.args[0]
    assert "--ssh" in a
    assert "--cache-volume" not in a
    assert "--pass-gpus" not in a


def test_boot_user_network_omits_net_iface():
    cfg = _cfg(
        config_volume="c", cache_volume="ca", storage_volume="s", network_type="user"
    )
    with patch("chutes_cvm.guest.__main__.main", return_value=0) as lv:
        _boot(cfg, "/img", "", benchmark=False, pass_gpus=True)
    assert "--net-iface" not in lv.call_args.args[0]


# ── main() orchestration (all steps + probes mocked) ─────────────────────────────

_STD_ARGV = [
    "--hostname",
    "h",
    "--miner-ss58",
    "x",
    "--miner-seed",
    "y",
    "--network-type",
    "user",
    "--no-gpus",
]


def _happy(**over):
    """ExitStack of patches for a passing host; `over` overrides individual return values."""
    from contextlib import ExitStack

    stack = ExitStack()
    defaults = {
        "_resolve_public_iface": "eth0",
        "_chutes_td_running": False,
        "_tdx_active": (True, "sysfs"),
        "_launchable": True,
        "_prepare_vm_image": "/var/lib/chutes/vm-images/img.qcow2",
    }
    defaults.update(over)
    for name, ret in defaults.items():
        stack.enter_context(patch(f"{P}.{name}", return_value=ret))
    for name in (
        "_ensure_numa_zone_reclaim",
        "_ensure_raw_volume",
        "_setup_config_volume",
    ):
        stack.enter_context(patch(f"{P}.{name}"))
    return stack


def test_main_happy_path_user_network():
    with _happy(), patch(f"{P}._boot", return_value=0) as boot:
        rc = launch.main(_STD_ARGV)
    assert rc == 0
    boot.assert_called_once()


def test_main_refuses_duplicate_without_force(capsys):
    with _happy(_chutes_td_running=True):
        rc = launch.main(_STD_ARGV)
    assert rc == 1
    assert "already running" in capsys.readouterr().err


def test_main_force_overrides_duplicate_guard():
    with _happy(_chutes_td_running=True), patch(f"{P}._boot", return_value=0) as boot:
        rc = launch.main(_STD_ARGV + ["--force"])
    assert rc == 0
    boot.assert_called_once()


def test_main_refuses_when_not_launchable():
    # No published measurement for this image x host class → stop before any GPU/volume work.
    with _happy(_launchable=False), patch(f"{P}._boot") as boot:
        rc = launch.main(_STD_ARGV)
    assert rc == 1
    boot.assert_not_called()


def test_main_benchmark_skips_launch_gate():
    # Benchmark VMs use dummy creds and aren't attested, so a failing gate must not block them.
    argv = ["--hostname", "h", "--benchmark", "--network-type", "user", "--no-gpus"]
    with _happy(_launchable=False), patch(f"{P}._boot", return_value=0) as boot:
        rc = launch.main(argv)
    assert rc == 0
    boot.assert_called_once()


def test_main_debug_image_is_still_gated():
    # A debug (RC) image is NOT special-cased: its rc:true measurement must be published just like
    # a production image's, so a non-launchable debug image is refused (no more blanket skip).
    argv = _STD_ARGV + ["--base-image", "/base/tdx-guest-debug"]
    with _happy(_launchable=False), patch(f"{P}._boot") as boot:
        rc = launch.main(argv)
    assert rc == 1
    boot.assert_not_called()


# ── the launch gate itself (mirrors host verify's API check) ─────────────────────


def test_launchable_true_when_measurement_covers(capsys):
    with patch(f"{P}.image_set.version_and_rc", return_value=("1.4.0", False)), patch(
        "chutes_cvm.guest.preflight.run_preflight",
        return_value={"launchable": True, "fingerprint": "abc", "detail": "covers"},
    ):
        assert launch._launchable("/cfg.yaml", "/base", force=False) is True
    assert "covers" in capsys.readouterr().out


def test_launchable_false_refuses_but_force_overrides(capsys):
    resp = {
        "launchable": False,
        "fingerprint": "abc",
        "detail": "no measurement for 1.4.0",
    }
    with patch(f"{P}.image_set.version_and_rc", return_value=("1.4.0", False)), patch(
        "chutes_cvm.guest.preflight.run_preflight", return_value=resp
    ):
        assert launch._launchable("/cfg.yaml", "/base", force=False) is False
        assert launch._launchable("/cfg.yaml", "/base", force=True) is True
    err = capsys.readouterr().err
    assert "Refusing to launch" in err
    assert "submit-profile" in err


def test_launchable_passes_image_version_rc_to_preflight():
    # The manifest's (version, rc) must be what's joined against — a debug image asks about rc:true.
    with patch(f"{P}.image_set.version_and_rc", return_value=("2.0.0", True)), patch(
        "chutes_cvm.guest.preflight.run_preflight",
        return_value={"launchable": True, "fingerprint": "abc", "detail": "ok"},
    ) as rp:
        assert launch._launchable("/cfg.yaml", "/base", force=False) is True
    assert rp.call_args.kwargs["version"] == "2.0.0"
    assert rp.call_args.kwargs["rc"] is True


def test_launchable_fails_closed_on_api_error():
    from chutes_cvm.guest.preflight import PreflightError

    with patch(f"{P}.image_set.version_and_rc", return_value=("1.4.0", False)), patch(
        "chutes_cvm.guest.preflight.run_preflight",
        side_effect=PreflightError("API unreachable"),
    ):
        assert launch._launchable("/cfg.yaml", "/base", force=False) is False
        assert launch._launchable("/cfg.yaml", "/base", force=True) is True


def test_launchable_blocks_on_unreadable_manifest(capsys):
    # Can't read the image version → can't know what we're booting → fail closed (force overrides).
    with patch(
        f"{P}.image_set.version_and_rc", side_effect=FileNotFoundError("no manifest")
    ):
        assert launch._launchable("/cfg.yaml", "/base", force=False) is False
        assert launch._launchable("/cfg.yaml", "/base", force=True) is True
    assert "image version" in capsys.readouterr().err


def test_main_blocks_when_tdx_inactive(capsys):
    with _happy(_tdx_active=(False, "")):
        rc = launch.main(_STD_ARGV)
    assert rc == 1
    assert "TDX" in capsys.readouterr().err


def test_main_missing_creds_is_error(capsys):
    with _happy():
        rc = launch.main(["--hostname", "h", "--network-type", "user"])
    assert rc == 1
    assert "miner.ss58" in capsys.readouterr().err


def _stage_image_set(tmp_path, sha):
    """A base image-set dir with a qcow2 + its 3 direct-boot sidecars; returns (set_dir, qcow2)."""
    base = tmp_path / "base"
    base.mkdir()
    qcow2 = base / "x.qcow2"
    qcow2.write_bytes(b"q")
    for ext in ("vmlinuz", "initrd", "cmdline"):
        (base / f"x.{ext}").write_bytes(b"s")
    return str(base), str(qcow2)


def test_prepare_vm_image_resolves_in_python_then_copies_via_sudo(tmp_path):
    """The image set is verified + resolved in Python (image_set.resolve); the privileged file
    mutations are done in-process as `sudo cp` (root-owned image dir), not shelled to a script
    that guessed a Python interpreter."""
    sha = "abc123def456abcd"  # 16 hex → [:16] is itself
    set_dir, qcow2 = _stage_image_set(tmp_path, sha)
    vm_dir = tmp_path / "vm-images"
    vm_dir.mkdir()
    calls: list[list[str]] = []

    with patch(f"{P}.image_set.resolve", return_value=(qcow2, sha)) as res, patch(
        f"{P}._run", side_effect=lambda cmd, **k: calls.append(cmd)
    ):
        out = launch._prepare_vm_image(set_dir, "h", str(vm_dir))

    res.assert_called_once_with(set_dir, full=False)
    vm_image = str(vm_dir / f"tdx-h-{sha}.qcow2")
    assert out == vm_image
    # qcow2 + 3 sidecars, each copied via `sudo cp`, into the per-VM name.
    cps = [c for c in calls if c[:2] == ["sudo", "cp"]]
    assert cps[0] == ["sudo", "cp", qcow2, vm_image]
    assert [c[-1] for c in cps[1:]] == [
        str(vm_dir / f"tdx-h-{sha}.{ext}") for ext in ("vmlinuz", "initrd", "cmdline")
    ]


def test_prepare_vm_image_reaps_stale_versions(tmp_path):
    sha = "newnewnewnewnew0"
    set_dir, qcow2 = _stage_image_set(tmp_path, sha)
    vm_dir = tmp_path / "vm"
    vm_dir.mkdir()
    stale = vm_dir / "tdx-h-oldoldoldoldold0.qcow2"  # a previous version's per-VM copy
    stale.write_bytes(b"old")
    calls: list[list[str]] = []

    with patch(f"{P}.image_set.resolve", return_value=(qcow2, sha)), patch(
        f"{P}._run", side_effect=lambda cmd, **k: calls.append(cmd)
    ):
        launch._prepare_vm_image(set_dir, "h", str(vm_dir))

    rms = [c for c in calls if c[:2] == ["sudo", "rm"]]
    assert len(rms) == 1
    # the stale qcow2 AND its sidecars are removed
    assert str(stale) in rms[0]
    assert str(vm_dir / "tdx-h-oldoldoldoldold0.vmlinuz") in rms[0]


def test_prepare_vm_image_missing_sidecar_raises(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    qcow2 = base / "x.qcow2"
    qcow2.write_bytes(b"q")  # no sidecars staged
    vm_dir = tmp_path / "vm"
    vm_dir.mkdir()
    with patch(f"{P}.image_set.resolve", return_value=(str(qcow2), "abc123def456abcd")):
        with patch(f"{P}._run"):
            with pytest.raises(LaunchError, match="direct-boot artifact missing"):
                launch._prepare_vm_image(str(base), "h", str(vm_dir))


def test_prepare_vm_image_surfaces_verification_failure():
    with patch(f"{P}.image_set.resolve", side_effect=ValueError("manifest mismatch")):
        with pytest.raises(LaunchError, match="image set verification failed"):
            launch._prepare_vm_image("/base/set", "h", "/vm")
