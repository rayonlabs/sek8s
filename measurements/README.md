# Measurement data (baselines + generated outputs)

Per-version measurement artifacts, kept separate from the tooling (on-host capture
in `guest-tools/measurement/`; offline replay/generation in `chutes_cvm.measurement`).
One subdir per guest image version *and variant*:

- `<version>/` — captured baseline (CCEL + fw_cfg ACPI/SMBIOS preimages,
  `baseline.json`) produced by the `capture-ccel` role (re-run via
  `--tags gather-measurement-inputs`). The baseline holds RTMR0 inputs only, which are
  identical across debug and prod, so it is not variant-split.
- `<version>/measurements.yaml` and `<version>-debug/measurements.yaml` — the generated
  `teeMeasurements` block, written per variant: a debug build's RTMR1/2/3 and MRTD differ
  from prod's, so the two must not share a file.

Committed reference data — small firmware/ACPI/SMBIOS preimages only. The
captured baseline holds only the **RTMR0** inputs (the debug CCEL splice + the
per-topology ACPI/SMBIOS preimages), which are identical across debug and prod.
**RTMR1/2/3** are not captured here; they are computed statically from each image at
build time — one post-luks `chutes-cvm measurements generate` (RTMR3 mounts the root,
unlocked via LUKS_PASSPHRASE) — for **both** prod and debug builds; the debug image is
attested too under the RC gate (its distinct measurement is registered `rc:true`).
