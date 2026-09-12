# Guest measurement & verification tooling

Tools to **extract, verify, and reproduce** a TDX guest VM's measurements
(MRTD / RTMR0-3) — so per-topology reference values can be produced for the
central validator / `chutes-ops`, and so an independent party can verify them.

This directory imports the launcher's QEMU-arg builders from
`the chutes-cvm package (chutes_cvm.guest)` (a one-way dependency: verification reuses the
*exact* launch code, so reproduced measurements can't drift from a real launch).

Design + rationale: [`docs/specs/offline-rtmr0-measurement.md`](../../docs/specs/offline-rtmr0-measurement.md).

## Tools

- **`capture-measurement-artifacts.sh`** — run inside a (debug) guest to capture
  the artifacts needed to *reproduce* measurements offline: its `data/CCEL` event
  log plus the fw_cfg ACPI/SMBIOS preimages (`etc/acpi/tables`, `etc/table-loader`,
  `etc/acpi/rsdp`, `etc/smbios/*`), `/sys/firmware/dmi/tables/*`, and the kernel
  cmdline. These are the inputs the offline recompute and the `#14` matcher consume.
  Driven unattended by the `capture-ccel` Ansible role (the final
  gather step (`capture-ccel`); re-run standalone via `--tags gather-measurement-inputs`).
  **Note:** the CCEL only exists on TDX hardware, so this bundle requires a
  TDX-capable host once per image version (RTMR1/2/3 + MRTD are reproducible
  offline without it; a fully CCEL-free RTMR0 is the Phase-2 goal).
- **`extract-measurements.sh`** — run inside a guest to *report* its live
  measurements: generates a fresh TDX quote and decodes MRTD + RTMR0-3 from it.
  Verification/inspection of a running VM — distinct from the artifact capture above.
- **`ccel_replay.py`** — CC event-log parser + SHA-384 RTMR replay, with `diff`
  (constant-vs-varying events across two CCELs) and `--expect` (verify a replay
  against known-good `chutes-ops` values, no quote needed). The validated oracle.

Diagnostics / one-off helpers live in **`utils/`**:

- **`utils/acpi_bytediff.py`** — parse a fw_cfg `etc/acpi/tables` blob into its
  tables and byte-diff two blobs per-table (localizes a generated-vs-real
  mismatch to a specific ACPI table). The go/no-go gate for offline ACPI gen.
- **`utils/smbios_match.py`** — reverse-engineer the exact preimage of the RTMR0
  SMBIOS handoff event (`#14`) by matching `SHA384` candidates against the real
  digest in a captured CCEL.

Planned (per the spec's generator design): `arg_synth.py` (synthesize a
topology's QEMU args from a `discover-profile` JSON), `acpi_source.py`
(pluggable generated-vs-captured ACPI/SMBIOS source), `measurements generate`
(splice + replay → full `teeMeasurements` block). Captured baselines and
generated outputs live in the top-level `measurements/<version>[-debug]/` (data,
kept separate from this tooling dir; the generated `measurements.yaml` is
per-variant, the captured baseline is not).

## External dependency: `virtee/tdx-measure` (forked)

The engine that computes MRTD + the TdxTable HOB (`#0`) and generates the ACPI
fw_cfg blobs is [`virtee/tdx-measure`](https://github.com/virtee/tdx-measure).
We maintain a thin fork **`git@github.com:chutesai/tdx-measure.git`**, branch
`feat/numa-smp-smbios-passthrough` (adds `-numa` / `-smp` / `-smbios`
pass-throughs the upstream `qemu` metadata block can't express). Build:

```bash
git clone -b feat/numa-smp-smbios-passthrough git@github.com:chutesai/tdx-measure.git
cd tdx-measure/cli && cargo build --release   # -> cli/target/release/tdx-measure
```

Requirements: Rust toolchain, Docker + buildx, KVM, and RAM ≥ the guest for
ACPI generation (`--create-acpi-tables` backs the guest memory; 8× RTX Pro 6000
= 768G). The guest **address width** is TDX GPAW (uniform across host CPUs), so
generation is not CPU-family-bound; only the CPU *topology* must match the
profile. Prototype metadata/validation scripts (`gen_metadata.py`,
`run_validation.sh`) live under `local/scripts/` (gitignored) pending
generalization into `arg_synth.py`.
