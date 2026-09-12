"""generate_measurements splices topology-varying RTMR0 events onto a baseline CCEL.

The events it overrides (TD-HOB + the three ACPI DATA digests) are located BY IDENTITY,
not fixed position, because the boot method changes the surrounding constant-event count:
indirect boot = 19 MrIndex==1 events, direct boot = 14 (no #15-18 boot variables and one
fewer QEMU FW CFG). These assert the locator/splice track the ACPI events across that shift
and never touch SMBIOS (#14).
"""

import pytest
from chutes_cvm.measurement.ccel_replay import RTMR_ALG, Event
from chutes_cvm.measurement.generate_measurements import (
    FORK_ACPI_IDX,
    FORK_TDHOB_IDX,
    locate_rtmr0_events,
    mr1_events,
    overrides_from_fork_log,
    replay_with_overrides,
)

# TCG event types used in an RTMR0 log (subset).
_TDHOB = 0x8000000B  # EV_EFI_HANDOFF_TABLES2 (TdxTable)
_FW_BLOB = 0x8000000A  # EV_EFI_PLATFORM_FIRMWARE_BLOB2 (CFV)
_CONFIG = 0x0000000A  # EV_PLATFORM_CONFIG_FLAGS (QEMU FW CFG / ACPI DATA)
_VAR_CFG = 0x80000001  # EV_EFI_VARIABLE_DRIVER_CONFIG (SecureBoot/PK/KEK/db/dbx)
_SEP = 0x00000004  # EV_SEPARATOR
_SMBIOS = 0x80000009  # EV_EFI_HANDOFF_TABLES (SMBIOS)
_VAR_BOOT = 0x80000002  # EV_EFI_VARIABLE_BOOT
_VAR_AUTH = 0x800000E0  # EV_EFI_VARIABLE_AUTHORITY


def _ev(event_type, data=b"", tag=0):
    # Distinct per-event digest so a mis-targeted splice changes the fold.
    return Event(
        mr_index=1,
        event_type=event_type,
        digests={RTMR_ALG: bytes([tag]) * 48},
        data=data,
    )


def _direct_boot():
    """14-event direct-boot layout (matches the real minimal-dump capture)."""
    ev = [
        _ev(_TDHOB, b"\x00TdxTable", 1),
        _ev(_FW_BLOB, b"", 2),
        _ev(_CONFIG, b"QEMU FW CFG etc/extra-pci-roots", 3),
        _ev(_CONFIG, b"QEMU FW CFG BootMenu", 4),
    ]
    ev += [_ev(_VAR_CFG, b"SecureBoot", 10 + i) for i in range(5)]
    ev += [
        _ev(_SEP, b"", 20),
        _ev(_CONFIG, b"ACPI DATA", 21),  # table-loader
        _ev(_CONFIG, b"ACPI DATA", 22),  # rsdp
        _ev(_CONFIG, b"ACPI DATA", 23),  # acpi-tables
        _ev(_SMBIOS, b"", 24),
    ]
    return ev


def _indirect_boot():
    """19-event indirect-boot layout (extra fw_cfg + the #15-18 boot variables)."""
    ev = _direct_boot()
    ev.insert(4, _ev(_CONFIG, b"QEMU FW CFG bootorder", 5))  # the 3rd fw_cfg event
    ev += [
        _ev(_VAR_BOOT, b"BootOrder", 30),
        _ev(_VAR_BOOT, b"Boot0000", 31),
        _ev(_VAR_BOOT, b"Boot0001", 32),
        _ev(_VAR_AUTH, b"SbatLevel", 33),
    ]
    return ev


def test_locate_direct_boot_shifts_acpi_indices():
    tdhob, acpi = locate_rtmr0_events(_direct_boot())
    assert tdhob == 0
    assert acpi == [10, 11, 12]  # shifted down vs the 19-event layout


def test_locate_indirect_boot():
    tdhob, acpi = locate_rtmr0_events(_indirect_boot())
    assert tdhob == 0
    assert acpi == [11, 12, 13]


@pytest.mark.parametrize("layout", [_direct_boot, _indirect_boot])
def test_splice_targets_acpi_and_never_smbios(layout):
    events = layout()
    mr1 = mr1_events(events)
    tdhob, acpi = locate_rtmr0_events(events)
    # Every overridden index is a TD-HOB or ACPI DATA event — never SMBIOS.
    for i in [tdhob, *acpi]:
        assert mr1[i].event_type in (_TDHOB, _CONFIG)
        assert b"ACPI DATA" in mr1[i].data or b"TdxTable" in mr1[i].data
    smbios_idx = next(i for i, e in enumerate(mr1) if e.event_type == _SMBIOS)
    assert smbios_idx not in {tdhob, *acpi}


@pytest.mark.parametrize("layout", [_direct_boot, _indirect_boot])
def test_fork_log_overrides_map_to_located_indices(layout):
    events = layout()
    tdhob, acpi = locate_rtmr0_events(events)
    log = [f"{i:02x}" * 48 for i in range(max((FORK_TDHOB_IDX, *FORK_ACPI_IDX)) + 1)]
    ov = overrides_from_fork_log(log, tdhob, acpi)
    assert ov[tdhob] == bytes.fromhex(log[FORK_TDHOB_IDX])
    for baseline_i, fork_i in zip(acpi, FORK_ACPI_IDX):
        assert ov[baseline_i] == bytes.fromhex(log[fork_i])


@pytest.mark.parametrize("layout", [_direct_boot, _indirect_boot])
def test_self_consistent_splice_reproduces_own_rtmr0(layout):
    # Splicing an event's OWN digests back through the fork-log path must reproduce the
    # unmodified fold — proves the located indices line up with the replay.
    events = layout()
    mr1 = mr1_events(events)
    tdhob, acpi = locate_rtmr0_events(events)
    log = ["00" * 48] * (max((FORK_TDHOB_IDX, *FORK_ACPI_IDX)) + 1)
    log[FORK_TDHOB_IDX] = mr1[tdhob].digest().hex()
    for baseline_i, fork_i in zip(acpi, FORK_ACPI_IDX):
        log[fork_i] = mr1[baseline_i].digest().hex()
    ov = overrides_from_fork_log(log, tdhob, acpi)
    assert replay_with_overrides(events, ov) == replay_with_overrides(events, {})


def test_locate_rejects_malformed_layout():
    events = [_ev(_TDHOB, b"\x00TdxTable"), _ev(_CONFIG, b"ACPI DATA")]  # only 1 ACPI
    with pytest.raises(Exception, match="unexpected RTMR0 layout"):
        locate_rtmr0_events(events)
