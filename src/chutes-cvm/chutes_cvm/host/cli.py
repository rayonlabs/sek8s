"""``chutes-cvm host <verb>`` — host lifecycle, attestation, and GPU/PCI hardware.

Groups the former setup-host / verify-host / tune-host / restore-host commands (and the new
submit-profile) under one noun, matching the CLI's noun/verb pattern. Dispatched via the
top-level ``host`` passthrough in ``chutes_cvm.cli``.

  chutes-cvm host setup            # provision this TDX host (args forwarded to host.setup)
  chutes-cvm host verify           # will this host relaunch + re-attest? (optionally --target-os)
  chutes-cvm host submit-profile   # register this host class with Chutes for baselining
                                   #   (optionally --target-os, as `host verify`)
  chutes-cvm host tune / restore   # NVIDIA host CPU tuning, and revert
  chutes-cvm host reset-gpus       # reset all GPUs via nvidia-gpu-tools SBR
  chutes-cvm host vfio-wedged      # exit 0 if host PCI passthrough is wedged and needs a reset

reset-gpus / vfio-wedged act on host hardware (GPUs, the PCI subsystem) and are useful with or
without a running guest, so they live under ``host``, not ``guest``.
"""

from __future__ import annotations

import argparse
import os
import sys

from chutes_cvm import proc
from chutes_cvm.paths import SCRIPTS_DIR, default_api_base, default_config_path


def _run_script(name: str, argv: "list[str]", cwd: "str | None" = None) -> int:
    """Exec a bundled chutes_cvm/scripts/<name> shell entrypoint, forwarding argv."""
    script = SCRIPTS_DIR / name
    if not script.exists():
        print(f"chutes-cvm: {name} not found at {script}", file=sys.stderr)
        return 1
    return proc.call(["bash", str(script), *argv], cwd=cwd)


# verify/submit exit codes → (banner label, ANSI attributes).
_VERIFY_STATUS = {
    0: ("READY", "1;32"),  # bold green
    1: ("BLOCKED", "1;31"),  # bold red
    2: ("WARNING", "1;33"),  # bold yellow
}


def _color(text: str, attrs: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{attrs}m{text}\033[0m"


def _run_verify(target_os, config, api, *, submit: bool, banner: str) -> int:
    """Run the host gates (via chutes_cvm.guest.verify) and print a colored result banner."""
    from chutes_cvm.guest.verify import verify_host

    print(_color(f"── chutes-cvm: {banner} ──", "1;36"))
    rc = verify_host(
        target_os=target_os,
        scripts_dir=str(SCRIPTS_DIR),
        config_path=config,
        api_base=api,
        submit=submit,
    )
    label, attrs = _VERIFY_STATUS.get(rc, (f"EXIT {rc}", "1"))
    print(_color(f"\nResult: {label}", attrs))
    return rc


def _cmd_verify(args: argparse.Namespace) -> int:
    return _run_verify(
        args.target_os, args.config, args.api, submit=False, banner="host verification"
    )


def _cmd_submit_profile(args: argparse.Namespace) -> int:
    # Registration is independent of the guest image: it captures + signs this host's hardware
    # profile and POSTs it for baselining. No image / version / readiness gate — that is `host
    # verify`, which needs a downloaded image to know which measurement to check against. A fresh
    # host (no image yet) is exactly when you submit, so requiring the image here was wrong.
    from chutes_cvm.guest.detection import (
        SUPPORTED_QEMU_BY_OS,
        verify_host_qemu_supported,
    )
    from chutes_cvm.guest.preflight import PreflightError, submit_profile

    print(_color("── chutes-cvm: host class submission ──", "1;36"))
    target_os = getattr(args, "target_os", None)
    if target_os:
        # Register the class this host will BE after the upgrade: the OS release picks the
        # QEMU that generates the measured guest ACPI, so the profile's release, QEMU and
        # -cpu args are all rewritten together (chutes_cvm.guest.preflight._apply_target_os).
        expected = SUPPORTED_QEMU_BY_OS.get(target_os)
        if expected is None:
            print(
                f"Registration failed: target OS {target_os!r} is not supported "
                f"{sorted(SUPPORTED_QEMU_BY_OS)}."
            )
            print(_color("\nResult: FAILED", "1;31"))
            return 1
        print(
            f"Registering for target OS {target_os} (ships QEMU {expected}); "
            f"the live OS/QEMU is ignored because the upgrade replaces it."
        )
    else:
        # Same gate `host verify` runs: registering the live host means registering its OS and
        # QEMU, so an unsupported release (or a release running a QEMU it does not ship) would
        # baseline a class that can never attest. Fail here rather than spend a generation slot
        # on it — the operator either upgrades, or names where they are headed with --target-os.
        try:
            verify_host_qemu_supported()
        except ValueError as exc:
            print(f"Registration failed: {exc}")
            print(
                "  If you are registering ahead of an OS upgrade, name the release you are "
                "moving to: `chutes-cvm host submit-profile --target-os "
                f"{sorted(SUPPORTED_QEMU_BY_OS)[-1]}`."
            )
            print(_color("\nResult: FAILED", "1;31"))
            return 1
    try:
        result = submit_profile(
            config_path=args.config,
            scripts_dir=str(SCRIPTS_DIR),
            api_base=args.api,
            target_os=target_os,
        )
    except PreflightError as exc:
        print(f"Registration failed: {exc}")
        print(_color("\nResult: FAILED", "1;31"))
        return 1
    already = "" if result.get("stored") else " (already on file)"
    print(
        f"Registered this host class for measurement{already} "
        f"(fingerprint {result.get('fingerprint', '?')}) — Chutes will generate its measurements."
    )
    print(_color("\nResult: SUBMITTED", "1;32"))
    return 0


def _cmd_tune(args: argparse.Namespace) -> int:
    from chutes_cvm.host.tune import apply_tuning

    apply_tuning()
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    from chutes_cvm.host.tune import restore_tuning

    restore_tuning()
    return 0


def _cmd_reset_gpus(args: argparse.Namespace) -> int:
    """Reset all host GPUs via nvidia-gpu-tools SBR (delegates to devices/reset-gpus.sh)."""
    return _run_script("devices/reset-gpus.sh", [])


def _cmd_vfio_wedged(args: argparse.Namespace) -> int:
    """Exit 0 if host PCI passthrough operations are wedged (a reset is needed before
    launch), else 1. Lets orchestration gate a launch/reset on the machine-parseable code.
    """
    from chutes_cvm.vfio import pci_operations_wedged

    return 0 if pci_operations_wedged() else 1


def _add_api_args(p: argparse.ArgumentParser) -> None:
    # Both default from the environment (resolved at parse time) so miners need no flags:
    # --config from CHUTES_CVM_CONFIG (else ./config.yaml), --api from CHUTES_API_BASE (else prod).
    p.add_argument(
        "--config",
        metavar="PATH",
        default=default_config_path(),
        help="Launch config.yaml with the miner hotkey (default: ./config.yaml; env CHUTES_CVM_CONFIG).",
    )
    p.add_argument(
        "--api",
        metavar="URL",
        default=default_api_base(),
        help="Control-plane base URL (default: https://api.chutes.ai; env CHUTES_API_BASE).",
    )


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `setup` owns its own argparse (--noninteractive), so forward to it
    # verbatim before our argparse touches the args (matches the top-level passthrough pattern).
    if argv and argv[0] == "setup":
        from chutes_cvm.host.setup import main as _setup_main

        return _setup_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="chutes-cvm host",
        description="Host lifecycle, attestation, and GPU/PCI hardware.",
    )
    sub = parser.add_subparsers(dest="verb", required=True, metavar="<verb>")

    # Registered for `chutes-cvm host --help` visibility; dispatched above.
    sub.add_parser(
        "setup",
        add_help=False,
        help="Provision this TDX host (args forwarded; `chutes-cvm host setup --help`).",
    )

    verify = sub.add_parser(
        "verify",
        help="Check this host will relaunch and re-attest (optionally after an OS upgrade).",
        description=(
            "Run the launch gates without launching a VM: host QEMU is the one its OS release "
            "baselines, and the control plane has a published measurement for this host class. "
            "Exit 0 READY / 1 BLOCKED / 2 WARNING. To register an unbaselined host class, use "
            "`chutes-cvm host submit-profile`."
        ),
    )
    verify.add_argument(
        "--target-os",
        metavar="VERSION",
        help="Verify against a target OS release's QEMU (pre-upgrade check), e.g. 26.04.",
    )
    _add_api_args(verify)
    verify.set_defaults(func=_cmd_verify)

    submit = sub.add_parser(
        "submit-profile",
        help="Register this host class with Chutes so it can generate measurements.",
        description=(
            "Capture this host's hardware profile, sign it with the miner hotkey, and register it "
            "with the control plane for baselining. Independent of the guest image — run it on a "
            "fresh host before any image is downloaded. Exit 0 submitted / 1 failed."
        ),
    )
    submit.add_argument(
        "--target-os",
        metavar="VERSION",
        help="Register the class this host becomes after an OS upgrade, e.g. 26.04 — the "
        "profile's OS release, QEMU version and -cpu args are all taken from that release "
        "instead of the live host.",
    )
    _add_api_args(submit)
    submit.set_defaults(func=_cmd_submit_profile)

    tune = sub.add_parser(
        "tune",
        help="Apply NVIDIA-recommended host CPU tuning (governor=performance, disable C1E/C6).",
    )
    tune.set_defaults(func=_cmd_tune)

    restore = sub.add_parser(
        "restore",
        help="Restore host CPU settings saved by `host tune` (no-op if never tuned).",
    )
    restore.set_defaults(func=_cmd_restore)

    reset = sub.add_parser(
        "reset-gpus",
        help="Reset all host GPUs via nvidia-gpu-tools SBR (stop any VM first).",
    )
    reset.set_defaults(func=_cmd_reset_gpus)

    vfio = sub.add_parser(
        "vfio-wedged",
        help="Exit 0 if host PCI passthrough is wedged and needs a reset before launch, else 1.",
    )
    vfio.set_defaults(func=_cmd_vfio_wedged)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
