"""Tests for the nvidia-gpu-tools availability check.

nvidia-gpu-tools is installed at CLI-setup time (install.sh installs the bundled wheel into the
chutes-cvm venv and symlinks it). This module only verifies it is present and runs.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from chutes_cvm.guest.gpu.tools import _cli_healthy, ensure_gpu_tools_available


def _completed(returncode):
    result = MagicMock()
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# _cli_healthy
# ---------------------------------------------------------------------------


@patch("chutes_cvm.guest.gpu.tools.proc.run")
def test_cli_healthy_false_when_not_on_path(mock_run):
    mock_run.return_value = _completed(1)  # `which` fails
    assert _cli_healthy() is False
    mock_run.assert_called_once()  # never probes --help when absent


@patch("chutes_cvm.guest.gpu.tools.proc.run")
def test_cli_healthy_false_when_cli_errors(mock_run):
    # On PATH, but --help fails — e.g. ModuleNotFoundError after a Python bump.
    mock_run.side_effect = [_completed(0), _completed(1)]
    assert _cli_healthy() is False
    assert mock_run.call_count == 2


@patch("chutes_cvm.guest.gpu.tools.proc.run")
def test_cli_healthy_true_when_help_succeeds(mock_run):
    mock_run.side_effect = [_completed(0), _completed(0)]
    assert _cli_healthy() is True


@patch("chutes_cvm.guest.gpu.tools.proc.run")
def test_cli_healthy_false_on_probe_timeout(mock_run):
    mock_run.side_effect = [
        _completed(0),
        subprocess.TimeoutExpired("nvidia-gpu-tools", 15),
    ]
    assert _cli_healthy() is False


# ---------------------------------------------------------------------------
# ensure_gpu_tools_available — health check only (no lazy install)
# ---------------------------------------------------------------------------


@patch("chutes_cvm.guest.gpu.tools._cli_healthy", return_value=True)
def test_ensure_returns_command_when_healthy(_healthy):
    assert ensure_gpu_tools_available() == "nvidia-gpu-tools"


@patch("chutes_cvm.guest.gpu.tools._cli_healthy", return_value=False)
def test_ensure_raises_when_missing(_healthy):
    # Missing = the CLI setup did not install it; it must not try to install on the fly.
    with pytest.raises(RuntimeError, match="not available on PATH"):
        ensure_gpu_tools_available()
