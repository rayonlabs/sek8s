"""Tests for VFIO PCI cleanup and binding helpers."""

from unittest.mock import patch

import pytest
from chutes_cvm.vfio import (
    _get_bound_driver,
    bind_explicit_devices_to_vfio,
    has_stale_vfio_devices,
    pci_operations_wedged,
    unbind_stale_vfio_devices,
    wait_pci_operations_idle,
)

# ---------------------------------------------------------------------------
# _get_bound_driver
# ---------------------------------------------------------------------------


@patch("os.path.realpath", return_value="/sys/bus/pci/drivers/vfio-pci")
@patch("os.path.islink", return_value=True)
def test_get_bound_driver_returns_driver_name(mock_islink, mock_realpath):
    assert _get_bound_driver("0000:b8:00.0") == "vfio-pci"
    mock_islink.assert_called_once_with("/sys/bus/pci/devices/0000:b8:00.0/driver")


@patch("os.path.islink", return_value=False)
def test_get_bound_driver_returns_none_when_unbound(mock_islink):
    assert _get_bound_driver("0000:b8:00.0") is None


# ---------------------------------------------------------------------------
# has_stale_vfio_devices
# ---------------------------------------------------------------------------


@patch("chutes_cvm.vfio._get_bound_driver")
def test_has_stale_vfio_devices_true(mock_driver):
    mock_driver.side_effect = [None, "vfio-pci", None]
    assert has_stale_vfio_devices(["0000:a:00.0", "0000:b:00.0", "0000:c:00.0"])


@patch("chutes_cvm.vfio._get_bound_driver", return_value=None)
def test_has_stale_vfio_devices_false(mock_driver):
    assert not has_stale_vfio_devices(["0000:a:00.0", "0000:b:00.0"])


# ---------------------------------------------------------------------------
# unbind_stale_vfio_devices
# ---------------------------------------------------------------------------


@patch("chutes_cvm.vfio._sysfs_write")
@patch("chutes_cvm.vfio._get_bound_driver", return_value="nvidia")
def test_unbind_noop_when_not_vfio(mock_driver, mock_write):
    """Devices not on vfio-pci should not be unbound."""
    unbind_stale_vfio_devices(["0000:b8:00.0"])
    mock_write.assert_not_called()


@patch("chutes_cvm.vfio._sysfs_write", return_value=True)
@patch("chutes_cvm.vfio._get_bound_driver", return_value="vfio-pci")
def test_unbind_writes_unbind_and_clears_override(mock_driver, mock_write):
    """Devices on vfio-pci should be unbound and have driver_override cleared."""
    unbind_stale_vfio_devices(["0000:b8:00.0"])

    calls = [(c[0][0], c[0][1]) for c in mock_write.call_args_list]
    assert ("/sys/bus/pci/drivers/vfio-pci/unbind", "0000:b8:00.0") in calls
    assert ("/sys/bus/pci/devices/0000:b8:00.0/driver_override", "") in calls


@patch("chutes_cvm.vfio._sysfs_write", return_value=True)
@patch("chutes_cvm.vfio._get_bound_driver")
def test_unbind_handles_multiple_devices(mock_driver, mock_write):
    """Multiple vfio-pci devices should all be unbound; non-vfio skipped."""
    mock_driver.side_effect = ["vfio-pci", "vfio-pci", None]

    devices = ["0000:b8:00.0", "0000:b9:00.0", "0000:ba:00.0"]
    unbind_stale_vfio_devices(devices)

    written = [(c[0][0], c[0][1]) for c in mock_write.call_args_list]
    assert ("/sys/bus/pci/drivers/vfio-pci/unbind", "0000:b8:00.0") in written
    assert ("/sys/bus/pci/drivers/vfio-pci/unbind", "0000:b9:00.0") in written
    assert ("/sys/bus/pci/devices/0000:b8:00.0/driver_override", "") in written
    assert ("/sys/bus/pci/devices/0000:b9:00.0/driver_override", "") in written
    assert all("0000:ba:00.0" not in str(c) for c in written)


@patch("chutes_cvm.vfio._sysfs_write", return_value=False)
@patch("chutes_cvm.vfio._get_bound_driver", return_value="vfio-pci")
def test_unbind_warns_on_timeout(mock_driver, mock_write, capsys):
    """Timed-out unbind should warn, not raise, and not clear override."""
    failed = unbind_stale_vfio_devices(["0000:b8:00.0"])

    captured = capsys.readouterr()
    assert "timed out" in captured.out
    assert mock_write.call_count == 1
    assert failed == 1


# ---------------------------------------------------------------------------
# bind_explicit_devices_to_vfio — verify each device actually bound
# ---------------------------------------------------------------------------


@patch("chutes_cvm.vfio.load_vfio_modules")
@patch("chutes_cvm.vfio.bind_device_to_vfio")
@patch("chutes_cvm.vfio._is_vfio_bound", return_value=True)
def test_bind_prints_success_when_all_bound(mock_bound, mock_bind, mock_load, capsys):
    bind_explicit_devices_to_vfio(["0000:dc:00.0", "0000:dd:00.0"])
    out = capsys.readouterr().out
    assert "0000:dc:00.0 → vfio-pci" in out
    assert "0000:dd:00.0 → vfio-pci" in out


@patch("chutes_cvm.vfio.load_vfio_modules")
@patch("chutes_cvm.vfio.bind_device_to_vfio")
@patch("chutes_cvm.vfio.time.sleep")
@patch("chutes_cvm.vfio._is_vfio_bound")
def test_bind_succeeds_after_retry(
    mock_bound, mock_sleep, mock_bind, mock_load, capsys
):
    # Not bound on the first check (still settling after reset), bound on retry.
    mock_bound.side_effect = [False, True, True]
    bind_explicit_devices_to_vfio(["0000:dc:00.0"])
    assert "0000:dc:00.0 → vfio-pci" in capsys.readouterr().out


@patch("chutes_cvm.vfio.load_vfio_modules")
@patch("chutes_cvm.vfio.bind_device_to_vfio")
@patch("chutes_cvm.vfio._is_vfio_bound", return_value=False)
@patch("chutes_cvm.vfio._get_bound_driver", return_value="nvidia")
@patch("chutes_cvm.vfio.time.sleep")
@patch("chutes_cvm.vfio.time.time", side_effect=[0, 0, 100])
def test_bind_raises_when_device_never_binds(
    mock_time, mock_sleep, mock_driver, mock_bound, mock_bind, mock_load
):
    # A device that never lands on vfio-pci must abort loudly (with its driver),
    # not sail into a cryptic QEMU "couldn't open .../vfio-dev" failure.
    with pytest.raises(
        RuntimeError, match=r"Failed to bind.*0000:dc:00.0.*driver=nvidia"
    ):
        bind_explicit_devices_to_vfio(["0000:dc:00.0"])


# ---------------------------------------------------------------------------
# pci_operations_wedged
# ---------------------------------------------------------------------------


@patch(
    "subprocess.run",
    return_value=type(
        "R",
        (),
        {
            "returncode": 0,
            "stdout": "D     bash -c echo 0000:12:00.0 > /sys/bus/pci/drivers/vfio-pci/unbind\n",
        },
    )(),
)
def test_pci_operations_wedged_detects_vfio_unbind(mock_run):
    assert pci_operations_wedged()


@patch(
    "subprocess.run",
    return_value=type(
        "R",
        (),
        {
            "returncode": 0,
            "stdout": "D     /usr/local/bin/nvidia-gpu-tools --set-cc-mode=on\n",
        },
    )(),
)
def test_pci_operations_wedged_detects_gpu_tools(mock_run):
    assert pci_operations_wedged()


@patch(
    "subprocess.run",
    return_value=type(
        "R",
        (),
        {"returncode": 0, "stdout": "S     sleep 1\n"},
    )(),
)
def test_pci_operations_wedged_false_when_no_d_state_pci_tasks(mock_run):
    assert not pci_operations_wedged()


@patch("chutes_cvm.vfio.pci_operations_wedged", side_effect=[True, True, False])
@patch("chutes_cvm.vfio.time.sleep")
def test_wait_pci_operations_idle_returns_when_tasks_clear(mock_sleep, mock_wedged):
    assert wait_pci_operations_idle(timeout_secs=10)


@patch("chutes_cvm.vfio.pci_operations_wedged", return_value=True)
@patch("chutes_cvm.vfio.time.sleep")
@patch("chutes_cvm.vfio.time.time", side_effect=[0, 0, 100])
def test_wait_pci_operations_idle_times_out(mock_time, mock_sleep, mock_wedged):
    assert not wait_pci_operations_idle(timeout_secs=10)
