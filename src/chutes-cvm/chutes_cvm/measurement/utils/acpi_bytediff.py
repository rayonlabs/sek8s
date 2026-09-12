#!/usr/bin/env python3
"""Byte-diff two fw_cfg ``etc/acpi/tables`` blobs, table by table.

The RTMR0 "ACPI DATA" digest (#13) is ``SHA384(etc/acpi/tables)`` over the raw
fw_cfg blob. To prove we can regenerate that blob offline (Tier-2), we compare a
QEMU-generated blob (``tdx-measure --create-acpi-tables``) against a real dump
from a debug boot. A whole-blob ``cmp`` only says "differs"; this walks the
concatenated System Description Tables and reports which *table* differs and the
first differing offset — so a mismatch confined to DSDT's pci-hole64 (host
phys-bits) is distinguishable from a real topology-table divergence (SRAT etc.).

Usage:
    acpi_bytediff.py <blob_a> <blob_b>        # diff two blobs
    acpi_bytediff.py --list <blob>            # just enumerate tables in one blob
"""

import argparse
import hashlib
import struct

# ACPI System Description Table header: signature[4], length(u32) at offset 4.
_HDR = struct.Struct("<4sI")


def list_tables(blob: bytes) -> list[tuple[str, int, int]]:
    """Return [(signature, offset, length)] for each table in the blob.

    QEMU concatenates the tables; each begins with a standard 36-byte header
    whose first 8 bytes are signature + total length. FACS has no standard
    length semantics but does carry a length field, so the same walk works.
    Trailing zero padding (the fw_cfg blob is a fixed size) ends the walk.
    """
    out: list[tuple[str, int, int]] = []
    off = 0
    n = len(blob)
    while off + 8 <= n:
        sig, length = _HDR.unpack_from(blob, off)
        if length == 0 or sig == b"\x00\x00\x00\x00":
            break  # padding region
        if off + length > n or not sig.isalnum() and sig not in (b"FACS",):
            # Not a plausible table header -> we've walked off the end.
            break
        out.append((sig.decode("latin1").rstrip("\x00"), off, length))
        off += length
    return out


def _cmd_list(blob: bytes) -> int:
    print(f"blob: {len(blob)} bytes  SHA384={hashlib.sha384(blob).hexdigest()[:24]}..")
    for sig, off, length in list_tables(blob):
        end = off + length
        seg_hash = hashlib.sha384(blob[off:end]).hexdigest()[:20]
        print(f"  {sig:<6} off={off:>7} len={length:>7}  sha384={seg_hash}..")
    return 0


def _cmd_diff(a: bytes, b: bytes) -> int:
    ta = {sig: (off, length) for sig, off, length in list_tables(a)}
    tb = {sig: (off, length) for sig, off, length in list_tables(b)}
    print(f"A: {len(a)} bytes, {len(ta)} tables   B: {len(b)} bytes, {len(tb)} tables")
    if hashlib.sha384(a).digest() == hashlib.sha384(b).digest():
        print("IDENTICAL blobs (SHA384 match) -> RTMR0 #13 would match.")
        return 0
    print(f"{'table':<6}{'A len':>8}{'B len':>8}   status")
    print("-" * 50)
    rc = 0
    for sig in sorted(set(ta) | set(tb)):
        pa, pb = ta.get(sig), tb.get(sig)
        if pa is None or pb is None:
            la = "-" if pa is None else pa[1]
            lb = "-" if pb is None else pb[1]
            only = "B" if pa is None else "A"
            print(f"{sig:<6}{la:>8}{lb:>8}   ONLY IN {only}")
            rc = 1
            continue
        sa = a[pa[0] : pa[0] + pa[1]]  # noqa: E203
        sb = b[pb[0] : pb[0] + pb[1]]  # noqa: E203
        if sa == sb:
            print(f"{sig:<6}{pa[1]:>8}{pb[1]:>8}   identical")
        else:
            first = next(
                (i for i in range(min(len(sa), len(sb))) if sa[i] != sb[i]),
                min(len(sa), len(sb)),
            )
            print(f"{sig:<6}{pa[1]:>8}{pb[1]:>8}   DIFFERS (first @ +{first})")
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("blob_a")
    ap.add_argument("blob_b", nargs="?")
    ap.add_argument("--list", action="store_true", help="enumerate tables in blob_a")
    args = ap.parse_args(argv)

    a = open(args.blob_a, "rb").read()
    if args.list or args.blob_b is None:
        return _cmd_list(a)
    b = open(args.blob_b, "rb").read()
    return _cmd_diff(a, b)


if __name__ == "__main__":
    raise SystemExit(main())
