# TDX guest measurements and independent verification

Chutes confidential GPU VMs run as Intel TDX trust domains. Each VM produces
hardware-rooted measurements that a central validator checks before releasing
secrets (LUKS keys) to the guest. This document describes how those measurements
are structured, what determines them, and how they can be independently
reproduced and verified.

## Measurement registers

A TDX quote reports:

- **MRTD** — the build-time measurement of the virtual firmware (TDVF).
- **RTMR0–3** — four runtime measurement registers, each a SHA-384 extension
  chain seeded from a fixed initial state.

For a chutes guest, the registers fall into two scopes:

- **Version-level** — the same for every hardware topology of a given guest
  image version: `mrtd`, `rtmr1`, `rtmr2`, and the guest-software measurement
  (`rtmr3`).
- **Topology-level** — varies with the VM's hardware topology: `rtmr0`.

The validator matches the version-level registers to identify the image, then
matches `rtmr0` to identify the specific topology. The reference values live in
the chutes-ops configuration the validator consumes.

## What RTMR0 covers

RTMR0 is extended by the firmware (TDVF) during its configuration phase, before
the operating system boots. Its inputs are the virtual platform as presented to
the guest: the firmware configuration volume, the UEFI variable / Secure Boot
configuration, the platform description tables the VMM supplies (ACPI, SMBIOS),
and the boot configuration.

Most of these are fixed by the guest image version. The parts that change with
the VM's hardware are the platform tables — specifically the ACPI/SMBIOS
description of **memory size, CPU/NUMA layout, and PCIe/GPU BAR windows**. That
is why RTMR0, and only RTMR0, is topology-specific.

## Determinism and hardware independence

RTMR0 is a deterministic function of the guest image (firmware) plus a small set
of topology parameters — total guest memory, CPU/socket/NUMA topology, and GPU
BAR sizing. Two properties make it reproducible and verifiable off the target
hardware:

- **Physical GPUs are not measured.** The measured platform tables describe the
  PCIe *topology* (root ports, MMIO windows) presented to the guest; the
  passed-through device endpoints themselves are not part of RTMR0. The GPU's
  contribution flows entirely through parameters (BAR size, count, placement).
- **Host CPU vendor is irrelevant.** A TDX trust domain's guest physical address
  width is fixed at TD creation (GPAW), so the guest address layout is identical
  whether the underlying host is AMD or Intel.

Because of this, the expected measurements for a topology can be computed from
its parameters, independent of which specific machine (or vendor) will run it.

## Producing measurements for a topology

For each supported topology, the expected measurements are computed from that
topology's parameters and published to chutes-ops ahead of time, so a miner's VM
can attest successfully on first launch. A miner describes its hardware with the
`discover-profile` tool; the matching topology's measurements must already be
published for attestation to succeed.

Security is unchanged by precomputation: publishing a topology's expected values
only lets a genuine, matching VM attest. A miner that misreports its hardware
simply fails attestation — its real measurements will not match any published
value, and no secret is released.

## Independent verification

Every register is a deterministic function of documented inputs, so its expected
value can be recomputed and checked against what a genuine TDX quote reports —
requiring trust only in the hardware quote, not in chutes.

Inside a guest, the firmware records every measurement extension in the CC event
log (`/sys/firmware/acpi/tables/data/CCEL`). The measurement tooling parses that
log and replays the SHA-384 chains to reproduce each register, then compares them
to published reference values (no signed quote required for the replay itself). Anyone can build the image (see `ansible/guest`),
run it, and confirm the reproduced registers match the published values and are a
faithful function of the documented inputs.

## Tooling

On-host capture (`guest-tools/measurement/`):

- **`capture-measurement-artifacts.sh`** — capture a guest's CC event log and the
  platform tables it measures (the inputs for offline reproduction). Requires TDX
  hardware, since the CCEL only exists there.
- **`extract-measurements.sh`** — report a running guest's live measurements from a
  fresh quote (MRTD + RTMR0-3); verification, not reproduction.

Offline replay/generation — the `chutes_cvm.measurement` package
(`src/chutes-cvm/chutes_cvm/measurement/`):

- **`ccel_replay.py`** — parse the event log, replay the RTMR chains, and verify
  them against known-good values (`--expect`) or compare two captures (`diff`).
- **`generate_measurements.py`** — offline per-topology RTMR0 generation.
- **`utils/`** — diagnostics (per-table ACPI byte-diff; event-log preimage
  matcher).

The measurement package reuses the same VM launch definitions as the host
launcher (`src/chutes-cvm/chutes_cvm/guest`), so reproduced measurements track
the real launch by construction.
