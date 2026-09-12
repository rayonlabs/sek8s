#!/bin/bash
# capture-measurement-artifacts.sh - Capture the boot artifacts needed to
# reproduce a TDX guest's measurements OFFLINE (not the measurements themselves).
#
# Runs inside the (debug) guest and copies out the raw preimages the RTMR0 events
# are computed over, plus the CC event log used to splice the constant events:
#   - CCEL + data/CCEL           -> the event log (ccel_replay.py) → RTMR0-3
#   - fw_cfg ACPI/SMBIOS blobs    -> RTMR0 ACPI (#11-13) / SMBIOS (#14) preimages
#   - /proc/cmdline               -> part of the boot chain (feeds RTMR2)
#   - DMI + EFI vars (reference)  -> for boot-event reconstruction (Phase 2)
# then tars the result for extraction. Self-locating and CWD-independent so it can
# be driven unattended by the capture-ccel Ansible role (the final
# gather step (capture-ccel); re-run via `--tags gather-measurement-inputs`).
#
# This does NOT generate a quote or print RTMR values — that is a separate
# concern; see extract-measurements.sh for reporting a running VM's measurements.
#
# NOTE: capturing the CCEL requires TDX hardware. Until the full 19-event RTMR0
# reconstruction lands (Phase 2), producing this bundle needs a TDX-capable host
# once per image version. (RTMR1/2/3 + MRTD are already reproducible offline
# without it.)
#
# Usage: capture-measurement-artifacts.sh [--output-dir DIR]  (default: ./measurement_artifacts)
#
# Captures one bundle. A baseline is captured once per image version, so there is
# no boot-numbering — re-running overwrites the output dir.

# NOT `-e`: some /sys sources are optional per kernel/image; the load-bearing
# copies each guard themselves and we want a partial bundle over a hard abort.
set -uo pipefail

OUTPUT_DIR="measurement_artifacts"
while [ $# -gt 0 ]; do
    case "$1" in
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Resolve OUTPUT_DIR to an absolute path up front so every later `cp` is
# CWD-independent.
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

echo "==================================="
echo "Capturing measurement artifacts"
echo "Output directory: $OUTPUT_DIR"
echo "==================================="

# Boot chain / reference state.
echo "Capturing system state..."
cat /proc/cmdline > "$OUTPUT_DIR/cmdline.txt"
uptime > "$OUTPUT_DIR/uptime.txt" 2>/dev/null || true
dmesg | head -100 > "$OUTPUT_DIR/dmesg.txt" 2>/dev/null || true
date > "$OUTPUT_DIR/timestamp.txt"

# UEFI variables — reference inputs for reconstructing the boot RTMR0 events.
echo "Capturing UEFI variables..."
ls -la /sys/firmware/efi/efivars/ > "$OUTPUT_DIR/efivars_list.txt" 2>/dev/null || true
for var in BootCurrent BootOrder MTC NvVars VarErrorFlag; do
    VAR_FILE=$(find /sys/firmware/efi/efivars/ -name "$var-*" 2>/dev/null | head -1)
    if [ -n "$VAR_FILE" ]; then
        xxd "$VAR_FILE" > "$OUTPUT_DIR/efivar_${var}.txt" 2>/dev/null || true
    fi
done

# Capture CCEL event log.
#
# Two artifacts are needed to reconstruct the measurement chain offline:
#   - CCEL           : the small ACPI table (pointer+length into the log region)
#   - data/CCEL      : the actual CC event log blob (TCG_PCR_EVENT2 records) that
#                      ccel_replay.py parses and replays to reproduce RTMR0-3.
# Older kernels only expose the table; the data blob lives under tables/data/.
echo "Capturing CCEL..."
xxd /sys/firmware/acpi/tables/CCEL > "$OUTPUT_DIR/ccel.txt" 2>/dev/null || true
cp /sys/firmware/acpi/tables/CCEL      "$OUTPUT_DIR/ccel.bin"      2>/dev/null || true
if [ -r /sys/firmware/acpi/tables/data/CCEL ]; then
    cp /sys/firmware/acpi/tables/data/CCEL "$OUTPUT_DIR/ccel_data.bin" 2>/dev/null || true
    echo "  Captured event-log data blob: $OUTPUT_DIR/ccel_data.bin ($(stat -c%s "$OUTPUT_DIR/ccel_data.bin" 2>/dev/null || echo 0) bytes)"
else
    echo "  WARNING: /sys/firmware/acpi/tables/data/CCEL not readable — event-log replay will be unavailable."
fi

# Capture the fw_cfg blobs and SMBIOS tables that the RTMR0 "ACPI DATA" (events
# #11-13) and SMBIOS handoff (event #14) digests are computed over. These are the
# raw preimages: each RTMR0 ACPI/SMBIOS digest is SHA-384 of the corresponding
# blob below, so capturing them lets the offline generator reproduce (and the
# matcher reverse-engineer) those events without booting the topology again.
echo "Capturing fw_cfg ACPI + SMBIOS preimages..."
FWCFG="/sys/firmware/qemu_fw_cfg/by_name"
declare -A FWCFG_ITEMS=(
    [etc/acpi/tables]=acpi_tables.bin        # -> RTMR0 event #13
    [etc/table-loader]=table_loader.bin      # -> RTMR0 event #11
    [etc/acpi/rsdp]=rsdp.bin                 # -> RTMR0 event #12
    [etc/smbios/smbios-tables]=smbios_tables.bin   # SMBIOS structure table (#14 candidate)
    [etc/smbios/smbios-anchor]=smbios_anchor.bin   # SMBIOS entry point   (#14 candidate)
)
for item in "${!FWCFG_ITEMS[@]}"; do
    if [ -r "$FWCFG/$item/raw" ]; then
        cp "$FWCFG/$item/raw" "$OUTPUT_DIR/${FWCFG_ITEMS[$item]}" 2>/dev/null || true
        echo "  fw_cfg $item -> ${FWCFG_ITEMS[$item]} ($(stat -c%s "$OUTPUT_DIR/${FWCFG_ITEMS[$item]}" 2>/dev/null || echo 0) bytes)"
    else
        echo "  (fw_cfg $item not readable)"
    fi
done
# The installed SMBIOS as the kernel sees it (alternative #14 preimage candidates).
for f in /sys/firmware/dmi/tables/DMI /sys/firmware/dmi/tables/smbios_entry_point; do
    if [ -r "$f" ]; then
        cp "$f" "$OUTPUT_DIR/$(basename "$f")" 2>/dev/null || true
        echo "  $f -> $(basename "$f") ($(stat -c%s "$OUTPUT_DIR/$(basename "$f")" 2>/dev/null || echo 0) bytes)"
    fi
done

echo ""
echo "Artifacts saved to $OUTPUT_DIR/"
ls -lh "$OUTPUT_DIR/"
echo ""

# Bundle so the capture playbook can fetch a single artifact.
TARBALL="${OUTPUT_DIR}.tar.gz"
tar -czf "$TARBALL" -C "$(dirname "$OUTPUT_DIR")" "$(basename "$OUTPUT_DIR")"
echo "Bundled artifacts: $TARBALL"

echo "Capture complete"
