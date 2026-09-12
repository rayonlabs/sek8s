"""Version-level runtime RTMRs (RTMR1/RTMR2/RTMR3), computed offline at build time.

Unlike RTMR0 (firmware + per-topology ACPI; see generate_measurements.py), these are
**version-level** — identical across GPU topologies — and derive from the built image:

    RTMR1/RTMR2  the direct-boot kernel/initrd/cmdline (via the tdx-measure fork,
                 --runtime-only). Post-LUKS: LUKS rebuilds the initrd, and RTMR2 measures
                 that final one.
    RTMR3        the SHA-384 extension chain over the userspace files named in the image's
                 /etc/tdx-measure.conf. Content-derived — normally computed PRE-LUKS against the
                 plaintext root; a re-run against an already-encrypted image unlocks it with the
                 LUKS passphrase and recomputes fresh (never a cached value).

Both replay exactly what the launcher boots / the guest measures, so the pinned values match
the running VM by construction. Ports host-tools' former compute-rtmr1-2.sh / compute-rtmr3.sh.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from chutes_cvm import proc
from chutes_cvm.measurement.rtmr3 import Rtmr3Error, fold_chain, measured_hashes
from chutes_cvm.paths import tdx_measure_script


class MeasurementError(RuntimeError):
    """A runtime-RTMR computation failed (missing input, tool error, parse failure)."""


def _have(tool: str) -> bool:
    """True if ``tool`` is on PATH."""
    return shutil.which(tool) is not None


# ── RTMR1 / RTMR2 (direct boot; version-level) ─────────────────────────────────

_RTMR1_RE = re.compile(r"^RTMR1:\s*([0-9a-fA-F]+)", re.MULTILINE)
_RTMR2_RE = re.compile(r"^RTMR2:\s*([0-9a-fA-F]+)", re.MULTILINE)


def compute_rtmr1_2(
    image: str, tdx_measure_bin: str = "tdx-measure"
) -> tuple[str, str]:
    """Compute (RTMR1, RTMR2) from the image's staged direct-boot artifacts.

    Reads ``<image-without-ext>.{vmlinuz,initrd,cmdline}`` (staged by stage-boot-artifacts —
    the exact bytes the launcher boots) and runs the tdx-measure fork in ``--runtime-only``
    direct-boot mode. Returns bare uppercase hex. Needs the fork on PATH (or an absolute
    ``tdx_measure_bin``); no TDX/GPU/topology input.
    """
    # Absolute so the paths written into metadata.json resolve correctly — the tdx-measure fork
    # opens them relative to the metadata file (a temp dir), not this process's cwd.
    base = os.path.splitext(os.path.abspath(image))[0]
    kernel, initrd = base + ".vmlinuz", base + ".initrd"
    cmdline_file = base + ".cmdline"
    for f in (kernel, initrd, cmdline_file):
        if not os.path.isfile(f):
            raise MeasurementError(
                f"missing direct-boot artifact {f} — run stage-boot-artifacts first"
            )
    # $(cat file) semantics: drop trailing newlines from the staged cmdline.
    cmdline = Path(cmdline_file).read_text().rstrip("\n")

    metadata = {"direct": {"kernel": kernel, "initrd": initrd, "cmdline": cmdline}}
    with tempfile.TemporaryDirectory() as td:
        meta_path = os.path.join(td, "metadata.json")
        Path(meta_path).write_text(json.dumps(metadata))
        result = proc.run(
            [tdx_measure_bin, "--runtime-only", meta_path],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-4:]
        raise MeasurementError(
            "tdx-measure --runtime-only failed "
            f"(exit {result.returncode}):\n    " + "\n    ".join(tail)
        )
    out = result.stdout
    m1, m2 = _RTMR1_RE.search(out), _RTMR2_RE.search(out)
    if not m1 or not m2:
        raise MeasurementError(
            "could not parse RTMR1/RTMR2 from tdx-measure output:\n" + out
        )
    return m1.group(1).upper(), m2.group(1).upper()


# ── RTMR3 (userspace file chain; version-level, LUKS-independent) ──────────────


def rtmr3_chain(mount_root: str, conf_path: str) -> tuple[str, list[tuple[str, str]]]:
    """Compute RTMR3 over a mounted root by running the bundled ``tdx-measure``.

    Which files are measured, in what order, and how each is hashed are decided by that
    script — the same one the guest runs at boot and at verify time — so this cannot
    predict a value the guest will not reproduce. Only the chain fold happens here,
    because the hardware does the folding at boot.

    Returns (uppercase hex, [(per-file sha384 hex, root-relative path)]).
    """
    try:
        hashes = measured_hashes(mount_root, conf_path, tdx_measure_script())
        return fold_chain(hashes), hashes
    except Rtmr3Error as exc:
        raise MeasurementError(str(exc)) from exc


def _wait_for_path(path: str, timeout: float = 5.0) -> bool:
    """Poll for a device node to appear — partprobe populates ``/dev`` asynchronously."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return os.path.exists(path)


def _free_nbd_device() -> str:
    """The first ``/dev/nbdN`` with nothing attached (sysfs size 0)."""
    for i in range(16):
        try:
            if Path(f"/sys/block/nbd{i}/size").read_text().strip() == "0":
                return f"/dev/nbd{i}"
        except OSError:
            continue
    raise MeasurementError(
        "no free /dev/nbd device — run `modprobe nbd max_part=8` or disconnect a stale one"
    )


def _detect_root_partition(nbd: str) -> tuple[str, bool]:
    """``(root partition device, is_luks)`` for a connected nbd image.

    Mirrors the build's own blkid-based layout detection (luks_encrypt.yml): the root is the
    ``crypto_LUKS`` partition when the image is encrypted, else the largest ext4 (the separate
    ``/boot`` is a smaller ext4). blkid reads the on-disk signature directly, so detection never
    depends on libguestfs inspecting the image.
    """
    luks_part: str | None = None
    best_ext4: str | None = None
    best_size = -1
    for part in sorted(glob.glob(f"{nbd}p*")):
        fstype = proc.run(
            ["blkid", "-o", "value", "-s", "TYPE", part],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if fstype == "crypto_LUKS":
            luks_part = part
        elif fstype == "ext4":
            try:
                size = int(
                    Path(f"/sys/class/block/{os.path.basename(part)}/size").read_text()
                )
            except OSError:
                size = 0
            if size > best_size:
                best_size, best_ext4 = size, part
    if luks_part:
        return luks_part, True
    if best_ext4:
        return best_ext4, False
    raise MeasurementError(
        f"no ext4 or LUKS root partition found on {nbd} — is this a bootable image?"
    )


@contextlib.contextmanager
def _mounted_image_root(
    image: str, luks_passphrase: str | None, root_part: str | None = None
):
    """Mount the image's OS root read-only and yield the mount path.

    Uses qemu-nbd + (for an encrypted root) ``cryptsetup luksOpen`` + ``mount`` — the same tooling
    the build uses to CREATE the image — rather than libguestfs, whose appliance will not open a
    LUKS2/argon2id root here (it detects the header but the open silently fails). Requires root,
    which the measurement flow already has. Tears down mount -> luksClose -> nbd disconnect in all
    cases so a failure never leaks a mapping or an nbd connection.

    ``root_part`` forces the partition: an absolute ``/dev/...`` path, or a suffix (e.g. ``p1``)
    appended to the chosen nbd device. When unset the root is auto-detected.
    """
    if os.geteuid() != 0:
        raise MeasurementError(
            "RTMR3 needs root to mount the image (qemu-nbd/mount) — re-run with sudo"
        )
    # cryptsetup is required ONLY for an encrypted root (checked in the LUKS branch below), so a
    # plaintext (debug) image measures identically without it — the process after mounting is the
    # same for prod and debug; the only difference is the luksOpen step.
    for tool in ("qemu-nbd", "mount", "umount", "blkid"):
        if not _have(tool):
            raise MeasurementError(
                f"{tool} not found — install qemu-utils and util-linux"
            )

    proc.run(["modprobe", "nbd", "max_part=8"], capture_output=True, text=True)
    nbd = _free_nbd_device()
    mapper_name: str | None = None
    mnt: str | None = None
    connected = False
    try:
        result = proc.run(
            ["qemu-nbd", "--read-only", "--connect", nbd, image],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise MeasurementError(f"qemu-nbd connect failed: {result.stderr.strip()}")
        connected = True
        proc.run(["partprobe", nbd], capture_output=True, text=True)
        if not _wait_for_path(f"{nbd}p1"):
            raise MeasurementError(
                f"partitions did not appear on {nbd} after partprobe"
            )

        if root_part:
            part = root_part if root_part.startswith("/dev/") else f"{nbd}{root_part}"
            # blkid (not cryptsetup) tells LUKS from ext4, so plaintext images stay cryptsetup-free.
            fstype = proc.run(
                ["blkid", "-o", "value", "-s", "TYPE", part],
                capture_output=True,
                text=True,
            ).stdout.strip()
            is_luks = fstype == "crypto_LUKS"
        else:
            part, is_luks = _detect_root_partition(nbd)

        if is_luks:
            if not luks_passphrase:
                raise MeasurementError(
                    "image root is LUKS-encrypted — set LUKS_PASSPHRASE (the passphrase the image "
                    "was encrypted with) so RTMR3 can be recomputed from the unlocked root"
                )
            if not _have("cryptsetup"):
                raise MeasurementError(
                    "image root is LUKS-encrypted but cryptsetup is not installed — install it to "
                    "unlock the root (plaintext/debug images do not need cryptsetup)"
                )
            mapper_name = f"chutes-rtmr3-{os.getpid()}"
            # Passphrase via stdin (--key-file=-), never argv/ps; exact bytes, no trailing newline.
            result = proc.run(
                [
                    "cryptsetup",
                    "luksOpen",
                    "--readonly",
                    part,
                    mapper_name,
                    "--key-file=-",
                ],
                input=luks_passphrase,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise MeasurementError(
                    f"cryptsetup luksOpen failed: {result.stderr.strip()}"
                )
            source = f"/dev/mapper/{mapper_name}"
        else:
            source = part

        mnt = tempfile.mkdtemp(suffix="-rtmr3")
        result = proc.run(
            ["mount", "-o", "ro", source, mnt], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise MeasurementError(f"mount failed: {result.stderr.strip()}")
        yield mnt
    finally:
        if mnt:
            proc.run(["umount", mnt], capture_output=True, text=True)
            try:
                os.rmdir(mnt)
            except OSError:
                pass
        if mapper_name:
            proc.run(
                ["cryptsetup", "luksClose", mapper_name], capture_output=True, text=True
            )
        if connected:
            proc.run(["qemu-nbd", "--disconnect", nbd], capture_output=True, text=True)


def compute_rtmr3(
    image: str, root_part: str | None = None, luks_passphrase: str | None = None
) -> tuple[str, list[tuple[str, str]]]:
    """Compute RTMR3 by mounting the image's root read-only and replaying the file chain.

    Always recomputes fresh from the actual root — no cached/reused value. A plaintext ext4 root
    (the PRE-LUKS build stage) is mounted directly; an already-encrypted (post-LUKS) root is
    unlocked with ``luks_passphrase`` (the same passphrase the image was encrypted with). Both go
    through qemu-nbd + cryptsetup — the tooling that built the image — so it never depends on
    libguestfs. Requires root. ``root_part`` overrides the auto-detected root partition.

    Returns (uppercase hex, per-file [(sha384hex, root-relative path)]).
    """
    image = os.path.abspath(image)
    if not os.path.isfile(image):
        raise MeasurementError(f"image not found: {image}")
    with _mounted_image_root(image, luks_passphrase, root_part) as mnt:
        conf = os.path.join(mnt, "etc/tdx-measure.conf")
        if not os.path.isfile(conf):
            raise MeasurementError(
                "/etc/tdx-measure.conf not found in image — rtmr3-measure did not run"
            )
        return rtmr3_chain(mnt, conf)
