"""Tests for the `chutes-cvm guest <verb>` dispatcher (chutes_cvm.guest.cli).

The guest noun groups the TDX VM runtime lifecycle: launch (forwarded to the Python
orchestrator), stop, and down. Both stop and down shut the guest off gracefully via the
system-manager API by default (--force force-kills QEMU); down additionally tears down the
bridge/benchmark-netlog. GPU/PCI hardware ops (reset-gpus / vfio-wedged) live under `host` —
see test_host_cli.py.
"""

from unittest.mock import patch

from chutes_cvm.guest import cli as guestcli


def test_unknown_verb_is_a_usage_error(capsys):
    # An unregistered verb is rejected by argparse (exit 2), confirming the verb set is closed.
    try:
        guestcli.main(["frobnicate"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - argparse always exits on an invalid choice
        raise AssertionError("expected a usage error for an unknown verb")


def test_launch_forwards_to_python_orchestrator():
    # `guest launch` is the Python orchestrator (chutes_cvm.guest.launch), forwarded verbatim.
    with patch("chutes_cvm.guest.launch.main", return_value=0) as orch:
        assert guestcli.main(["launch", "config.yaml", "--benchmark"]) == 0
    assert orch.call_args.args[0] == ["config.yaml", "--benchmark"]


def test_stop_is_graceful_by_default():
    # `guest stop` asks the guest to power off via the API — no force-kill, no bridge teardown.
    with patch(
        "chutes_cvm.guest.shutdown.graceful_shutdown", return_value="192.168.100.2"
    ) as graceful, patch("chutes_cvm.guest.__main__.stop_existing_vm") as kill, patch(
        "chutes_cvm.guest.cli._run_script", return_value=0
    ) as run:
        assert guestcli.main(["stop", "--config", "/nope/config.yaml"]) == 0
    graceful.assert_called_once()
    kill.assert_not_called()
    run.assert_not_called()  # stop leaves the bridge/volumes in place


def test_stop_force_kills_without_teardown():
    with patch("chutes_cvm.guest.__main__.stop_existing_vm") as kill, patch(
        "chutes_cvm.guest.cli._run_script", return_value=0
    ) as run:
        assert guestcli.main(["stop", "--force"]) == 0
    kill.assert_called_once_with()
    run.assert_not_called()  # still no teardown — that is `down`'s job


def test_stop_graceful_failure_suggests_force(capsys):
    from chutes_cvm.guest.shutdown import ShutdownError

    with patch(
        "chutes_cvm.guest.shutdown.graceful_shutdown",
        side_effect=ShutdownError("unreachable"),
    ):
        assert guestcli.main(["stop", "--config", "/nope/config.yaml"]) == 1
    assert "--force" in capsys.readouterr().err


def test_down_force_kills_and_tears_down():
    with patch("chutes_cvm.guest.cli._run_script", return_value=0) as run:
        assert guestcli.main(["down", "--force", "--config", "/nope/config.yaml"]) == 0
    # --force goes straight to teardown (force-kill), no --no-stop.
    assert run.call_args.args[0] == "teardown.sh"
    assert "--no-stop" not in run.call_args.args[1]
    assert run.call_args.kwargs["cwd"] == str(guestcli.SCRIPTS_DIR)


def test_down_graceful_then_teardown_no_stop():
    with patch(
        "chutes_cvm.guest.shutdown.graceful_shutdown", return_value="192.168.100.2"
    ), patch("chutes_cvm.guest.cli._run_script", return_value=0) as run:
        assert guestcli.main(["down", "--config", "/nope/config.yaml"]) == 0
    # Graceful path tells teardown NOT to force-kill (the guest is powering off itself).
    assert run.call_args.args[0] == "teardown.sh"
    assert "--no-stop" in run.call_args.args[1]


def test_down_graceful_failure_suggests_force(capsys):
    from chutes_cvm.guest.shutdown import ShutdownError

    with patch(
        "chutes_cvm.guest.shutdown.graceful_shutdown",
        side_effect=ShutdownError("unreachable"),
    ), patch("chutes_cvm.guest.cli._run_script", return_value=0) as run:
        assert guestcli.main(["down", "--config", "/nope/config.yaml"]) == 1
    run.assert_not_called()  # no teardown when graceful fails
    err = capsys.readouterr().err
    assert "--force" in err


def test_hardware_verbs_are_not_guest_commands():
    # reset-gpus / vfio-wedged moved to `host`; they must not be accepted under `guest`.
    for verb in ("reset-gpus", "vfio-wedged"):
        try:
            guestcli.main([verb])
        except SystemExit as exc:
            assert exc.code == 2
        else:  # pragma: no cover - argparse always exits on an invalid choice
            raise AssertionError(f"{verb} should no longer be a guest verb")
