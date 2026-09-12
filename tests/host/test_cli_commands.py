"""Tests for the top-level chutes-cvm CLI dispatcher (chutes_cvm.cli).

Covers the noun-group command surface: every operator command is a noun (guest / host /
image / config / measurements) whose args are forwarded verbatim to that subpackage's main.
The low-level QEMU boot primitive (chutes_cvm.guest.__main__) is not a CLI command — the
operator VM lifecycle lives under the `guest` noun (see test_guest_cli.py for its verbs).
"""

from unittest.mock import patch

import pytest
from chutes_cvm import cli


def _visible_commands():
    parser = cli.build_parser()
    # The subparsers action holds the registered command choices.
    subactions = [
        a
        for a in parser._actions
        if getattr(a, "choices", None) and "guest" in a.choices
    ]
    return set(subactions[0].choices)


def test_visible_command_surface():
    cmds = _visible_commands()
    # The top level is all nouns.
    for expected in ("guest", "host", "image", "config", "measurements"):
        assert expected in cmds
    # The VM lifecycle verbs live under `guest`; the hardware ops under `host`. None are top-level.
    for gone in ("launch", "stop", "down", "reset-gpus", "vfio-wedged", "up"):
        assert gone not in cmds
    # host lifecycle commands are `host <verb>`, not top-level.
    for gone in (
        "verify-host",
        "setup-host",
        "tune-host",
        "restore-host",
        "discover-profile",
        "preflight",
        "init",
    ):
        assert gone not in cmds
    # The boot primitive is not a CLI command at all (no `launch-vm` verb).
    assert "launch-vm" not in cmds


def test_guest_dispatches_to_guest_cli():
    with patch("chutes_cvm.guest.cli.main", return_value=0) as g:
        assert cli.main(["guest", "down", "--force"]) == 0
    assert g.call_args.args[0] == ["down", "--force"]


def test_host_dispatches_to_host_cli():
    with patch("chutes_cvm.host.cli.main", return_value=0) as h:
        assert cli.main(["host", "verify", "--target-os", "26.04"]) == 0
    assert h.call_args.args[0] == ["verify", "--target-os", "26.04"]


def test_no_launch_vm_command():
    # `launch-vm` was removed with prime-vm; it is not a passthrough and not dispatched.
    assert "launch-vm" not in cli._PASSTHROUGH


def test_measurements_dispatches_to_engine():
    with patch(
        "chutes_cvm.measurement.generate_measurements.main", return_value=0
    ) as gen:
        assert cli.main(["measurements", "list", "--qemu", "10.2.1"]) == 0
    assert gen.call_args.args[0] == ["list", "--qemu", "10.2.1"]


def test_image_dispatches_to_engine():
    # `chutes-cvm image <verb>` forwards verbatim to the image_set module's main.
    with patch("chutes_cvm.guest.image_set.main", return_value=0) as img:
        assert cli.main(["image", "verify", "/some/dir"]) == 0
    assert img.call_args.args[0] == ["verify", "/some/dir"]


def test_image_download_selects_production_by_default():
    from chutes_cvm.guest import image_set

    with patch("chutes_cvm.guest.image_set.proc.call", return_value=0) as call:
        assert image_set.main(["download"]) == 0
    # download-image-set.sh is invoked with the production variant.
    assert call.call_args.args[0][-1] == "tdx-guest"


def test_image_download_debug_flag_selects_debug_set():
    from chutes_cvm.guest import image_set

    with patch("chutes_cvm.guest.image_set.proc.call", return_value=0) as call:
        assert image_set.main(["download", "--debug"]) == 0
    assert call.call_args.args[0][-1] == "tdx-guest-debug"


def test_config_init_writes_config_and_guards_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config", "init"]) == 0
    dest = tmp_path / "config.yaml"
    assert dest.exists() and dest.read_text().strip()

    # A second `config init` refuses (non-zero) rather than clobbering an edited config.
    assert cli.main(["config", "init"]) == 1

    # --force overwrites.
    dest.write_text("stale")
    assert cli.main(["config", "init", "--force"]) == 0
    assert dest.read_text() != "stale"


def test_config_init_generates_valid_config_from_schema(tmp_path, monkeypatch):
    # `config init` generates the config from the LaunchConfig model; it must load back cleanly.
    from chutes_cvm.guest.config import LaunchConfig

    monkeypatch.chdir(tmp_path)
    assert cli.main(["config", "init"]) == 0
    cfg = LaunchConfig.from_file(str(tmp_path / "config.yaml"))
    assert cfg.network.type == "tap"


def test_version_command(capsys):
    # `chutes-cvm version` prints the version plus where it resolves from (drift detection).
    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("chutes-cvm ")
    assert "package:" in out and "python:" in out


def test_version_flag_exits_zero(capsys):
    # `chutes-cvm --version` is the argparse action: prints and exits 0.
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("chutes-cvm ")


def test_version_is_a_visible_command():
    assert "version" in _visible_commands()
