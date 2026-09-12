#!/usr/bin/env python3
"""Parse and replay the TDX CC (Confidential Computing) event log.

Phase-1 tooling for offline TDX measurement work. Given the CC event-log blob
(`/sys/firmware/acpi/tables/data/CCEL`, captured by `extract-measurements.sh`)
this module:

  1. Decodes the TCG_PCR_EVENT2 records (the TDVF measured-boot log).
  2. Groups events by measurement-register index (MrIndex).
  3. Replays the SHA-384 extend chain -- ``rtmr = SHA384(rtmr || digest)`` from a
     48-byte zero seed -- to reconstruct each RTMR value.
  4. Cross-checks the replayed values against the RTMR0-3 in a live TDX quote,
     which both validates the parse and *discovers* the MrIndex->RTMR mapping
     empirically rather than trusting a hard-coded convention.

The point of Phase 1: enumerate exactly which events feed RTMR0 so we can tell
which inputs are reproducible offline (from topology parameters) versus
host-physical. Everything downstream depends on that answer.

Pure stdlib -- no third-party deps, runs on a minimal host.

Log format references:
  - TCG PC Client Platform Firmware Profile (TCG_PCR_EVENT / TCG_PCR_EVENT2).
  - Intel TDX Virtual Firmware (TDVF) Design Guide -- MrIndex assignment.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# --- TPM algorithm ids -> digest length (bytes) ----------------------------
TPM_ALG_SHA1 = 0x0004
TPM_ALG_SHA256 = 0x000B
TPM_ALG_SHA384 = 0x000C
TPM_ALG_SHA512 = 0x000D
TPM_ALG_SM3_256 = 0x0012

_ALG_SIZES = {
    TPM_ALG_SHA1: 20,
    TPM_ALG_SHA256: 32,
    TPM_ALG_SHA384: 48,
    TPM_ALG_SHA512: 64,
    TPM_ALG_SM3_256: 32,
}
_ALG_NAMES = {
    TPM_ALG_SHA1: "sha1",
    TPM_ALG_SHA256: "sha256",
    TPM_ALG_SHA384: "sha384",
    TPM_ALG_SHA512: "sha512",
    TPM_ALG_SM3_256: "sm3_256",
}

# RTMRs are extended with SHA-384; that is the algorithm we replay.
RTMR_ALG = TPM_ALG_SHA384
RTMR_LEN = 48

# TDVF measurement-register index convention (EDK2 TD_MR_INDEX_*). MrIndex 0 is
# MRTD (build-time, not RTMR-extended); 1-4 are the runtime RTMRs. Used only as
# the default label -- discover_mapping() verifies the true mapping against a
# quote, so a firmware that deviates is caught rather than silently mislabeled.
DEFAULT_MR_INDEX_TO_RTMR = {1: "rtmr0", 2: "rtmr1", 3: "rtmr2", 4: "rtmr3"}

# --- event type names (subset; unknown types render as hex) -----------------
EV_NO_ACTION = 0x00000003
_EVENT_TYPES = {
    0x00000000: "EV_PREBOOT_CERT",
    0x00000001: "EV_POST_CODE",
    EV_NO_ACTION: "EV_NO_ACTION",
    0x00000004: "EV_SEPARATOR",
    0x00000005: "EV_ACTION",
    0x00000006: "EV_EVENT_TAG",
    0x00000007: "EV_S_CRTM_CONTENTS",
    0x00000008: "EV_S_CRTM_VERSION",
    0x00000009: "EV_CPU_MICROCODE",
    0x0000000A: "EV_PLATFORM_CONFIG_FLAGS",
    0x0000000B: "EV_TABLE_OF_DEVICES",
    0x0000000C: "EV_COMPACT_HASH",
    0x0000000D: "EV_IPL",
    0x0000000E: "EV_IPL_PARTITION_DATA",
    0x00000010: "EV_EFI_EVENT_BASE",
    0x80000001: "EV_EFI_VARIABLE_DRIVER_CONFIG",
    0x80000002: "EV_EFI_VARIABLE_BOOT",
    0x80000003: "EV_EFI_BOOT_SERVICES_APPLICATION",
    0x80000004: "EV_EFI_BOOT_SERVICES_DRIVER",
    0x80000005: "EV_EFI_RUNTIME_SERVICES_DRIVER",
    0x80000006: "EV_EFI_GPT_EVENT",
    0x80000007: "EV_EFI_ACTION",
    0x80000008: "EV_EFI_PLATFORM_FIRMWARE_BLOB",
    0x80000009: "EV_EFI_HANDOFF_TABLES",
    0x8000000A: "EV_EFI_PLATFORM_FIRMWARE_BLOB2",
    0x8000000B: "EV_EFI_HANDOFF_TABLES2",
    0x8000000C: "EV_EFI_VARIABLE_BOOT2",
    0x800000E0: "EV_EFI_VARIABLE_AUTHORITY",
}


def event_type_name(event_type: int) -> str:
    return _EVENT_TYPES.get(event_type, f"0x{event_type:08X}")


# --- quote register extraction (offsets match attest.py / extract_tdx_quote.c)
_QUOTE_FIELDS = {
    "mrtd": (184, 48),
    "rtmr0": (376, 48),
    "rtmr1": (424, 48),
    "rtmr2": (472, 48),
    "rtmr3": (520, 48),
}
_MIN_QUOTE_LEN = 632


def parse_quote_registers(quote: bytes) -> dict[str, bytes]:
    """Return {mrtd, rtmr0..3: 48-byte digests} from a TDX v4 quote binary."""
    if len(quote) < _MIN_QUOTE_LEN:
        raise ValueError(
            f"Quote is {len(quote)} bytes, expected >= {_MIN_QUOTE_LEN}; "
            "not a TDX v4 quote?"
        )
    regs: dict[str, bytes] = {}
    for name, (off, size) in _QUOTE_FIELDS.items():
        end = off + size
        regs[name] = quote[off:end]
    return regs


@dataclass
class Event:
    """One TCG_PCR_EVENT2 record."""

    mr_index: int
    event_type: int
    digests: dict[int, bytes] = field(default_factory=dict)  # alg_id -> digest
    data: bytes = b""

    @property
    def type_name(self) -> str:
        return event_type_name(self.event_type)

    def digest(self, alg: int = RTMR_ALG) -> bytes | None:
        return self.digests.get(alg)


class EventLogError(ValueError):
    """Raised when the event-log blob cannot be parsed."""


def _read(fmt: str, blob: bytes, off: int) -> tuple:
    size = struct.calcsize(fmt)
    if off + size > len(blob):
        raise EventLogError(f"truncated log: need {size} bytes at offset {off}")
    return struct.unpack_from(fmt, blob, off), off + size


def parse_event_log(blob: bytes) -> list[Event]:
    """Parse a CC event-log blob into a list of Events.

    Layout: a legacy TCG_PCR_EVENT header record (Spec ID Event03), followed by
    TCG_PCR_EVENT2 records. Trailing zero padding in the ACPI region is ignored.
    """
    events: list[Event] = []
    off = 0

    # Header record (TCG_PCR_EVENT, SHA1-shaped): pcrIndex, eventType, digest[20],
    # eventSize, event[]. It carries the Spec ID Event03 algorithm table; we skip
    # over it and rely on the static _ALG_SIZES map for the event2 records.
    (_pcr, _etype), off = _read("<II", blob, off)
    off += 20  # legacy 20-byte SHA1 digest
    (event_size,), off = _read("<I", blob, off)
    off += event_size
    if _etype != EV_NO_ACTION:
        # Not fatal -- some producers omit the classic header -- but worth noting.
        pass

    n = len(blob)
    while off + 12 <= n:
        # An all-zero or all-0xFF MrIndex+EventType+count region is trailing
        # padding in the fixed-size ACPI event-log buffer.
        peek_end = off + 12
        peek = blob[off:peek_end]
        if peek == b"\x00" * 12 or peek == b"\xff" * 12:
            break

        (mr_index, event_type, count), rec_off = _read("<III", blob, off)
        digests: dict[int, bytes] = {}
        ok = True
        for _ in range(count):
            try:
                (alg,), rec_off = _read("<H", blob, rec_off)
            except EventLogError:
                ok = False
                break
            size = _ALG_SIZES.get(alg)
            if size is None:
                # Not a known algorithm: we've run past the real records into
                # the ACPI region's trailing padding (often 0xFF/0x00). Stop and
                # keep what we parsed rather than treating padding as an error.
                ok = False
                break
            dig_end = rec_off + size
            if dig_end > n:
                ok = False
                break
            digests[alg] = blob[rec_off:dig_end]
            rec_off = dig_end
        if not ok:
            break
        try:
            (data_size,), rec_off = _read("<I", blob, rec_off)
        except EventLogError:
            break
        data_end = rec_off + data_size
        if data_end > n:
            break
        data = blob[rec_off:data_end]
        rec_off = data_end

        events.append(
            Event(mr_index=mr_index, event_type=event_type, digests=digests, data=data)
        )
        off = rec_off

    if not events:
        raise EventLogError("no TCG_PCR_EVENT2 records found")
    return events


def replay(events: list[Event], mr_index: int, alg: int = RTMR_ALG) -> bytes:
    """Fold the extend chain for one MrIndex: acc = H(acc || digest), from zeros.

    Events of ``event_type == EV_NO_ACTION`` are informational and are NOT
    extended into the register (matching TDVF behaviour), so they are skipped.
    """
    hash_name = _ALG_NAMES[alg]
    size = _ALG_SIZES[alg]
    acc = b"\x00" * size
    for ev in events:
        if ev.mr_index != mr_index or ev.event_type == EV_NO_ACTION:
            continue
        digest = ev.digest(alg)
        if digest is None:
            raise EventLogError(
                f"event (type {ev.type_name}) missing {hash_name} digest"
            )
        acc = hashlib.new(hash_name, acc + digest).digest()
    return acc


def replay_all(events: list[Event], alg: int = RTMR_ALG) -> dict[int, bytes]:
    """Replay every MrIndex present in the log. Returns {mr_index: digest}."""
    indices = sorted({ev.mr_index for ev in events})
    return {idx: replay(events, idx, alg) for idx in indices}


def discover_mapping(
    events: list[Event], expected_regs: dict[str, bytes]
) -> dict[int, str]:
    """Match each MrIndex's replayed value to a known RTMR value.

    ``expected_regs`` maps rtmr names -> 48-byte digests. The source is
    deliberately generic: it can come from a TDX quote OR from known-good
    reference values (e.g. the chutes-ops measurements) -- no quote required to
    determine RTMR0. Returns {mr_index: 'rtmrN'} for every register whose replay
    reproduces a known value -- the empirical, self-validating mapping.
    """
    replays = replay_all(events)
    rtmr_by_value = {
        expected_regs[name]: name
        for name in ("rtmr0", "rtmr1", "rtmr2", "rtmr3")
        if name in expected_regs
    }
    return {
        idx: rtmr_by_value[val] for idx, val in replays.items() if val in rtmr_by_value
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fmt(b: bytes) -> str:
    return b.hex().upper()


def _event_ascii(ev: "Event", n: int = 40) -> str:
    """Best-effort printable label from an event's data (variable/fw_cfg name)."""
    return "".join(chr(c) if 32 <= c < 127 else "." for c in ev.data[:n])


def _cmd_diff(args: argparse.Namespace) -> int:
    """Compare two CCELs per-register: which events are identical vs differ.

    'same' events are constant across the two captures (firmware/image-derived);
    'DIFF' events are what varies (topology / QEMU version). This is how we
    confirm RTMR0 decomposes into a reusable constant baseline + a small set of
    topology-varying events.
    """
    ga: dict[int, list[Event]] = defaultdict(list)
    gb: dict[int, list[Event]] = defaultdict(list)
    for e in parse_event_log(Path(args.eventlog_a).read_bytes()):
        ga[e.mr_index].append(e)
    for e in parse_event_log(Path(args.eventlog_b).read_bytes()):
        gb[e.mr_index].append(e)

    rc = 0
    for idx in sorted(set(ga) | set(gb)):
        la, lb = ga.get(idx, []), gb.get(idx, [])
        label = DEFAULT_MR_INDEX_TO_RTMR.get(idx, "?")
        print(
            f"\n=== MrIndex {idx} ({label}): A={len(la)} events, B={len(lb)} events ==="
        )
        for i in range(max(len(la), len(lb))):
            a = la[i] if i < len(la) else None
            b = lb[i] if i < len(lb) else None
            ev = a if a is not None else b
            if ev is None:
                continue
            da = a.digest(RTMR_ALG) if a else None
            db = b.digest(RTMR_ALG) if b else None
            same = a is not None and b is not None and da == db
            if not same:
                rc = 1
            mark = "same" if same else "DIFF"
            ha = da.hex()[:12] if da else "-"
            hb = db.hex()[:12] if db else "-"
            print(
                f" {i:2} {mark:<4} {ev.type_name:<30} "
                f"A={ha:<12} B={hb:<12} '{_event_ascii(ev)}'"
            )
    print("\n(same = constant across both captures; DIFF = topology/version-varying)")
    return rc


def _cmd_parse(args: argparse.Namespace) -> int:
    events = parse_event_log(Path(args.eventlog).read_bytes())
    only = args.rtmr
    print(f"{len(events)} events\n")
    print(f"{'#':>3}  {'MrIdx':>5}  {'EventType':<34}  {'SHA384(digest)':<24}  Size")
    for i, ev in enumerate(events):
        if only is not None and ev.mr_index != only:
            continue
        d = ev.digest(RTMR_ALG)
        dshort = (d.hex().upper()[:20] + "..") if d else "(no sha384)"
        print(
            f"{i:>3}  {ev.mr_index:>5}  {ev.type_name:<34}  {dshort:<24}  {len(ev.data)}"
        )
    return 0


def _parse_expect(pairs: list[str]) -> dict[str, bytes]:
    """Turn ['rtmr0=ABCD..', ...] into {name: 48-byte digest}.

    The oracle for validating a replay -- sourced from known-good reference
    values (e.g. chutes-ops), so no quote is needed to confirm RTMR0.
    """
    expected: dict[str, bytes] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--expect must be NAME=HEX, got {pair!r}")
        name, hexval = pair.split("=", 1)
        name = name.strip().lower()
        raw = bytes.fromhex(hexval.strip())
        if name not in ("rtmr0", "rtmr1", "rtmr2", "rtmr3", "mrtd"):
            raise ValueError(f"unknown register name {name!r}")
        if len(raw) != RTMR_LEN:
            raise ValueError(f"{name}: expected {RTMR_LEN} bytes, got {len(raw)}")
        expected[name] = raw
    return expected


def _cmd_replay(args: argparse.Namespace) -> int:
    events = parse_event_log(Path(args.eventlog).read_bytes())
    replays = replay_all(events)

    # Oracle: known-good register values, from either a quote OR --expect (e.g.
    # a chutes-ops rtmr0). No quote is required to determine/verify RTMR0.
    expected: dict[str, bytes] = {}
    if args.quote:
        expected.update(parse_quote_registers(Path(args.quote).read_bytes()))
    if args.expect:
        expected.update(_parse_expect(args.expect))

    mapping = DEFAULT_MR_INDEX_TO_RTMR
    if expected:
        discovered = discover_mapping(events, expected)
        if discovered:
            mapping = discovered
        print("MrIndex -> RTMR mapping (replay matched a known value):")
        for idx in sorted(mapping):
            print(f"  MrIndex {idx} -> {mapping[idx]}")
        print()

    rc = 0
    matched_any = False
    print(f"{'MrIdx':>5}  {'Label':<7}  {'Replayed RTMR (SHA-384)':<96}  Match")
    for idx in sorted(replays):
        label = mapping.get(idx, "-")
        val = replays[idx]
        match = ""
        if label in expected:
            ok = val == expected[label]
            match = "OK" if ok else "MISMATCH"
            matched_any = True
            if not ok:
                rc = 1
        print(f"{idx:>5}  {label:<7}  {_fmt(val):<96}  {match}")

    if expected:
        print("\nExpected (reference) registers:")
        for name in ("mrtd", "rtmr0", "rtmr1", "rtmr2", "rtmr3"):
            if name in expected:
                print(f"  {name:<6} {_fmt(expected[name])}")
        if not matched_any:
            print(
                "\nRESULT: INCONCLUSIVE -- no replayed register matched a reference "
                "value (check the MrIndex mapping / reference source)"
            )
            rc = 1
        else:
            print(
                "\nRESULT:",
                (
                    "PASS -- replay reproduces the reference RTMR(s)"
                    if rc == 0
                    else "FAIL -- see MISMATCH above"
                ),
            )
    return rc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("parse", help="dump events (optionally one MrIndex)")
    pp.add_argument("eventlog", help="path to CC event-log blob (data/CCEL)")
    pp.add_argument("--rtmr", type=int, help="show only this MrIndex")
    pp.set_defaults(func=_cmd_parse)

    pd = sub.add_parser(
        "diff", help="compare two CCELs per-register (constant vs varying events)"
    )
    pd.add_argument("eventlog_a", help="first CC event-log blob")
    pd.add_argument("eventlog_b", help="second CC event-log blob")
    pd.set_defaults(func=_cmd_diff)

    pr = sub.add_parser(
        "replay",
        help="replay extend chains; verify against known-good values (no quote needed)",
    )
    pr.add_argument("eventlog", help="path to CC event-log blob (data/CCEL)")
    pr.add_argument(
        "--expect",
        action="append",
        metavar="NAME=HEX",
        help="reference register value, e.g. --expect rtmr0=<chutes-ops hex> "
        "(repeatable). Validates the replay without a quote.",
    )
    pr.add_argument("--quote", help="optional TDX quote binary (alternative oracle)")
    pr.set_defaults(func=_cmd_replay)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (EventLogError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
