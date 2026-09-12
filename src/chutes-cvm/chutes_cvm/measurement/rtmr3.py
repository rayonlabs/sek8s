"""Python side of the RTMR3 measurement: run ``tdx-measure``, fold the chain.

**This module deliberately does not decide which files are measured, in what
order, or how they are hashed.** All three live in the ``tdx-measure`` shell
script, which the initramfs measurer, the build-time manifest generator and both
Python consumers all run. Four independent walkers of ``tdx-measure.conf`` used
to exist and they disagreed four ways — inline comments and trimming, symlinked
conf entries, sort collation, and ``sha384sum FILE`` versus stdin. Re-deriving
any of it here, however convenient, recreates that class of bug.

What is left in Python is the chain fold, which touches no files: the hardware
does the folding at boot, so only the verifier and the offline predictor need it.

**Stdlib only, and no chutes_cvm imports, deliberately.** This exact file is also
installed into the guest at ``/usr/local/lib/sek8s/rtmr3.py`` (by the rtmr3-measure
Ansible role, from this checkout) and imported by ``rtmr3-verify``, which runs on the
guest's *system* interpreter, ordered ``Before=k3s.service``, and powers the VM off on
failure. It must not resolve through ``/opt/sek8s/venv``, which is outside the RTMR3
measured-path list, nor drag in the rest of this package. Keep it importable as a bare
top-level module.
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404
from pathlib import Path

__all__ = [
    "Rtmr3Error",
    "RTMR3_LEN",
    "TDX_MEASURE",
    "measured_hashes",
    "fold_chain",
    "compute_rtmr3",
]

#: TDX RTMR registers are SHA-384, i.e. 48 bytes.
RTMR3_LEN = 48

#: In the guest, installed by the rtmr3-measure role — ``/usr/local/bin`` is itself
#: RTMR3-measured. On the host, callers pass the bundled copy
#: (``chutes_cvm/scripts/tdx-measure``) explicitly.
TDX_MEASURE = "/usr/local/bin/tdx-measure"


class Rtmr3Error(Exception):
    """Raised when ``tdx-measure`` fails or returns output that cannot be parsed."""


def measured_hashes(
    root: str | Path,
    conf: str | Path,
    tdx_measure: str | Path = TDX_MEASURE,
) -> list[tuple[str, str]]:
    """Return ``(sha384 hex, root-relative path)`` for every measured file, in chain order.

    ``root`` prefixes the conf's absolute paths: ``""`` for the live root, or the
    mount point of an image being measured offline. Ordering and hashing are
    whatever ``tdx-measure`` produced — this function only parses.
    """
    if not Path(tdx_measure).is_file():
        raise Rtmr3Error(f"tdx-measure not found: {tdx_measure}")

    # Fixed argv, no shell: the program is a caller-supplied path and the rest are data.
    result = subprocess.run(  # nosec B603
        [str(tdx_measure), "hash", str(root), str(conf)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-4:]
        raise Rtmr3Error(
            "tdx-measure hash failed: " + (" / ".join(detail) or "no output")
        )

    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        # "<sha384hex> <path>" — split once, so paths containing spaces survive.
        digest, _, rel_path = line.partition(" ")
        if not rel_path or len(digest) != 96:
            raise Rtmr3Error(f"malformed tdx-measure output: {line!r}")
        entries.append((digest, rel_path))

    if not entries:
        raise Rtmr3Error(f"tdx-measure returned no files for {conf}")
    return entries


def fold_chain(hashes: list[tuple[str, str]]) -> str:
    """RTMR3 over ``(hash hex, path)`` pairs in chain order.

    ``rtmr3 = SHA384(0x00*48 || SHA384(list))``, where ``list`` is ``tdx-measure hash``
    output verbatim — one ``"<sha384hex> <path>\\n"`` line per file. ONE hardware extend
    over a digest of the whole ordered list, not one extend per file: 41k per-file TDCALLs
    cost ~167s of boot and bind nothing this does not. Hashing the list text also binds the
    paths, which the former per-file content chain did not.

    Returns the final register value as uppercase hex.
    """
    body = "".join(f"{digest} {rel_path}\n" for digest, rel_path in hashes).encode()
    chain_digest = hashlib.sha384(body).digest()
    return hashlib.sha384(bytes(RTMR3_LEN) + chain_digest).hexdigest().upper()


def compute_rtmr3(
    root: str | Path,
    conf: str | Path,
    tdx_measure: str | Path = TDX_MEASURE,
) -> tuple[str, list[tuple[str, str]]]:
    """Convenience wrapper: ``(final RTMR3 uppercase hex, per-file (hash, path))``."""
    hashes = measured_hashes(root, conf, tdx_measure)
    return fold_chain(hashes), hashes
