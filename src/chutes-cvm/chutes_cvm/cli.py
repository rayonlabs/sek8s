"""chutes-cvm — CLI for confidential-VM host operations.

Invoked as ``chutes-cvm <command>`` via the ``chutes-cvm`` console script (installed by the
package's ``src/chutes-cvm/install.sh``), or directly as ``python3 -m chutes_cvm.cli
<command>``.

This is the package-level dispatcher: it routes to the ``guest`` group (launch / stop / down),
the ``host`` group (setup / verify / submit-profile / tune / restore / reset-gpus /
vfio-wedged), image (``image``), measurement (``measurements``), and config (``config``)
subpackages, so it lives at the package root rather than under any one of them.

Stdlib-only dispatcher. Every operator command is a noun group (guest / host / image / config /
measurements) whose args are forwarded verbatim to that subpackage's own ``main`` — each owns
its own ``--help`` and imports its implementation lazily, so a command that needs extra
dependencies never burdens one that doesn't. The low-level QEMU-boot primitive
(``chutes_cvm.guest.__main__``) is not a CLI command — ``guest launch`` reaches it via import.
"""

import argparse
import os
import sys


def _installed_version() -> str:
    """The version of the installed ``chutes-cvm`` distribution (accurate for editable,
    non-editable, and PyPI installs alike, since it reads the dist metadata)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("chutes-cvm")
    except PackageNotFoundError:
        return "unknown"


def _cmd_version(_args: argparse.Namespace) -> int:
    """Print the version and where this CLI resolves from — the `package`/`python` lines let a
    miner spot a stray editable/.venv install shadowing the install.sh shim on PATH."""
    import chutes_cvm

    print(f"chutes-cvm {_installed_version()}")
    print(f"  package: {os.path.dirname(os.path.abspath(chutes_cvm.__file__))}")
    print(f"  python:  {sys.executable}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chutes-cvm",
        description="Operate and inspect Chutes confidential GPU VMs on this host.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"chutes-cvm {_installed_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # Every operator command is a noun group whose args are forwarded verbatim to that
    # subpackage's own main (see _PASSTHROUGH / main): the entries below exist for
    # `chutes-cvm --help` visibility; main() intercepts them before argparse (which mishandles
    # leading options like --help/--image via REMAINDER), so no func is set.
    sub.add_parser(
        "guest",
        add_help=False,
        help="TDX VM lifecycle — launch / stop / down "
        "(args forwarded; `chutes-cvm guest --help`).",
    )
    sub.add_parser(
        "host",
        add_help=False,
        help="Host lifecycle + hardware — setup / verify / submit-profile / tune / restore / "
        "reset-gpus / vfio-wedged (args forwarded; `chutes-cvm host --help`).",
    )
    sub.add_parser(
        "image",
        add_help=False,
        help="Base image sets — download / verify / manifest (args forwarded; "
        "`chutes-cvm image --help`).",
    )
    sub.add_parser(
        "config",
        add_help=False,
        help="Manage the launch config.yaml — init / verify (args forwarded; "
        "`chutes-cvm config --help`).",
    )
    sub.add_parser(
        "measurements",
        add_help=False,
        help="Offline TDX measurement generation — generate / list "
        "(build-host tool; args forwarded, `chutes-cvm measurements --help`).",
    )

    # Not a passthrough noun group: a self-contained utility so a miner can confirm which
    # chutes-cvm (and version) is actually on PATH. Also available as `chutes-cvm --version`.
    p_version = sub.add_parser(
        "version",
        help="Print the installed chutes-cvm version and where it resolves from.",
    )
    p_version.set_defaults(func=_cmd_version)

    return parser


# Commands whose arguments are forwarded verbatim to an underlying main(argv). Intercepted
# before argparse because REMAINDER mishandles leading options (e.g. `image --image`,
# `host --help`). Each underlying main owns its own --help. The low-level QEMU boot primitive
# (chutes_cvm.guest.__main__) is not a CLI command — `guest launch` reaches it via import.
_PASSTHROUGH = (
    "guest",
    "host",
    "image",
    "config",
    "measurements",
)


def main(argv: "list[str] | None" = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in _PASSTHROUGH:
        forward = raw[1:]
        if raw[0] == "guest":
            from chutes_cvm.guest.cli import main as _guest_main

            return _guest_main(forward)
        if raw[0] == "host":
            from chutes_cvm.host.cli import main as _host_main

            return _host_main(forward)
        if raw[0] == "image":
            from chutes_cvm.guest.image_set import main as _image_main

            return _image_main(forward)
        if raw[0] == "measurements":
            from chutes_cvm.measurement.generate_measurements import (
                main as _measurements_main,
            )

            return _measurements_main(forward)
        from chutes_cvm.guest.config import main as _config_main

        return _config_main(forward)
    args = build_parser().parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
