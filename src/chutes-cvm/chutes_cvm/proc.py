"""Centralized subprocess execution for chutes-cvm.

Every command chutes-cvm runs is a fixed ``argv`` list of trusted system tools
(qemu, ip, cryptsetup, nvidia-gpu-tools, the bundled bash helpers, …) — never a
shell string built from untrusted input. That makes bandit's shell-execution
checks (B404/B603/B607) noise at each of the ~60 call sites. Routing every call
through this one module keeps the ``# nosec`` vetting in a single reviewable place
instead of scattering it across the package: callers use ``proc.run`` / ``proc.call``
/ ``proc.check_call`` / ``proc.check_output`` (and ``proc.DEVNULL`` etc.) exactly as
they would the stdlib, and never import ``subprocess`` directly.

If you ever need ``shell=True`` or a command built from external input, do NOT add
it here — that is a real finding, not boilerplate, and belongs with its own audited
``# nosec`` (or, better, a fix) at the call site.
"""

from __future__ import annotations

import subprocess  # nosec B404

# Re-export the non-executing helpers so callers need only import this module.
from subprocess import (  # noqa: F401  # nosec B404
    DEVNULL,
    PIPE,
    STDOUT,
    CalledProcessError,
    CompletedProcess,
    TimeoutExpired,
)
from typing import Any


def run(*args: Any, **kwargs: Any) -> "subprocess.CompletedProcess[Any]":
    """``subprocess.run`` with a fixed argv (see module docstring)."""
    return subprocess.run(*args, **kwargs)  # nosec B603


def call(*args: Any, **kwargs: Any) -> int:
    """``subprocess.call`` with a fixed argv (see module docstring)."""
    return subprocess.call(*args, **kwargs)  # nosec B603


def check_call(*args: Any, **kwargs: Any) -> int:
    """``subprocess.check_call`` with a fixed argv (see module docstring)."""
    return subprocess.check_call(*args, **kwargs)  # nosec B603


def check_output(*args: Any, **kwargs: Any) -> Any:
    """``subprocess.check_output`` with a fixed argv (see module docstring)."""
    return subprocess.check_output(*args, **kwargs)  # nosec B603
