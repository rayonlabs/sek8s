"""Where chutes-cvm finds its bundled data and the one external artifact it needs.

* **Bundled** (ship with the package under ``chutes_cvm/scripts/``): the VM-management
  shell scripts, the config JSON schema + template, the vfio udev rules, and the
  nvidia-gpu-tools wheel. Resolved package-relative, so they travel with a ``pip install``
  — no checkout needed.
* **External**: the guest firmware (OVMF). It is a large, MRTD-measured, image-bound
  artifact, so it is NOT shipped in this (host-side) package. Resolved from an env override,
  else the checkout copy as a fast-path (see ``firmware_dir``). Nothing in the package
  imports from the repo, and only ``firmware_dir`` consults the checkout layout at all.
"""

import os
from pathlib import Path

# chutes_cvm/ — bundled data lives under here.
PACKAGE_DIR = Path(__file__).resolve().parent
# VM-management shell scripts + config schema/template + udev rules + gpu-tools wheel.
SCRIPTS_DIR = PACKAGE_DIR / "scripts"

# The checkout root — consulted ONLY by firmware_dir() as a checkout fast-path (the firmware
# is image-bound and not shipped in this package). Nothing else here uses it.
_REPO_ROOT = PACKAGE_DIR.parents[2]


def firmware_dir() -> Path:
    """The guest firmware (OVMF) directory.

    ``CHUTES_CVM_FIRMWARE_DIR`` wins; otherwise the checkout copy (``<repo>/firmware``) as a
    fast-path for a repo-present (editable) install. The firmware is MRTD-measured and not
    bundled in this host-side package; a standalone (non-editable) install copies it out of the
    checkout and sets the env in the shim, so no repo or R2 is needed at runtime."""
    return Path(os.environ.get("CHUTES_CVM_FIRMWARE_DIR") or (_REPO_ROOT / "firmware"))


def gpu_tools_dir() -> Path:
    """The bundled nvidia-gpu-tools wheel directory (ships with the package)."""
    return SCRIPTS_DIR / "gpu-tools"


def tdx_measure_script() -> Path:
    """The bundled ``tdx-measure``: the single implementation of which files RTMR3
    measures, in what order, and how each is hashed.

    The identical file is installed into the guest at ``/usr/local/bin/tdx-measure`` by
    the rtmr3-measure Ansible role, and is run there by the initramfs measurer, the
    build-time manifest generator and ``rtmr3-verify``. Predicting a measurement on the
    host must run the same script rather than reimplement it — four independent walkers
    of ``tdx-measure.conf`` used to exist and they disagreed four ways."""
    return SCRIPTS_DIR / "tdx-measure"


def default_config_path() -> str:
    """The launch config.yaml when a caller passes none. ``CHUTES_CVM_CONFIG`` override, else
    ``./config.yaml`` in the current directory — where ``chutes-cvm config init`` writes it. Ansible
    and the launch orchestrator pass an explicit path instead of relying on this."""
    return os.environ.get("CHUTES_CVM_CONFIG") or "config.yaml"


# Chutes control-plane base URL. Lives here (not in the substrate-heavy preflight module) so the
# CLI can wire it as an argparse default without eagerly importing preflight.
DEFAULT_API_BASE = "https://api.chutes.ai"


def default_api_base() -> str:
    """The control-plane base URL when a caller passes none: ``CHUTES_API_BASE`` override, else
    the production default. Lets `--api` default from the environment so miners need no flag.
    """
    return os.environ.get("CHUTES_API_BASE") or DEFAULT_API_BASE
