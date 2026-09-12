#!/usr/bin/env python3
"""Reverse-engineer the RTMR0 SMBIOS handoff (event #14) preimage.

The RTMR0 ACPI "ACPI DATA" events (#11-13) turned out to be plain
``SHA384(fw_cfg blob)`` of ``etc/table-loader`` / ``etc/acpi/rsdp`` /
``etc/acpi/tables``. The SMBIOS handoff event (``EV_EFI_HANDOFF_TABLES``, #14)
is the last per-topology event whose exact preimage we haven't pinned — TDVF
hashes the *content* the SMBIOS entry-point descriptor points to, but whether
that is the raw ``etc/smbios/smbios-tables`` bytes, entry-point+tables, the
kernel-visible ``/sys/firmware/dmi/tables/DMI``, or an EDK2-normalized variant
is what this script determines.

Given a capture directory (from ``extract-measurements.sh``, which now grabs the
SMBIOS blobs) and the matching CCEL, it hashes every candidate preimage and
reports which one reproduces the real #14 digest. Once a candidate matches, the
offline generator computes #14 the same way — no debug boot per topology.

Usage:
    smbios_match.py <capture_dir> [--ccel <path>]

<capture_dir> should contain some of: smbios_tables.bin, smbios_anchor.bin,
DMI, smbios_entry_point; and a CCEL blob (ccel_data.bin or data/CCEL) unless
--ccel is given.
"""

import argparse
import hashlib
from pathlib import Path

from chutes_cvm.measurement.ccel_replay import RTMR_ALG, parse_event_log

SMBIOS_HANDOFF_TYPE = "EV_EFI_HANDOFF_TABLES"


def _load(d: Path, name: str) -> bytes | None:
    p = d / name
    return p.read_bytes() if p.is_file() else None


def _find_ccel(d: Path) -> Path | None:
    for cand in ("ccel_data.bin", "data/CCEL", "CCEL"):
        p = d / cand
        if p.is_file():
            return p
    return None


def real_smbios_digest(ccel: Path) -> bytes:
    """Return the RTMR0 (MrIndex 1) EV_EFI_HANDOFF_TABLES digest."""
    events = parse_event_log(ccel.read_bytes())
    hits = [e for e in events if e.mr_index == 1 and e.type_name == SMBIOS_HANDOFF_TYPE]
    if not hits:
        raise SystemExit("No EV_EFI_HANDOFF_TABLES event in RTMR0 of this CCEL")
    if len(hits) > 1:
        print(f"warning: {len(hits)} handoff-tables events; using the first")
    digest = hits[0].digest(RTMR_ALG)
    if digest is None:
        raise SystemExit("EV_EFI_HANDOFF_TABLES event has no digest for the RTMR alg")
    return digest


def candidates(d: Path) -> dict[str, bytes]:
    """Build the candidate preimages from whatever the capture holds."""
    tables = _load(d, "smbios_tables.bin")
    anchor = _load(d, "smbios_anchor.bin")
    dmi = _load(d, "DMI")
    ep = _load(d, "smbios_entry_point")
    out: dict[str, bytes] = {}

    def add(name: str, blob: bytes | None) -> None:
        if blob is not None:
            out[name] = blob

    add("smbios_tables", tables)
    add("smbios_anchor", anchor)
    add("DMI", dmi)
    add("smbios_entry_point", ep)
    if anchor is not None and tables is not None:
        out["anchor||tables"] = anchor + tables
        out["tables||anchor"] = tables + anchor
    if ep is not None and dmi is not None:
        out["entry_point||DMI"] = ep + dmi
        out["DMI||entry_point"] = dmi + ep
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("capture_dir")
    ap.add_argument("--ccel", help="CCEL blob (else auto-detected in capture_dir)")
    args = ap.parse_args(argv)

    d = Path(args.capture_dir)
    ccel = Path(args.ccel) if args.ccel else _find_ccel(d)
    if ccel is None or not ccel.is_file():
        raise SystemExit(f"No CCEL found (looked in {d}); pass --ccel")

    target = real_smbios_digest(ccel)
    print(f"real RTMR0 #14 ({SMBIOS_HANDOFF_TYPE}) digest:\n  {target.hex()}\n")

    cands = candidates(d)
    if not cands:
        raise SystemExit(
            "No SMBIOS blobs found. Capture smbios_tables.bin / smbios_anchor.bin "
            "/ DMI / smbios_entry_point (see extract-measurements.sh)."
        )

    match = None
    print(f"{'candidate preimage':<22}{'bytes':>8}  SHA384")
    print("-" * 70)
    for name, blob in cands.items():
        h = hashlib.sha384(blob).digest()
        hit = h == target
        match = match or (name if hit else None)
        print(
            f"{name:<22}{len(blob):>8}  {h.hex()[:24]}..  {'<== MATCH' if hit else ''}"
        )

    print()
    if match:
        print(f"RESULT: RTMR0 #14 = SHA384({match}). Wire this into the generator.")
        return 0
    print(
        "RESULT: no raw candidate matched — #14 is likely EDK2-normalized "
        "(SmbiosMeasurementDxe zeroes volatile fields). Next: apply the TCG "
        "SMBIOS measurement filter to smbios_tables and retry."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
