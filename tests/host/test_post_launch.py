"""Unit tests for post-launch QEMU vCPU thread pinning helpers."""

from unittest.mock import MagicMock, patch

from chutes_cvm.guest.post_launch import (
    apply_post_launch_tuning,
    expand_cpulist,
    find_qemu_pid,
)

# ---------------------------------------------------------------------------
# expand_cpulist
# ---------------------------------------------------------------------------


def test_expand_cpulist_single_range():
    assert expand_cpulist("0-3") == [1, 2, 3]


def test_expand_cpulist_multiple_ranges():
    assert expand_cpulist("0-1,4-5") == [1, 4, 5]


def test_expand_cpulist_excludes_cpu_zero():
    assert 0 not in expand_cpulist("0,2,4")


def test_expand_cpulist_single_cpus():
    assert expand_cpulist("1,3,5") == [1, 3, 5]


def test_expand_cpulist_empty_string():
    assert expand_cpulist("") == []


# ---------------------------------------------------------------------------
# find_qemu_pid
# ---------------------------------------------------------------------------


def test_find_qemu_pid_returns_pid_when_pidfile_and_proc_exist(tmp_path):
    pidfile = tmp_path / "td.pid"
    pidfile.write_text("12345\n")

    with patch("chutes_cvm.guest.post_launch.os.path.exists", return_value=True):
        pid = find_qemu_pid(pidfile=str(pidfile))

    assert pid == 12345


def test_find_qemu_pid_returns_none_when_pidfile_missing(tmp_path):
    assert find_qemu_pid(pidfile=str(tmp_path / "missing.pid")) is None


def test_find_qemu_pid_returns_none_when_process_gone(tmp_path):
    pidfile = tmp_path / "td.pid"
    pidfile.write_text("99999\n")

    with patch("chutes_cvm.guest.post_launch.os.path.exists", return_value=False):
        pid = find_qemu_pid(pidfile=str(pidfile))

    assert pid is None


def test_find_qemu_pid_returns_none_for_corrupt_pidfile(tmp_path):
    pidfile = tmp_path / "td.pid"
    pidfile.write_text("not-a-number\n")
    assert find_qemu_pid(pidfile=str(pidfile)) is None


# ---------------------------------------------------------------------------
# apply_post_launch_tuning — pin_threads gate
# ---------------------------------------------------------------------------


def test_apply_pins_threads_when_pid_found():
    pin = MagicMock()

    with (
        patch("chutes_cvm.guest.post_launch.find_qemu_pid", return_value=42),
        patch("chutes_cvm.guest.post_launch.pin_qemu_threads", pin),
        patch("chutes_cvm.guest.post_launch.time.sleep"),
    ):
        apply_post_launch_tuning(
            pidfile="/tmp/fake.pid",
            vcpus_total=16,
            host_nodes=[0, 1],
            pin_threads=True,
        )

    pin.assert_called_once_with(42, vcpus_total=16, host_nodes=[0, 1])


def test_apply_skips_pin_when_pin_threads_false():
    pin = MagicMock()

    with (
        patch("chutes_cvm.guest.post_launch.pin_qemu_threads", pin),
        patch("chutes_cvm.guest.post_launch.time.sleep"),
    ):
        apply_post_launch_tuning(
            pidfile="/tmp/fake.pid",
            vcpus_total=16,
            host_nodes=[0, 1],
            pin_threads=False,
        )

    pin.assert_not_called()


def test_apply_warns_when_pid_not_found(capsys):
    pin = MagicMock()

    with (
        patch("chutes_cvm.guest.post_launch.find_qemu_pid", return_value=None),
        patch("chutes_cvm.guest.post_launch.pin_qemu_threads", pin),
        patch("chutes_cvm.guest.post_launch.time.sleep"),
    ):
        apply_post_launch_tuning(
            pidfile="/tmp/fake.pid",
            vcpus_total=16,
            host_nodes=[0, 1],
            pin_threads=True,
        )

    pin.assert_not_called()
    assert "Warning" in capsys.readouterr().out
