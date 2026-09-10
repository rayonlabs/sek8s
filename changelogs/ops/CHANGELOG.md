# Ops Changelog

Operational tooling changes: `ansible/host/`, `host-tools/`, `.github/workflows/`.
Versioned with CalVer `YYYY.MM.PATCH` via `changelogs/ops/VERSION`. Run `make promote-changelogs` to aggregate fragments into the current version section.

## [2026.09.1] - 2026-09-10

### Added
- `make publish-guest` / `make publish-guest-debug` — upload a built guest image **and
  its direct-boot artifacts** to R2 in one step (via `publish-image.sh` + rclone),
  replacing the manual per-file `rclone copyto`. Uploads
  `<version>[-debug].{qcow2,vmlinuz,initrd,cmdline}` to the canonical
  `tdx-guest[-debug].{qcow2,vmlinuz,initrd,cmdline}` R2 objects that
  `quick-launch --download` fetches; pre-flight fails if any of the four is missing, so a
  qcow2 is never published without its matching boot artifacts. Prompts once for the
  rclone config password (`RCLONE_CONFIG_PASS`).
- `build-setup.yml` now installs and enables Docker **with the buildx plugin** on build
  hosts. Offline RTMR0 generation (the `compute-rtmr0` role) builds the tdx-measure fork's
  patched QEMU via `docker build --progress plain`, which needs BuildKit/buildx. A build host
  without it reported every profile PENDING with "Failed to invoke `docker build`" (no
  docker) or "unknown flag: --progress" (no buildx).
- Image-set coherence checking, as the single image format. A VM image is a set — the
  qcow2 plus its direct-boot `.vmlinuz`/`.initrd`/`.cmdline` and a `manifest.json` (sha256 +
  size per artifact) — verified as a matched unit against the manifest at download (full
  hash) and at launch (presence/size). A stale or mismatched artifact now fails with a clear
  "out of sync" error instead of an opaque boot/attestation failure. `chutes.guest.image_set`
  is the single manifest generator/verifier, used by the build, `publish-image.sh`, and the
  launcher; the boot artifacts previously had no integrity link at all.
- `make images` builds **standalone docker images** — `docker/<name>` dirs that have a
  `Dockerfile` but no matching `src/` package (e.g. `docker/busybox`). Build all, or one by
  name: `make images busybox`. Images are tagged with a `latest`-style tag (`latest` on
  `main`, `<branch>-latest` otherwise), and `tag`/`push`/`sign` now work for these
  standalone images too (versioned `dev` when no package `VERSION` applies).
- **RC-gate operator signing key injection.** `config.yaml` gains an optional `rc.operator_signing_key`
  (host path to the operator RSA private key), also settable via `quick-launch.sh --operator-signing-key`.
  The referenced key is copied onto the per-VM config volume as `operator-signing-key.pem` (mode 0600),
  where the RTMR2-measured initramfs (`rc-sign`) signs the attestation nonce with it for `rc=true`
  measurements — the API verifies with the matching public key. The key is referenced by path (never
  inlined in the config), and never leaves the config volume + initramfs `/run` tmpfs. Completes the
  producer side of the RC-gate flow (the initramfs consumer already existed).
- **`chutes-cvm` CLI (seed).** A stdlib-based Python CLI (`chutes.guest.cli`) as the eventual
  single entry point for confidential-VM host operations, growing gradually to subsume the
  host-tools bash scripts. First command: `chutes-cvm verify-host` (wraps the existing
  host-readiness gates with a colored, TTY-aware result banner; `--target-os` for a
  pre-upgrade check). No new runtime dependency — verify-host is pure stdlib.
- **`host-tools/provision/setup-chutes-cvm.sh`.** Idempotent, self-contained bootstrap: creates
  a venv with the CLI's deps and installs the `chutes-cvm` shim (`→ python3 -m chutes.guest.cli`)
  pointing at this checkout. A miner can run it directly (no Ansible required), and Ansible
  host-setup can invoke the same script — one source of truth for CLI setup. Paths are
  overridable via `CHUTES_CVM_VENV` / `CHUTES_CVM_BIN`.
- **`chutes-cvm discover-profile`.** New CLI command that captures this host's GPU/CPU/NUMA
  profile (delegating to `discover-profile.sh` for now), so `chutes-cvm` is the front door
  for both host inspection commands (`verify-host`, `discover-profile`). `--json-only` /
  `--no-json` forward to the underlying script.
- **`chutes-cvm image-set` / `chutes-cvm config` / `chutes-cvm vfio-wedged`** — the
  image-set manifest tool, the config renderer, and the PCI-passthrough-wedged check are
  now first-class subcommands, so every caller routes through the one console script.
- **`host-tools/scripts/generate-operator-signing-key.sh`** — standalone helper to mint an
  RC-gate operator RSA key pair for testing. Writes the PRIVATE key (referenced from
  `config.yaml` as `rc.operator_signing_key`) and the matching PUBLIC key (to register with the
  Chutes API's accepted RC measurement). Thin `openssl` wrapper (`genpkey` + `pkey -pubout`) whose
  keys are compatible with the initramfs `rc-sign` signer and the API verifier
  (`openssl dgst -sha256 -sign`/`-verify`); it writes no config and registers nothing.

### Changed
- Pin host kernel to `linux-image-6.17.0-35-generic` in both Ubuntu 25.10 and
  26.04 host profiles to guarantee RTMR0 measurement consistency across the
  fleet. Previously the `linux-image-generic` metapackage was used, which
  allowed hosts to silently diverge after routine apt upgrades, causing
  attestation failures.
- Host setup now enforces that `kernel_package` is a pinned versioned package
  and rejects metapackages (e.g. `linux-image-generic`) at startup.
- `_get_kernel_version()` in host setup now handles pinned kernel package names
  directly instead of requiring `apt show` resolution via Depends.
- Pin SMBIOS type 1/2/3 (system/baseboard/chassis identity) to static values in
  the QEMU launch. TDVF folds the fw_cfg `etc/smbios/smbios-tables` blob into
  RTMR0, so motherboard-identity fields previously made two servers of the same
  profile produce different RTMR0 values; pinning them removes that per-server
  drift. This does not make RTMR0 host-independent — type 4/17 (processor/memory)
  tables still vary with `-smp`/`-m`/topology, absorbed by the per-profile
  measurement baseline, and type 0 (BIOS) is not overridden.
- Apply the same SMBIOS pinning in `extract-acpi.sh` so the extracted golden
  RTMR0 matches what is launched.
- The TDX launcher now **direct-boots** the guest (1.4.0+, required — no GRUB fallback):
  OVMF boots the image's kernel/initrd directly via QEMU `-kernel`/`-initrd`/`-append`
  instead of GRUB, dropping GRUB/shim from the measured boot chain (TCB reduction).
  `build_base_cmd` always emits the direct-boot args and drops `bootindex` from the disk
  device — the qcow2 stays attached as the LUKS root, just not the boot device. There is
  deliberately no GRUB path: a second boot method would produce a second,
  network-inconsistent set of measurements. The offline ACPI-dump path passes placeholders
  (RTMR0 is boot-method independent).
- Direct-boot artifacts (`<image>.vmlinuz` / `.initrd` / `.cmdline`) are produced once at
  build time and published to R2 alongside the qcow2. `quick-launch --download` /
  `--download-debug` fetch them next to the image, and `chutes.guest.direct_boot` resolves
  them at launch — no per-launch extraction and no `guestfish` on fleet hosts. The launcher
  and the build read the *same* staged files, so the pinned RTMR1/2 match the running VM.
- `base_image` is a published **image-set directory** (qcow2 + boot artifacts +
  `manifest.json`), not a bare qcow2 — the only supported format. `quick-launch --download` /
  `--download-debug` fetch the whole set into `/var/lib/chutes/base-images/<variant>/` and
  verify it; the build (ansible) emits the per-variant `manifest.json` for both debug and
  prod. Keeping an old build means moving its directory aside before re-downloading
  (downloads overwrite in place).
- Launch is decoupled from download: a missing image set fails with a clear remediation
  message rather than being auto-downloaded. Stage sets explicitly with `--download` (or, in
  a build, via ansible).
- Base-image integrity is carried entirely by the manifest instead of a hand-maintained
  `EXPECTED_BASE_SHA256` (removed) — no per-release hash bump, no per-launch re-hash of the
  multi-GB image, and per-variant shas for debug and prod (the old single constant could
  represent only one).
- The 25.10 → 26.04 upgrade path is untouched: `upgrade-host.yml`, the
  `os_upgrade_path` hops, and the `pre_2510` / `init_2604` hooks all still run, so
  existing hosts can advance. 25.10 is now an upgrade waypoint only — from 25.04
  always run with `-e target_version=26.04`, since a run whose final hop is 25.10 is
  refused by the `verify-host` pre-flight (no baselined QEMU for that release).
- **Consolidated the host entrypoint scripts into the `chutes-cvm` CLI.** The thin wrapper
  scripts `run-td`, `verify-host`, `setup-tdx-host`, `tune-host.sh`, `restore-host.sh` and the
  `host-tools/bin/chutes-*` PATH delegators are removed; their operations are now `chutes-cvm`
  subcommands: `launch`, `verify-host`, `setup-host`, `tune-host`, `restore-host`, `reset-gpus`
  (plus `discover-profile`). Logic still lives in the `chutes.guest` / `chutes.host` modules;
  the CLI is a thin front door. `discover-profile.sh` is deliberately kept as a standalone
  script. Ansible invokes the bootstrap-free `python3 -m chutes.guest.cli <cmd>` form (no venv
  needed); host setup installs the `chutes-cvm` shim via `setup-chutes-cvm.sh` instead of
  symlinking `bin/`.
- **`chutes-cvm` is now a real Python package under `src/chutes-cvm/`** (import
  `chutes_cvm`, published to PyPI), instead of a loose module tree on `PYTHONPATH` at
  `host-tools/scripts/chutes/`. The rename from `chutes` to `chutes_cvm` avoids colliding
  with the Chutes platform SDK once installed. The offline measurement engine
  (`guest-tools/measurement/*.py`) moved into `chutes_cvm.measurement`, dropping its
  `sys.path` shims.
- **Host provisioning installs the package** — `host_tools` now runs `setup-chutes-cvm.sh`,
  which `pip install -e`'s the package into a venv and puts the `chutes-cvm` console script on
  PATH (with its deps: pyyaml/jsonschema/substrate-interface). Host ansible + `quick-launch.sh`
  call `chutes-cvm <command>` instead of `python3 -m chutes.guest.*`, so dependency-bearing
  commands (`config`, and the upcoming `preflight`) run with their deps available. The sparse
  checkout now includes `src/chutes-cvm/`. Guest image build keeps `PYTHONPATH` (stdlib commands
  only). Set `CHUTES_CVM_PYPI=1` to install from PyPI instead of the checkout.
- `host-tools/scripts/prepare-vm-image.sh`: replaced QEMU qcow2 overlay creation with a full `cp` of the base image into a per-VM file; added stale-image cleanup for previous base image versions.
- `host-tools/scripts/quick-launch.sh`: renamed `--overlay-dir` to `--vm-image-dir`; default directory changed from `/var/lib/chutes/vm-overlays/` to `/var/lib/chutes/vm-images/`.
- Config key `overlay_directory` renamed to `vm_image_directory` in schemas, templates, example configs, `config.py`, and `CONFIG-GUIDE.md`.
- The host setup guide no longer tells operators to print a topology matrix from the repo.
  That command was removed along with the built-in matrix it read; host support is now
  determined by querying the host directly rather than by consulting a checked-in table.

### Fixed
- 25.10 → 26.04 host upgrade no longer stalls on `sgx-dcap-pccs`. Intel's
  `noble`-suite PCCS now depends on `nodejs (>= 22.13)`, which is unsatisfiable
  on 25.10 (questing ships nodejs 20.x), so apt parks it as permanently
  "kept back" and `do-release-upgrade` refuses to proceed (holding the package
  does not help — `do-release-upgrade` also refuses with held packages).
  `pre_2510` now backs up the PCCS artifacts (`config/`, `ssl_key/` — API key,
  token hashes, TLS cert, cached collateral) and removes the package before the
  upgrade; a new `init_2604` hook restores them on the upgraded OS *before*
  `setup-tdx-host` reinstalls PCCS from the `resolute` suite, so the reinstall's
  post-install brings the service up already configured and registration is
  preserved without manual steps. Adds a target-keyed `init_<version>` hook
  phase to the os_upgrade role (runs on the new OS after reboot, before
  `setup-tdx-host`).
- Add `xfsprogs` to host prerequisites so `mkfs.xfs` is available when `create-cache.sh` creates the storage volume (regression introduced in #34 when the storage volume format was switched from ext4 to XFS)

### Removed
- The bare-qcow2 launch path and `quick-launch --skip-checksum`. Every image — including
  benchmark and custom images — is consumed as a verified image set.
- **Ubuntu 25.10 host support — 26.04 is the only supported host OS.**
  - `support_matrix.py` and the host-tools README table list 26.04 only (H200 /
    B200 / RTX Pro 6000, 8-GPU); the three `25.10` rows are gone, so
    `setup-tdx-host --topology-matrix` shows 26.04 exclusively.
  - `Ubuntu2510Profile` and its `HOST_PROFILES["25.10"]` entry are deleted:
    `setup-tdx-host` now fails with "Unsupported Ubuntu version" on a 25.10 host
    instead of provisioning it.
  - `SUPPORTED_QEMU_BY_OS` drops `25.10 -> 10.1.0`, and the `"10.1.0"` keys are
    removed from `baselined_measurements` (H200, B200_XEON6, RTX_PRO_6000). A host
    still on 25.10 is refused at launch by the QEMU host-readiness gate, and
    `verify-host` reports BLOCKED. This also retires the fingerprints that were
    only ever baselined at 10.1.0 — flat-path H200 (>2 NUMA nodes) and SNC3
    B200_XEON6 — since neither has a registered 10.2.1 RTMR0 and so could not
    attest on 26.04 anyway.

## [2026.09.0] - 2026-09-03

### Added
- **`B200_XEON6_256` GPU profile** — B200 on a 2×64c×2t Xeon 6 host (256 logical
  CPUs, ~3 TB RAM, SNC off → 2 NUMA nodes, GPUs 4+4). A submitted host profile of
  this class (KR9288-X3 board, Ubuntu 26.04 / QEMU 10.2.1) matched neither existing
  B200 sibling — device ID `2901` is disambiguated by exact host CPU count, and the
  registry only knew 192 (`B200`) and 288 (`B200_XEON6`) — so `_match_gpu_model`
  raised *"Add a new profile for this CPU topology"* and the host could not launch
  at all. The new profile is a third `B200Profile` sibling with `host_cpus = 256`:
  240 vcpus (`240,sockets=2,cores=120,threads=1`), and the same
  `ram_per_gpu_gb = 369` (guest RAM 2952G) as `B200_XEON6`. All GPU and passthrough
  policy — CC mode, no NVSwitch/IB passthrough, guest NUMA, post-launch tuning,
  Fabric Manager requirement — is inherited from `B200Profile` unchanged.
  `baselined_measurements` is intentionally left empty: the 240-vcpu `-smp` moves
  RTMR0 and no measurement is registered for this host class yet. An empty map
  skips the launch-time topology hard-match (so the host launches) while
  `verify-host` still returns WARNING rather than claiming a baseline that does not
  exist — which also means `upgrade-host.yml`'s pre-flight will abort on this host
  until the measurement lands (override with `upgrade_preflight_override=true`).
  Populate it with `{"10.2.1": {NumaTopology(gpu_nodes=(0, 0, 0, 0, 1, 1, 1, 1))}}`
  once the RTMR0 for this profile is registered in chutes-ops `teeMeasurements`.

## [2026.07.3] - 2026-07-24

### Changed
- Reconciled the validated host-topology matrix (`support_matrix.py` and the
  host-tools / ansible READMEs). H200, B200, and RTX Pro 6000 (all 8-GPU) are now
  listed as validated on both Ubuntu 25.10 and 26.04. Removed the stale
  `25.04 / H200` (EOL OS) and `26.04 / B300` (not yet validated end-to-end) rows.

## [2026.07.2] - 2026-07-09

### Added
- VM launch now hard-matches the host's RTMR0-impacting configuration before launching, so an unbaselined host fails fast with an actionable message instead of a silent attestation 403 later. Two checks:
  - **QEMU host-readiness gate** (`verify_host_qemu_supported`, run before profile resolution): the host QEMU must be the build its OS release ships (`SUPPORTED_QEMU_BY_OS = {"25.10": "10.1.0", "26.04": "10.2.1"}`). QEMU generates the guest ACPI tables measured into RTMR0, so a different QEMU attests with an unbaselined RTMR0; tying it to the OS also enforces hosts run the release's security-patched build. Proactive operator check, not a security boundary — the real gate is the control-plane RTMR0 match.
  - **Topology hard-match** in `detect_profile`: each `GpuProfile` declares `baselined_measurements`, a map of `QEMU version → set of host topology fingerprints` (`detection.host_topology_fingerprint`) with a registered RTMR0 measurement (RTMR0 = f(topology, QEMU), so a measurement is only valid for a specific topology×QEMU pair). `baselined_topologies` derives from it as the union across QEMU versions. Fingerprints are self-documenting value types in the new `chutes.guest.gpu.topology` module (see below): `NumaTopology(gpu_nodes, nvswitch_nodes, ib_nodes)` on the 2-node NUMA path (where each device→NUMA vector drives the guest PXB-PCIe grouping and thus RTMR0) or `FlatTopology(gpu_count, nvswitch_count, ib_count)` otherwise. The launch-time hard-match is deliberately QEMU-agnostic (uses the union): a host whose live topology isn't characterized at all is refused with "run discover-profile.sh and send the output." Populated for H200 / B200 / B200_XEON6 / RTX_PRO_6000 to mirror the authoritative chutes-ops `teeMeasurements` v1.3.1 (`values/chutes-api/values.yaml`). An empty map (e.g. B300, not yet characterized in sek8s) skips the launch check; verify-host will advise on such hosts since it can't confirm the measurement.
- **`chutes.guest.gpu.topology` module** — `NumaTopology` / `FlatTopology` frozen dataclasses that name every field of a topology fingerprint (per-device host-NUMA vectors vs device counts), replacing the previous opaque positional tuples (`("numa", …)` / `("flat", …)`). They are hashable value types, so they live in the `baselined_measurements` sets and support `fingerprint in baselined_topologies`; a `NumaTopology` never equals a `FlatTopology`, which is the guest-NUMA-path vs flat-fallback discriminator the string tag used to carry. Only *device* topology is captured — CPU/socket/RAM are pinned to profile constants and identical across a profile's hosts, so a host that differs only in NUMA-node count (the RTX 4-node case) or logical-CPU count is characterized by its topology, not named after the host.
- **`chutes.guest.verify` — standalone host-readiness check** (`verify-host` CLI, mirrors `run-td`): runs the launch gates without launching a VM, so it can be run **before an upgrade** to confirm a node will relaunch and re-attest rather than going offline. Exit codes: `0` ready, `1` blocked (QEMU wrong for OS, or topology uncharacterized — won't relaunch), `2` warning (gates pass but no registered measurement for this topology×QEMU — would 403 at attestation). `--target-os VERSION_ID` checks against the QEMU an OS upgrade would bring (skipping the live-QEMU hygiene gate, since the upgrade replaces it) so an OS upgrade can be pre-flighted. The QEMU-keyed `baselined_measurements` is what lets it distinguish "OS/QEMU is supported" from "we actually have a measurement for it" — the case where an H200 on 26.04/10.2.1 resolves and gates cleanly but has no registered 10.2.1 RTMR0.
- **`upgrade-host.yml` pre-flight gate**: runs `verify-host --target-os <final hop>` after computing the upgrade path and **before draining/shutting down the guest**; aborts the upgrade (unless `upgrade_preflight_override=true`) if the host wouldn't relaunch or attest at the target OS's QEMU — so an OS upgrade can't strand a node whose topology×QEMU has no registered measurement.
- **RTX Pro 6000 4-NUMA-node host support**: added the flat-fallback fingerprint `FlatTopology(gpu_count=8)` at QEMU `10.2.1` to `RTXPro6000Profile.baselined_measurements`. Hosts with more than 2 NUMA nodes fail `use_numa_topology`'s 2-node gate and launch on the flat path (single memory-backend, no PXB-PCIe grouping), which is a distinct guest topology → distinct RTMR0 from the 2-node NUMA hosts. The RTX entries are keyed under `10.2.1` (Ubuntu 26.04, confirmed by `discover-profile.sh` on `se-028` and `tlusa-9`), with the prior `10.1.0` numa entry retained for RTX hosts still on 25.10. **The matching RTMR0 for RTX flat @ 10.2.1 must be registered in chutes-ops `teeMeasurements` before this host can attest — the profile carries the fingerprint; the measurement follows.**
- `discover-profile.sh`: capture per-NVSwitch host NUMA node (`nvswitch.numa_nodes` in JSON, plus a report row) — the field that distinguishes otherwise-identical H200 chassis whose NVSwitches attach to a different NUMA node (e.g. Dell XE9680 node 1 vs KR6288 node 0), which changes RTMR0.
- `discover-profile.sh`: capture per-IB-PF host NUMA node for the passthrough candidates (`nic.passthrough_numa_nodes` in JSON, plus a report row) — retained as a diagnostic. The topology fingerprint keeps an IB axis (`ib_nodes` / `ib_count`) wired to `should_passthrough_infiniband`, so it is empty for every profile now that IB passthrough is removed (see Removed) but would automatically capture IB again if any profile re-enabled it.

### Fixed
- `nvidia-gpu-tools` (and `chutes-reset-gpus`) now self-heal after a host OS
  upgrade that changes the system Python (e.g. 25.10 → 26.04, Python 3.13 →
  3.14). `ensure_gpu_tools_available()` verifies the CLI actually runs instead
  of trusting its presence on `PATH`, and rebuilds the bundled-wheel venv when
  it was built for a different Python version. Previously the orphaned venv left
  the CLI broken with `ModuleNotFoundError: No module named 'entry_point'` — and
  re-running `setup-tdx-host` did not fix it because the stale symlink still
  resolved on `PATH`.

### Removed
- **InfiniBand passthrough for B200 / B200_XEON6** (`should_passthrough_infiniband` → `False`, matching H200/B300/RTX). It added no value — guest networking is virtio-net and NVLink fabric is host-side Fabric Manager (which works with IB off). Its only effect was to make RTMR0 vary by each host's IB NIC loadout , forcing a separate measurement per loadout. With IB off, every B200 converges to one fingerprint `NumaTopology(gpu_nodes=(0,0,0,0,1,1,1,1))`. The new no-IB RTMR0 is submitted to chutes-ops `teeMeasurements` after the fact (the profile carries the fingerprint; the measurement follows).

## [2026.07.1] - 2026-07-01

### Changed
- **Per-profile host CPU reserve.** `HOST_RESERVED_CPUS` is no longer a single
  global constant applied to every GPU profile. `GpuProfile` now exposes a
  per-profile `host_reserved_cpus` property (default 4) that feeds
  `vcpus = host_cpus - host_reserved_cpus`. This lets a GPU type with a heavier
  fixed host workload reserve more cores without shifting the vcpu count — and
  therefore the RTMR0 measurement — of unrelated profiles.

### Fixed
- **B200/B200_XEON6 reserve 16 host CPUs** (up from the default 4), leaving 176
  vcpus on the 192-thread B200 (8 physical cores, 4/socket; clean 88 cores/socket
  topology). The B200 host runs FabricManager alongside QEMU's iothread/vhost
  workers; a thin 4-CPU reserve starved those threads under heavy NVLink/NCCL
  I/O, which surfaced in the guest as `cudaErrorNvlinkUncorrectable` during
  distributed init. The wider reserve also enlarges the CPU gap that QEMU
  iothreads pin into (`post_launch.py`).
  **Attestation impact:** changing B200/B200_XEON6 vcpus changes their `-smp`
  topology and thus RTMR0. The B200 and B200_XEON6 attestation baselines must be
  re-measured. H200, RTX_PRO_6000, and B300 keep the default reserve of 4 and are
  byte-identical — no re-baseline needed for those.

## [2026.07.0] - 2026-07-01

### Changed
- `upgrade-guest.yml` no longer hashes the multi-GB base qcow2 to decide whether
  a host needs upgrading. It reads `needs_upgrade` per server from
  `chutes-miner tee maintenance-status --raw-json` and ends already-current hosts
  immediately (no download, no hash), so running with no `--limit` safely walks
  the whole fleet and only touches hosts behind the active window's target
  version. When an upgrade is needed the image is fetched once and verified by
  aria2 itself (`--checksum` against the repo-pinned `EXPECTED_BASE_SHA256`).

### Fixed
- Guest upgrade/remediation no longer marches into a cryptic
  `qemu-nbd: Failed to get "write" lock` when the guest fails to power down.
  `shutdown_via_miner.yml` now escalates SIGTERM→SIGKILL to a stuck `chutes-td`
  QEMU (`stop_chutes_td.sh`) and, if it survives SIGKILL (uninterruptible
  D-state), fails loudly instructing the operator to reboot the host.
- `create-config.sh` now recovers a stale `qemu-nbd` holding the config image
  (leftover from an interrupted run) and retries once, and otherwise reports the
  remaining holders (lsof/fuser) instead of failing with an opaque lock error.
- The force-evict path (`drain_and_shutdown.yml`, `force_upgrade=true`) and
  `shutdown.yml` now auto-confirm the `sync-kubeconfig` "Continue? [y/N]" prompt
  (`stdin: "y\n"`), matching the normal drain path. Previously these aborted with
  `Aborted.` / non-zero return code.
- VM launch (`launch_and_verify.yml`) now guards against a wedged GPU/PCI
  passthrough subsystem (stuck vfio-pci/nvidia-gpu-tools D-state tasks that make
  `run-td` fail with "SBR cannot run until the host is rebooted"). It pre-flight
  detects the wedge via `chutes.guest.vfio.pci_operations_wedged()`, and if the
  launch itself wedges the host it reboots, waits for SSH, and retries the launch
  once. Tunable via `upgrade_reboot_timeout_seconds` (default 1800).
- `chutes_vm_config` now reads the public interface from `ansible_facts.default_ipv4.interface`
  instead of the injected `ansible_default_ipv4.interface` var, so public-interface
  re-detection (and the bridge-network assert) still works when `inject_facts_as_vars`
  is disabled.
- The wedge-recovery reboot is now a forced kernel-level SysRq reboot
  (`force_reboot.yml`) instead of a graceful one. A graceful reboot hangs
  indefinitely in `systemd-shutdown` waiting for the wedged QEMU and other
  un-killable D-state tasks (seen on the iLO console as "Waiting for process:
  ... chutes-td ..."), leaving the host stuck mid-reboot. SysRq 's' then 'b'
  syncs and reboots immediately, bypassing service/device shutdown.

## [2026.06.1] - 2026-06-30

### Added
- `discover-profile.sh`: **Host / Firmware Identity** section + JSON `host` object — baseboard model and BIOS vendor/version/date read from world-readable `/sys/class/dmi/id/*` (no root), plus product name and OS `VERSION_ID`. Pins down "identical" servers that actually differ in firmware/BIOS settings.
- `discover-profile.sh`: **Launch Determinism (RTMR0-relevant)** section + JSON `launch_determinism` object — surfaces the host-derived inputs that reshape the guest memory map / ACPI tables and therefore RTMR0: the host **QEMU version** (`qemu_version` + full distro string — QEMU generates the guest ACPI tables and TD HOB that TDVF measures, so two hosts with byte-identical launch args but different QEMU builds, e.g. Ubuntu 25.10→10.1.0 vs 26.04→10.2.1, extend different RTMR0s), NUMA node count and the `numa_topology_eligible` gate (mirrors `qemu.use_numa_topology()` — guest NUMA topology only activates on exactly 2 host NUMA nodes; any other count, e.g. Sub-NUMA Clustering, falls back to a flat map + `numactl` interleave and extends a different RTMR0), the derived QEMU `-cpu` args (avx10 mask gated on `VERSION_ID`), and the SMP topology. Diffing this section between two hosts isolates an RTMR0 divergence.
- `discover-profile.sh`: per-GPU VBIOS version (JSON `gpu.vbios`), mapped by PCI bus id so nvidia-smi index order cannot mislabel GPUs. (VBIOS feeds the GPU/nvtrust attestation path, not the TDX RTMRs.)
- `discover-profile.sh`: full `lspci -tv` PCIe topology tree (JSON `pci_topology`) — enumeration order drives PXB-PCIe root-port assignment and thus the guest's PCI bus layout.

## [2026.06.0] - 2026-06-25

### Added
- B300 Blackwell HGX GPU support for TDX VM launch (`B300Profile`: PCI device ID `3182`, 288 GiB HBM3e VRAM, 2-socket/192-vCPU topology).
- Per-profile TDVF firmware selection (`firmware_filename` property).
- `use_ovmf_mmio_fw_cfg` profile property — B300 disables per-GPU fw_cfg MMIO hints in favour of OVMF auto-sized multi-TB MMIO window.
- `get_sbr_reset_args()` profile method for Secondary Bus Reset recovery after CC-mode switch.
- PCI wedge detection (`pci_operations_wedged()`, `wait_pci_operations_idle()`) — pre-flight and post-unbind checks abort with a clear message instead of hanging when the PCI subsystem is stuck in D-state.
- Standalone host CPU tuning tool (`python -m chutes.host.tune apply|restore`, exposed as the `chutes-tune-host` / `chutes-restore-host` commands via the existing `setup-tdx-host` tool-symlink step), decoupled from VM launch — applied and reverted deliberately by the operator rather than automatically on every start/stop. Implements the NVIDIA Confidential Computing Deployment Guide (DU-12302-001) host-OS recommendation: CPU frequency governor → `performance` and C-states **C1E/C6** disabled, leaving the shallow `POLL`/`C1` states enabled for thermal headroom (it no longer disables every C-state, nor forces turbo/EPP). Pre-tuning values are snapshotted to `/var/lib/chutes/tdx-host-tuning-restore.sh`; re-running `apply` reapplies settings without overwriting the saved original state.
- Automatic GPU profile detection from host PCI/sysfs topology, with multi-GPU host support — new `detect_host_cpus()`, `detect_host_sockets()`, and `detect_numa_node_count()` helpers read CPU count, socket count, and NUMA layout from sysfs to select and verify the correct `GpuProfile`.
- `discover-profile.sh`: hardware discovery script that probes GPU topology, PCI BAR sizes, NUMA layout, CPU/memory configuration, and firmware paths — outputs a terminal report and JSON file with all values needed to verify or author a `GpuProfile` entry.
- `benchmark-hf-downloads.py`: new TDX-focused benchmark comparing XET concurrency configurations.

### Changed
- All GPU profiles now use `OVMF.inteltdx.fd` firmware (edk2-stable202605 Config-B, no Secure Boot). Addresses CVE-2025-2296 (legacy Linux loader disabled by default). Old `TDVF.fd` removed.
- `gpu-admin-tools` bumped to v2026.06.05 with hardened B300 PCI recovery.
- B300 disables InfiniBand passthrough: all ConnectX-7 IB-class PFs are NVSwitch bridge devices managed by host-side Fabric Manager; guest networking uses virtio-net.
- NVSwitch-based B200/B300 profiles now declare `requires_fabric_manager`; host setup starts (or restarts) `nvidia-fabricmanager.service` immediately rather than only enabling it, so the NVSwitch fabric is active before launch.
- InfiniBand passthrough is now optional: a host whose profile supports IB passthrough but exposes no IB devices logs a note and skips passthrough instead of aborting the launch.
- `benchmark-network.py`: removed `hf_transfer` scenario (deprecated in huggingface_hub 1.x).
- GPU profile auto-detection now refuses to guess. `_match_gpu_model` raises a `ValueError` when a host's GPU topology cannot be resolved to exactly one supported profile — no profile matches the device ID + CPU count, or the CPU count is unavailable to disambiguate a shared device ID (e.g. B200 vs B200_XEON6) — instead of falling back to the first match. An unsupported/undetermined topology has no measurement baseline (MRTD/RTMR), so launching it with a guessed profile would produce a VM that cannot attest; the caller must surface the error. Added `_is_known_gpu` for recognition-only detection (used by `detect_nvidia_gpus`), which never raises on a shared device ID.
- NUMA profiles (H200/B200/B300) now back guest RAM with one `memory-backend-ram` per host NUMA node, each bound via `host-nodes=…,policy=bind` for NUMA locality. These backends deliberately do **not** set `prealloc=on`: under TDX the guest's RAM is private memory served lazily from `guest_memfd`, so preallocating would pin a second full copy of pages the guest never uses as shared (~2× guest RAM) and OOM-kill QEMU as a pod warms up.

### Fixed
- VM launch now aborts with a clear error instead of OOM-killing the host when a profile's fixed guest RAM cannot physically fit (host RAM minus reserve). Guest RAM stays a fixed, profile-determined value (it feeds the guest ACPI tables and thus TDX measurements), so this check never resizes the VM.
- Fabric Manager `PARTITION_RAIL_POLICY` is now set to the string `symmetric` required by FM 595+ instead of the numeric `1` that newer FM rejects (CC mode would otherwise stay silently disabled on Blackwell).
- Pinned the Fabric Manager package to `595.71.05-0ubuntu0.26.04.1` (full distro-qualified version) to match the guest driver pin and stop apt from installing a mismatched build.
- VM launch no longer rejects large-RAM profiles whose guest RAM actually fits. The `safe_vm_mem_gb` host-RAM reserve was 12% of host RAM, which on 2-3 TB GPU hosts reserved 240-360 GB — far more than the ~64 GB the `GpuProfile`s are sized against (`ram_per_gpu_gb ~= (host_RAM - 64) / gpus`). This wrongly aborted `B200_XEON6` (8x369=2952G on a 3017G host) and `B200` (8x243=1944G on a ~2 TB host) launches with a misleading "host has only NG" error. The reserve is now a flat 64 GB (`VM_MEM_RESERVE_GB`) matching the profiles' sizing convention, rather than a percentage — real host overhead (TDX PAMT ~0.4% of guest, page tables, host OS, QEMU) is well under 64 GB, which covers PAMT for guests up to ~16 TB. The abort message now reports the actual safe-backable size and the reason, instead of implying the host lacks RAM.
- Host GPU-driver blacklist now also blocks `nova_core` (and `nvidiafb`). `nova_core` is the in-tree Rust NVIDIA driver shipped on recent kernels (Ubuntu 25.10/26.04); it matches the GPU PCI ID and was not blacklisted, so it could auto-load and re-claim a GPU after the CC/PPCIe-mode reset re-enumerated it — leaving the device on `nova_core` instead of vfio-pci and causing the launch-time bind failure on 26.04 hosts. `setup.py` now blacklists it and unloads it (and `nvidiafb`) immediately alongside `nouveau`.
- GPU passthrough now verifies each device actually bound to vfio-pci before launching QEMU. `bind_explicit_devices_to_vfio` previously printed success unconditionally and never read back, so a GPU that failed to bind — still re-enumerating after its CC/PPCIe-mode reset, dropped to D3 when power control was restored to auto, or re-claimed by the nvidia driver — sailed through to a cryptic `qemu-system-x86_64: vfio ...: couldn't open .../vfio-dev: No such file or directory` failure. It now polls each device until it exposes a `vfio-dev` cdev (re-probing stragglers, which also re-unbinds a driver that re-grabbed the device), and aborts with a clear, actionable error naming the unbound device(s) and their current driver if any never bind.

## [2026.05.2] - 2026-05-29

### Added
- B200 GPU support: host-side Fabric Manager setup in `chutes.host.setup` — detects B200 GPUs at runtime and installs `nvidia-fabricmanager`, `nvlsm`, `libibumad3`, `infiniband-diags`, configures `ib_umad` autoload and `PARTITION_RAIL_POLICY=1` in `fabricmanager.cfg`, and enables `nvidia-fabricmanager.service`.
- `detect_cx7_bridge_pfs()` in `chutes.guest.detection` — identifies ConnectX-7 NVSwitch bridge PFs via VPD `SMDL=SW_MNG` field so they can be excluded from guest passthrough.
- CX7 NVSwitch bridge PF exclusion in `setup_passthrough()` — bridge PFs are logged and excluded before IB NIC VFs are created, preventing accidental VFIO binding of FM-managed devices.
- `(25.10, B200, 8)` added to validated topology matrix in `support_matrix.py`.
- `ansible/host/roles/ntp` — new Ansible role that installs chrony, masks `systemd-timesyncd`, and configures `makestep 1 -1` so large clock offsets (e.g. BMC RTC set ahead) are stepped immediately on first boot rather than slowly slewed. Wired as the first role in `setup.yml`.
- `playbooks/launch.yml` now verifies host clock offset is within 5 seconds via `chronyc tracking` before launching the VM. A skewed host clock causes VMs to boot with the wrong time, which breaks boot-time mTLS cert validation at the attestation endpoint.

### Changed
- `B200Profile` corrected: `host_cpus=192`, `host_sockets=2`, `bar_size_mb=262144` (256 GiB BAR confirmed from hardware).
- `detect_infiniband_pfs()` now accepts an optional `exclude_bdfs` parameter.

### Fixed
- `ansible/host/roles/pccs_configure`: fixed automated SGX platform registration via `PCKIDRetrievalTool`.
  Two bugs were addressed:
  1. `PCKIDRetrievalTool` was piped a password via stdin which the tool ignores (reads from `/dev/tty`);
     replaced with the `-user_token` CLI flag.
  2. PCCS ≤1.26 intentionally downgrades Intel PCS requests for QE/QVE identity and TCB to v3 when a
     v3-format response is needed, but Intel retired the v3 PCS API in 2026 (returns HTTP 410). This caused
     PCCS to return 404 to `PCKIDRetrievalTool` before its own v4 retry completed. A patch task now removes
     the two downgrade blocks from `pcs_client.js` so PCCS always uses v4; the data format is identical and
     v3 PCCS API clients continue to work. Upstream fix submitted as
     [intel/confidential-computing.tee.dcap.pccs#52](https://github.com/intel/confidential-computing.tee.dcap.pccs/pull/52).
     The patch task will be removed once that PR is merged and a fixed package ships.

## [2026.05.1] - 2026-05-21

### Fixed
- `drain_and_shutdown.yml`: pass `stdin: "y\n"` to the CLI drain command to satisfy the confirmation prompt introduced in the latest CLI version.
- `shutdown_via_miner.yml`: replace serial-log grep for "Power down" with `is_live_chutes_td.sh` script polling; loop now succeeds when the QEMU guest process exits (`rc != 0`) rather than waiting for a log message that may not appear.

## [2026.05.0] - 2026-05-18

### Added
- `ansible/host/playbooks/benchmark-setup.yml`: idempotent host-side setup playbook for benchmark VMs — syncs host-tools, installs `conntrack`, deploys the `benchmark-netlog` service, and starts logging. Safe to run while the VM is running; image build and launch remain manual steps.
- `ansible/host/roles/benchmark_vm`: single host role covering all benchmark VM host-side infrastructure. Derives the bridge subnet from `config.yaml` (or an explicit `benchmark_vm_bridge_subnet` override), installs `conntrack`, deploys `benchmark-netlog.sh` / `.service` / `.logrotate` from the synced host-tools checkout, writes `/etc/chutes/benchmark-netlog.env`, and enables + starts the service.
- `host-tools/scripts/network/benchmark-netlog.sh` / `.service` / `.logrotate`: host-side systemd service that streams `conntrack` events for the VM bridge subnet to daily log files under `/var/log/chutes/benchmark-netlog/`.
- `quick-launch.sh --benchmark` flag: sets benchmark defaults, skips cache volume, creates a network/hostname config volume, manages the `benchmark-netlog` service lifecycle, and validates config against `config-schema.benchmark.json`.
- `host-tools/scripts/config/config-schema.benchmark.json`: dedicated JSON schema for benchmark VM launch configs — omits `miner`, `volumes.cache`, `volumes.config`, and `docker_hub` fields which are not applicable.
- `host-tools/scripts/config/config.benchmark.example.yaml`: ready-to-use benchmark launch config template.
- **GPU profile SMP topology**: `GpuProfile` now derives an accurate `smp_topology`
  string from `host_cpus` and `host_sockets` and passes it to QEMU via `-smp`. This
  matches the physical NUMA topology of the host CPU, improving vCPU scheduling.
  Profile authors only need to set `host_cpus` and `host_sockets`; `vcpus` and
  `smp_topology` are derived automatically.
- **`chutes.guest launch --ssh` flag**: SSH login hint is now opt-in via `--ssh`
  rather than always printed, keeping standard launch output clean.
- **Benchmark config validation**: `chutes.guest.config` accepts a `--benchmark`
  flag to validate against the benchmark-specific JSON schema instead of the default
  miner schema.
- **Ansible timing callbacks**: `ansible.cfg` now enables
  `ansible.posix.timer` and `ansible.posix.profile_tasks` callbacks, surfacing
  per-task timing in playbook output.

### Changed
- `chutes_vm_config` role: `base_image` and `overlay_directory` in `config.yaml` are now driven by `chutes_vm_base_image` and `chutes_vm_overlay_directory` Ansible variables (both default to `""`, preserving the existing behaviour of letting host-tools use its built-in defaults).
- `create-config.sh`: miner SS58/seed arguments are now optional — both must be provided together or both left empty, so benchmark config volumes can be created without miner credentials using the same script.
- `ansible/guest/roles/k3s`: absorbed all k3s/k8s prerequisite tasks previously scattered across the `common` and `security` roles — k3s networking (UFW rules, iptables compatibility), k3s directory/registry config, k8s tooling, helm install, and seccomp profiles now live entirely within the `k3s` role. Playbooks compose roles without build-type flags.
- `ansible/guest/roles/common`: slimmed to base system setup only (`system.yml`, `mirror.yml`, `container-networking.yml`). Container networking tasks (br_netfilter, overlay, bridge sysctl, AppArmor) kept here as they support Docker in all builds; renamed `kubernetes.conf` module persistence file to `container-modules.conf`.
- `ansible/guest/roles/security`: removed `seccomp-profiles` tasks and files; role now only performs chroot/init hardening and cloud-init disabling, unconditionally.
- `ansible/guest/inventory.yml`: removed `benchmark_build` flag — build type is now determined entirely by playbook selection.
- `ansible/guest/inventory.yml`, `ansible/guest/playbooks/tee-gpu-vm.yml`, `ansible/guest/roles/setup-ssh-access/`: renamed `benchmark_ssh_keys` to `guest_ssh_keys`.
- **build-setup playbook**: Installs `ansible` package via apt and adds the
  `host_prerequisites` role, simplifying first-time host preparation.
- **upgrade-guest playbook**: Passes the pre-validated image SHA to the
  `launch_and_verify` task when upgrading from a freshly-downloaded image, avoiding
  a redundant SHA recomputation.
- `pre_2510.yml`: adds explicit gating so SGX/DCAP packages that were updated via `unattended-upgrades` are preserved rather than purged during the 25.04 → 25.10 hop; only packages that were not updated get removed

### Fixed
- `passthrough.py` (`_run_gpu_tools`): GPU tools commands now run with a 120-second timeout and gracefully handle `TimeoutExpired`/`CalledProcessError` on reset failure, preventing indefinite hangs when a GPU is wedged at the PCIe level during VM teardown or reboot.
- `reset-gpus.sh`: aligned with updated `passthrough.py` error handling to avoid host lockups on stuck device resets.
- `pre_2504.yml`: `grub-common` is now pinned/held before the OS upgrade begins to prevent apt from purging it during dependency resolution — loss of `grub-common` left hosts unbootable
- `pre_2504.yml` / `hop.yml` / `post_2504.yml`: corrected task ordering so DKMS kernel module rebuilds and `grub-pc` reconfiguration occur in the right sequence for both fresh 25.04 installs and the 25.04 → 25.10 hop, eliminating boot failures caused by stale module state
- QEMU was always launched with `sockets=1` regardless of the physical host topology. On 2-socket servers (H200, RTX PRO 6000) this caused QEMU to emit a degenerate CPUID with no core or package topology levels, triggering the kernel `arch topology borken` warning on every vCPU at boot and presenting a misleading scheduler topology to workloads inside the VM

## [2026-05-07]

### Added
- `remediate-host.yml` playbook: remediates an existing TDX host in-place without an OS upgrade; detects the current Ubuntu version and runs the matching `pre_<version>.yml` cleanup hook (shared with `upgrade-host.yml`), then re-provisions via `host_prerequisites` → `host_tools` → `tdx_bootstrap` → `pccs_configure`
- `setup-tdx-host --noninteractive` flag: suppresses apt/debconf prompts when invoked by Ansible so `pccs_configure` can handle PCCS configuration externally; manual runs remain interactive so the PCCS debconf prompts work as normal
- `upgrade-guest.yml`: `relaunch_vm` flag (default `true`) — set `false` to promote the image and leave the VM down (e.g. when chaining with `remediate-host.yml`)
- `upgrade-guest.yml` / `remediate-host.yml`: `force_upgrade` flag — when the validator denies maintenance (e.g. `sole_survivor`), calls `purge-server` to notify the control plane, waits up to 5 minutes for natural drain, force-evicts any remaining pods, then retries maintenance
- `drain_and_shutdown`: human-readable maintenance-denial message surfacing reason and blocking instance IDs instead of a raw JSON dump
- `launch_and_verify`: pre-flight SHA256 check against the expected hash from `quick-launch.sh` before attempting VM start; fails with a clear message pointing to `upgrade-guest.yml` when the image is stale
- `host_prerequisites`: installs `git` so `host_tools` (which runs `git fetch`) always has it available

### Changed
- `setup.yml` and `remediate-host.yml`: `host_prerequisites` now runs before `host_tools` (correct ordering — prerequisites must be installed before the repo sync that depends on them)
- `pccs_configure` role: replaced purge+reinstall-on-broken-node_modules logic with an explicit `npm install --prefer-offline` task; purge+reinstall is now reserved for the case where `package.json` itself is absent
- `pccs_configure` role: generates TLS certificate as `file.crt` (was `certificate.pem`) to match the filename PCCS expects by default
- `pre_2510.yml`: removes `/opt/intel/sgx-dcap-pccs/` directory after purging packages so the reinstall starts with a clean slate and `npm install` runs correctly
- `setup-tdx-host` / `setup.py`: removed `_purge_conflicting_sgx_packages()` — migration cleanup is a host-upgrade concern owned by `pre_2510.yml`, not the vanilla-OS setup script

### Fixed
- PCCS service failing to start after non-interactive install: `sgx-dcap-pccs` Debian post-install only runs `npm install` during interactive debconf prompts; non-interactive installs silently skipped it, leaving `node_modules/` empty
- PCCS TLS: `ssl_key/certificate.pem` generated by the role was ignored by PCCS which hardcodes `ssl_key/file.crt`

## [2026-05-06]

### Fixed
- `setup-tdx-host` now configures QGS for vsock mode (`port = 4050` in `/etc/qgs.conf`) on all hosts; the Intel-shipped default leaves this commented out, silently breaking TDX quote generation in VMs
- `setup-tdx-host` now sets `use_secure_cert: false` in `/etc/sgx_default_qcnl.conf` so QGS can reach the local PCCS instance which uses a self-signed certificate

## [2026-05-05]

### Added
- Layered guest image build caching: `prepared_img_path` (offline kernel install via `virt-customize`) and `gpu_img_path` (NVIDIA drivers + CUDA) checkpoint layers using `virsh suspend`/`resume`. K3s version bumps now resume from `gpu_img_path`, skipping the expensive NVIDIA driver reinstall.
- `save-checkpoint` Ansible role: reusable virsh suspend → disk copy → resume sequence; restarts `systemd-networkd` and `systemd-resolved` after resume to recover VM network state.
- `build-setup.yml` playbook: one-shot host preparation for VM image building — benchmarks regional apt mirrors and pins the fastest via the `host_mirror` role.
- Dynamic apt mirror selection in both host (`host_mirror` role) and guest (`common/mirror.yml`): benchmarks `archive.ubuntu.com` regional mirrors at build time; override by setting `apt_mirror` in inventory.
- `NO_CACHE` env var support: invalidates all cached image layers and forces a full rebuild from the base Ubuntu cloud image.

### Changed
- GPU role split from K3s-aware monolith: NVIDIA container runtime for K3s (`nvidia-runtime.yml`) moved into K3s role and runs conditionally when `nvidia-container-toolkit` is installed. GPU role now contains only driver/CUDA/InfiniBand setup.
- GPU checkpoint now saved after drivers only; K3s installs after the checkpoint. Previously K3s was baked into `gpu_img_path`, forcing driver reinstall on every K3s upgrade.
- Intermediate image paths (`prepared_img_path`, `gpu_img_path`) namespaced under `{{ img_dir }}/{{ build_env }}/{{ vm_version }}-*.qcow2` to prevent overwrites when managing multiple build versions on the same host.
- TDX host check in `quick-launch.sh` now checks sysfs (`/sys/module/kvm_intel/parameters/tdx=Y`) first, then `/proc/cpuinfo`, dmesg as last resort — avoids false negatives on long-running hosts where the dmesg ring buffer has rolled over.

### Fixed
-

### Removed
-

## [2026-05-04]

### Added
- `upgrade-host.yml` playbook for automated, version-aware multi-hop OS upgrades. Follows the `os_upgrade_path` registry in `group_vars/all.yml`, supports `auto_drain_vm` (default `false`) to drain/shutdown the VM before upgrading, prompts for operator confirmation, and relaunches the VM with node-health verification after the final hop.
- `os_upgrade` Ansible role with a common `hop.yml` skeleton (disk check, pre-hook, dist-upgrade, do-release-upgrade, re-provision) and version-specific pre-upgrade hooks `pre_2504.yml` and `pre_2510.yml` that remove stale kobuk PPA sources and purge old attestation packages before `apt` touches the index.
- `chutes_tee_vm/tasks/launch_and_verify.yml` shared task: renders `config.yaml`, downloads base image if missing, runs `quick-launch.sh`, and polls `node-health`. Used by `launch.yml`, `upgrade-guest.yml`, and `upgrade-host.yml`.
- `chutes_tee_vm/tasks/drain_and_shutdown.yml` shared task: idempotent maintenance-mode check, full drain sequence, and graceful VM shutdown — gated on VM liveness so re-runs skip already-completed steps.
- `os_upgrade_path` map in `group_vars/all.yml` defining supported upgrade hops (`25.04 → 25.10`, `25.10 → 26.04`).
- `os_upgrade/defaults/main.yml` with `auto_drain_vm: false` and `relaunch_vm: true` role defaults (inventory overrides both).

### Changed
- Renamed `upgrade.yml` → `upgrade-guest.yml`; updated all README and playbook references.
- `Ubuntu2510Profile`: removed kobuk attestation PPA, added Intel DCAP repo (`https://download.01.org/intel-sgx/sgx_repo/ubuntu/`, suite `noble`) via the `repos` property introduced in PR #67.
- `HostProfile` base class: added default `repos` property returning `[]` to prevent `AttributeError` when `setup_host()` iterates repos on profiles that don't define one.
- `drain_and_shutdown.yml`: added `--miner-api` flag to `start-maintenance` (was silently defaulting to `127.0.0.1:32000` regardless of inventory); added maintenance-status idempotency check; gated sync-kubeconfig, pod-drain wait, and shutdown on VM liveness to allow clean re-runs.
- `launch.yml`: simplified tasks section to use `launch_and_verify.yml`; added `chutes_hotkey_path` as a required variable.
- `upgrade-guest.yml`: replaced inline config-render/quick-launch/health-poll block with `launch_and_verify.yml`.
- `tdx_bootstrap` and post-upgrade reboot timeouts increased to 1800s (30 min); post-do-release-upgrade reboot timeout increased to 3600s (60 min) to accommodate slow first-boot fsck on large drives.
- Post-upgrade host provisioning (`host_prerequisites` + `tdx_bootstrap`) now only runs on the final hop of a multi-hop upgrade, skipping redundant provisioning of intermediate OS versions.
- `assert_not_running.yml`: `when` condition now uses `| default(1)` to handle `--check` mode where `rc` is undefined.

### Removed
- `Ubuntu2504Profile` and `KOBUK_TEAM_KEY` from `host/profiles.py`: Ubuntu 25.04 reached EOL January 2026. Hosts still on 25.04 should use `upgrade-host.yml` to advance to 25.10 or 26.04.

## [2026-04-23]

### Added
- Add firewall playbook to setup default firewall for host
- Added support to create changelog for current branchin make commands

### Changed
- Added python3 venv deps to host
- Update host tools to handle divergent branches

### Fixed
- Fixed handlign of stale venv if previous ansible run had an issue in host setup playbooks.

### Removed
-

## [2026-04-20]

### Added
- Operational Ansible under `ansible/host/` (setup, launch, upgrade, shutdown) for bare-metal TDX hosts.
- QEMU duplicate-instance guard in `quick-launch.sh`.
- `chutes_vm_config`: volume `path` variables (`chutes_volume_cache_path`, `chutes_volume_storage_path`, `chutes_volume_config_path`) now render into `config.yaml` instead of hardcoded empty strings, allowing per-host volume path overrides via inventory `host_vars` or `group_vars`.

### Changed
- Renamed guest image build Ansible directory from `ansible/k3s/` to `ansible/guest/`; VM `VERSION` path is now `ansible/guest/VERSION`.
- `chutes_vm_config`: network CIDR detection (`pick_guest_network.py`) now only runs on first launch. Subsequent runs read `bridge_ip`, `vm_ip`, and `public_interface` from the existing `config.yaml` via `slurp` + `from_yaml`, preventing a new subnet from being assigned and accumulating stale bridge IPs on `br0` on every re-run.
- `shutdown.yml`: full graceful shutdown sequence — lock server (`chutes-miner lock`), purge deployments (`chutes-miner purge-deployments`), poll until chute pods are gone, then issue shutdown and wait for power-down in the guest serial log.
- `upgrade.yml`: replaced manual `kubectl delete pods` drain block with a poll-until-empty using `--context {{ chutes_tee_server_name }}` and label `chutes/chute=true`. Validator drains pods automatically after `start-maintenance`; playbook now waits for that to complete rather than forcing deletion.
- `shutdown_via_miner.yml`: scope reduced to issue-shutdown + wait-for-power-down only. Lock and drain are the caller's responsibility, allowing `shutdown.yml` (manual drain) and `upgrade.yml` (validator-managed drain) to use different strategies.

### Fixed
- `setup.yml`: `tdx_bootstrap` now always runs `setup-tdx-host` unconditionally instead of falling back to `--install-tools-only` when the TDX module is already initialized.
- `tdx_bootstrap`: reboot detection now also compares `uname -r` against the `saved_entry` in `/boot/grub/grubenv` to avoid skipping reboots when `update-notifier-common` is absent.
- `security-verified-path-gate.yml`: `git diff` now uses `-M --diff-filter=ACM` so pure renames are not treated as new release-scoped files.
- `.gitignore`: added `.vscode/` and removed tracked IDE settings files.

