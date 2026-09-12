"""nvidia-gpu-tools availability check.

nvidia-gpu-tools is installed ONCE, at CLI-setup time, by the package's ``install.sh`` (the wheel
bundled in the package is pip-installed into the chutes-cvm venv and symlinked onto PATH). This
module only verifies it is present and runs — it does not install it lazily.
"""

from chutes_cvm import proc


def _cli_healthy() -> bool:
    """Return True if nvidia-gpu-tools is on PATH and actually executes.

    Presence on PATH is not sufficient: /usr/local/bin/nvidia-gpu-tools is a symlink into the
    chutes-cvm venv, whose interpreter is bound to one Python minor version. An OS upgrade that
    bumps the system Python leaves the symlink resolving but the wheel's modules unreachable, so
    the CLI raises ModuleNotFoundError. Verify it runs (``--help`` exits 0), not just ``which``.
    """
    which = proc.run(["which", "nvidia-gpu-tools"], capture_output=True)
    if which.returncode != 0:
        return False
    try:
        probe = proc.run(
            ["nvidia-gpu-tools", "--help"], capture_output=True, timeout=15
        )
    except (proc.TimeoutExpired, OSError):
        return False
    return probe.returncode == 0


def ensure_gpu_tools_available() -> str:
    """Return the ``nvidia-gpu-tools`` command if it is installed and runs, else raise.

    Installation happens at CLI-setup time (the package's install.sh installs the bundled wheel
    into the chutes-cvm venv and symlinks it on PATH). If it is missing or broken here, the CLI
    install is incomplete — re-run install.sh rather than installing on the fly.

    Raises:
        RuntimeError: if nvidia-gpu-tools is not on PATH or does not run.
    """
    if _cli_healthy():
        return "nvidia-gpu-tools"
    raise RuntimeError(
        "nvidia-gpu-tools is not available on PATH. It is installed by the package's install.sh "
        "(src/chutes-cvm/install.sh, or the curl one-liner) from the wheel bundled in the package. "
        "Re-run install.sh to install/repair it."
    )
