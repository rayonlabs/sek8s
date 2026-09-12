"""Tests for the CC event-log parser/replay (guest-tools/measurement/ccel_replay.py)."""

import hashlib
import struct

import pytest

# sys.path is wired to guest-tools/measurement by tests/measurement/conftest.py.
from chutes_cvm.measurement.ccel_replay import (
    EV_NO_ACTION,
    RTMR_ALG,
    RTMR_LEN,
    TPM_ALG_SHA256,
    TPM_ALG_SHA384,
    EventLogError,
    discover_mapping,
    parse_event_log,
    parse_quote_registers,
    replay,
    replay_all,
)

# --------------------------------------------------------------------------- #
# Synthetic-log builders (mirror the TCG_PCR_EVENT / TCG_PCR_EVENT2 wire format)
# --------------------------------------------------------------------------- #


def _header(event: bytes = b"Spec ID Event03\x00") -> bytes:
    # TCG_PCR_EVENT: pcrIndex(u32) eventType(u32) digest[20] eventSize(u32) event[]
    return (
        struct.pack("<II", 0, EV_NO_ACTION)
        + b"\x00" * 20
        + struct.pack("<I", len(event))
        + event
    )


def _record(mr_index, event_type, digests, data=b""):
    # digests: list of (alg_id, digest_bytes)
    out = struct.pack("<III", mr_index, event_type, len(digests))
    for alg, dig in digests:
        out += struct.pack("<H", alg) + dig
    out += struct.pack("<I", len(data)) + data
    return out


def _sha384(seed: bytes) -> bytes:
    return hashlib.sha384(seed).digest()


def _expected_replay(digests384):
    """Independent reference fold: acc = SHA384(acc || digest), from zeros."""
    acc = b"\x00" * RTMR_LEN
    for d in digests384:
        acc = hashlib.sha384(acc + d).digest()
    return acc


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #


def test_parse_reads_all_event2_records():
    d1, d2 = _sha384(b"a"), _sha384(b"b")
    blob = (
        _header()
        + _record(1, 0x80000008, [(TPM_ALG_SHA384, d1)], b"fw")
        + _record(1, 0x80000001, [(TPM_ALG_SHA384, d2)], b"var")
    )
    events = parse_event_log(blob)
    assert [e.mr_index for e in events] == [1, 1]
    assert events[0].digest(RTMR_ALG) == d1
    assert events[0].data == b"fw"
    assert events[1].type_name == "EV_EFI_VARIABLE_DRIVER_CONFIG"


def test_parse_multi_alg_digests_selects_by_alg():
    d256, d384 = _sha384(b"x")[:32], _sha384(b"y")
    blob = _header() + _record(
        2, 0x80000003, [(TPM_ALG_SHA256, d256), (TPM_ALG_SHA384, d384)], b""
    )
    (ev,) = parse_event_log(blob)
    assert ev.digest(TPM_ALG_SHA384) == d384
    assert ev.digest(TPM_ALG_SHA256) == d256


def test_parse_stops_on_trailing_zero_padding():
    blob = (
        _header()
        + _record(1, 0x80000008, [(TPM_ALG_SHA384, _sha384(b"a"))])
        + b"\x00" * 64  # ACPI region padding
    )
    events = parse_event_log(blob)
    assert len(events) == 1


def test_parse_rejects_unknown_algorithm():
    bad = struct.pack("<III", 1, 0x80000008, 1) + struct.pack("<H", 0xABCD)
    with pytest.raises(EventLogError):
        parse_event_log(_header() + bad)


def test_parse_empty_log_raises():
    with pytest.raises(EventLogError):
        parse_event_log(_header())


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #


def test_replay_reproduces_reference_fold():
    digs = [_sha384(bytes([i])) for i in range(4)]
    blob = _header() + b"".join(
        _record(1, 0x80000008, [(TPM_ALG_SHA384, d)]) for d in digs
    )
    events = parse_event_log(blob)
    assert replay(events, 1) == _expected_replay(digs)


def test_replay_skips_ev_no_action():
    real = _sha384(b"real")
    noise = _sha384(b"noise")
    blob = (
        _header()
        + _record(1, EV_NO_ACTION, [(TPM_ALG_SHA384, noise)])  # must be ignored
        + _record(1, 0x80000008, [(TPM_ALG_SHA384, real)])
    )
    events = parse_event_log(blob)
    assert replay(events, 1) == _expected_replay([real])


def test_replay_all_groups_by_mr_index():
    da, db = _sha384(b"a"), _sha384(b"b")
    blob = (
        _header()
        + _record(1, 0x80000008, [(TPM_ALG_SHA384, da)])
        + _record(3, 0x80000003, [(TPM_ALG_SHA384, db)])
    )
    out = replay_all(parse_event_log(blob))
    assert out == {1: _expected_replay([da]), 3: _expected_replay([db])}


def test_empty_register_replays_to_zero_seed():
    # A register with no (extendable) events stays at the initial all-zero state.
    blob = _header() + _record(1, EV_NO_ACTION, [(TPM_ALG_SHA384, _sha384(b"z"))])
    events = parse_event_log(blob)
    assert replay(events, 1) == b"\x00" * RTMR_LEN


# --------------------------------------------------------------------------- #
# quote parsing + mapping discovery
# --------------------------------------------------------------------------- #


def _quote_with(rtmr0=None, rtmr1=None, rtmr2=None, rtmr3=None, mrtd=None):
    q = bytearray(632)
    for name, off, val in [
        ("mrtd", 184, mrtd),
        ("rtmr0", 376, rtmr0),
        ("rtmr1", 424, rtmr1),
        ("rtmr2", 472, rtmr2),
        ("rtmr3", 520, rtmr3),
    ]:
        if val is not None:
            end = off + 48
            q[off:end] = val
    return bytes(q)


def test_parse_quote_registers_offsets():
    r0 = bytes(range(48))
    regs = parse_quote_registers(_quote_with(rtmr0=r0))
    assert regs["rtmr0"] == r0
    assert len(regs["rtmr3"]) == 48


def test_parse_quote_rejects_short_input():
    with pytest.raises(ValueError):
        parse_quote_registers(b"\x00" * 100)


def test_discover_mapping_matches_replay_to_quote_rtmr():
    da, db = _sha384(b"a"), _sha384(b"b")
    blob = (
        _header()
        + _record(1, 0x80000008, [(TPM_ALG_SHA384, da)])
        + _record(2, 0x80000003, [(TPM_ALG_SHA384, db)])
    )
    events = parse_event_log(blob)
    quote = _quote_with(
        rtmr0=_expected_replay([da]),
        rtmr1=_expected_replay([db]),
    )
    mapping = discover_mapping(events, parse_quote_registers(quote))
    assert mapping == {1: "rtmr0", 2: "rtmr1"}


def test_diff_cli_flags_constant_vs_varying(tmp_path, capsys):
    from chutes_cvm.measurement import ccel_replay as cc

    const = _sha384(b"firmware-derived")  # same in both captures
    topo_a, topo_b = _sha384(b"topoA"), _sha384(b"topoB")  # topology-varying
    log_a = (
        _header()
        + _record(1, 0x80000008, [(TPM_ALG_SHA384, const)])
        + _record(1, 0x0000000A, [(TPM_ALG_SHA384, topo_a)])
    )
    log_b = (
        _header()
        + _record(1, 0x80000008, [(TPM_ALG_SHA384, const)])
        + _record(1, 0x0000000A, [(TPM_ALG_SHA384, topo_b)])
    )
    pa, pb = tmp_path / "a.bin", tmp_path / "b.bin"
    pa.write_bytes(log_a)
    pb.write_bytes(log_b)

    rc = cc.main(["diff", str(pa), str(pb)])
    out = capsys.readouterr().out
    assert rc == 1  # the two captures differ (the topology event)
    assert " 0 same" in out  # constant event matches
    assert " 1 DIFF" in out  # topology event differs

    # A capture diffed against itself: everything constant, rc 0.
    assert cc.main(["diff", str(pa), str(pa)]) == 0


def test_discover_mapping_from_reference_value_only():
    # No quote at all -- just a known-good rtmr0 (e.g. from chutes-ops).
    da = _sha384(b"a")
    blob = _header() + _record(1, 0x80000008, [(TPM_ALG_SHA384, da)])
    events = parse_event_log(blob)
    expected = {"rtmr0": _expected_replay([da])}
    assert discover_mapping(events, expected) == {1: "rtmr0"}


def test_replay_cli_validates_against_expect_without_quote(tmp_path, capsys):
    import struct as _struct

    from chutes_cvm.measurement import ccel_replay as cc

    da = _sha384(b"boot")
    blob = _header() + _record(1, 0x80000008, [(TPM_ALG_SHA384, da)])
    # trailing pad so the parser's zero-padding break path is exercised too
    log = tmp_path / "ccel_data.bin"
    log.write_bytes(blob + _struct.pack("<III", 0, 0, 0))
    expected_rtmr0 = _expected_replay([da]).hex().upper()

    rc = cc.main(["replay", str(log), "--expect", f"rtmr0={expected_rtmr0}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MrIndex 1 -> rtmr0" in out
    assert "PASS" in out

    # A wrong reference must fail, not silently pass.
    rc_bad = cc.main(["replay", str(log), "--expect", f"rtmr0={'00' * 48}"])
    assert rc_bad == 1
