"""Unit tests for standalone host CPU tuning (chutes_cvm.host.tune).

Verifies alignment with NVIDIA's CC Deployment Guide guidance: governor ->
performance, and only the C1E/C6 C-states disabled (POLL/C1 left enabled, and
no turbo/EPP writes).
"""

from unittest.mock import MagicMock, patch

from chutes_cvm.host import tune

_GOV = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
_STATES = {
    "/sys/devices/system/cpu/cpu0/cpuidle/state0": "POLL",
    "/sys/devices/system/cpu/cpu0/cpuidle/state1": "C1",
    "/sys/devices/system/cpu/cpu0/cpuidle/state2": "C1E",
    "/sys/devices/system/cpu/cpu0/cpuidle/state3": "C6",
}


def _glob_side_effect(pattern):
    if "scaling_governor" in pattern:
        return [_GOV]
    if "cpuidle/state*" in pattern:
        return list(_STATES)
    return []


def _read_side_effect(original_saved="powersave"):
    def _read(path):
        if path.endswith("/name"):
            return _STATES.get(path[: -len("/name")])
        if "scaling_governor" in path:
            return original_saved
        if path.endswith("/disable"):
            return "0"
        return None

    return _read


# ---------------------------------------------------------------------------
# apply_tuning — first call snapshots and applies
# ---------------------------------------------------------------------------


def test_apply_sets_governor_performance(tmp_path):
    write_root = MagicMock()
    with (
        patch("chutes_cvm.host.tune.RESTORE_SCRIPT", str(tmp_path / "restore.sh")),
        patch("chutes_cvm.host.tune.glob.glob", side_effect=_glob_side_effect),
        patch("chutes_cvm.host.tune._read", side_effect=_read_side_effect()),
        patch("chutes_cvm.host.tune._write_root", write_root),
    ):
        tune.apply_tuning()

    assert (_GOV, "performance") in [c.args for c in write_root.call_args_list]


def test_apply_disables_only_c1e_and_c6(tmp_path):
    write_root = MagicMock()
    with (
        patch("chutes_cvm.host.tune.RESTORE_SCRIPT", str(tmp_path / "restore.sh")),
        patch("chutes_cvm.host.tune.glob.glob", side_effect=_glob_side_effect),
        patch("chutes_cvm.host.tune._read", side_effect=_read_side_effect()),
        patch("chutes_cvm.host.tune._write_root", write_root),
    ):
        tune.apply_tuning()

    disabled = [
        c.args[0] for c in write_root.call_args_list if c.args[0].endswith("/disable")
    ]
    assert disabled == [
        "/sys/devices/system/cpu/cpu0/cpuidle/state2/disable",  # C1E
        "/sys/devices/system/cpu/cpu0/cpuidle/state3/disable",  # C6
    ]
    # POLL (state0) and C1 (state1) must NOT be disabled.
    assert all("state0" not in d and "state1" not in d for d in disabled)


def test_apply_does_not_touch_turbo_or_epp(tmp_path):
    write_root = MagicMock()
    with (
        patch("chutes_cvm.host.tune.RESTORE_SCRIPT", str(tmp_path / "restore.sh")),
        patch("chutes_cvm.host.tune.glob.glob", side_effect=_glob_side_effect),
        patch("chutes_cvm.host.tune._read", side_effect=_read_side_effect()),
        patch("chutes_cvm.host.tune._write_root", write_root),
    ):
        tune.apply_tuning()

    written = [c.args[0] for c in write_root.call_args_list]
    assert not any("no_turbo" in p for p in written)
    assert not any("energy_performance_preference" in p for p in written)


def test_apply_writes_restore_snapshot(tmp_path):
    restore_path = tmp_path / "restore.sh"
    with (
        patch("chutes_cvm.host.tune.RESTORE_SCRIPT", str(restore_path)),
        patch("chutes_cvm.host.tune.glob.glob", side_effect=_glob_side_effect),
        patch("chutes_cvm.host.tune._read", side_effect=_read_side_effect("powersave")),
        patch("chutes_cvm.host.tune._write_root"),
    ):
        tune.apply_tuning()

    content = restore_path.read_text()
    assert "powersave" in content  # original governor captured
    assert "state2/disable" in content and "state3/disable" in content
    # Shallow states are never snapshotted because they are never changed.
    assert "state0/disable" not in content and "state1/disable" not in content


# ---------------------------------------------------------------------------
# apply_tuning — idempotency: existing snapshot is preserved
# ---------------------------------------------------------------------------


def test_apply_second_call_preserves_original_snapshot(tmp_path):
    restore_path = tmp_path / "restore.sh"
    restore_path.write_text("ORIGINAL SNAPSHOT\n")
    write_root = MagicMock()

    with (
        patch("chutes_cvm.host.tune.RESTORE_SCRIPT", str(restore_path)),
        patch("chutes_cvm.host.tune.glob.glob", side_effect=_glob_side_effect),
        # sysfs now reads "performance" (already tuned)
        patch(
            "chutes_cvm.host.tune._read", side_effect=_read_side_effect("performance")
        ),
        patch("chutes_cvm.host.tune._write_root", write_root),
    ):
        tune.apply_tuning()

    # Snapshot untouched; settings still reapplied.
    assert restore_path.read_text() == "ORIGINAL SNAPSHOT\n"
    assert (_GOV, "performance") in [c.args for c in write_root.call_args_list]


# ---------------------------------------------------------------------------
# restore_tuning
# ---------------------------------------------------------------------------


def test_restore_runs_script_when_present(tmp_path):
    restore_path = str(tmp_path / "restore.sh")
    run = MagicMock(return_value=MagicMock(returncode=0))
    with (
        patch("chutes_cvm.host.tune.RESTORE_SCRIPT", restore_path),
        patch("chutes_cvm.host.tune.os.path.isfile", return_value=True),
        patch("chutes_cvm.host.tune.proc.run", run),
    ):
        tune.restore_tuning()

    run.assert_called_once_with([restore_path], check=False)


def test_restore_is_noop_when_script_missing(tmp_path):
    run = MagicMock()
    with (
        patch("chutes_cvm.host.tune.RESTORE_SCRIPT", str(tmp_path / "missing.sh")),
        patch("chutes_cvm.host.tune.os.path.isfile", return_value=False),
        patch("chutes_cvm.host.tune.proc.run", run),
    ):
        tune.restore_tuning()

    run.assert_not_called()


def test_restore_warns_on_nonzero_exit(tmp_path, capsys):
    restore_path = str(tmp_path / "restore.sh")
    run = MagicMock(return_value=MagicMock(returncode=3))
    with (
        patch("chutes_cvm.host.tune.RESTORE_SCRIPT", restore_path),
        patch("chutes_cvm.host.tune.os.path.isfile", return_value=True),
        patch("chutes_cvm.host.tune.proc.run", run),
    ):
        tune.restore_tuning()

    assert "Warning" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main dispatch
# ---------------------------------------------------------------------------


def test_main_apply_dispatches(monkeypatch):
    apply_mock = MagicMock()
    monkeypatch.setattr("chutes_cvm.host.tune.apply_tuning", apply_mock)
    monkeypatch.setattr("sys.argv", ["tune", "apply"])
    assert tune.main() == 0
    apply_mock.assert_called_once()


def test_main_restore_dispatches(monkeypatch):
    restore_mock = MagicMock()
    monkeypatch.setattr("chutes_cvm.host.tune.restore_tuning", restore_mock)
    monkeypatch.setattr("sys.argv", ["tune", "restore"])
    assert tune.main() == 0
    restore_mock.assert_called_once()
