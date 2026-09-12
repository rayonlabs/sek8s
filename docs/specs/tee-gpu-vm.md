# Feature Spec: TEE GPU VM Build Profile

**Date**: 2025-07-22 (updated 2026-05-11)
**Status**: ready

---

## Context

Build a dedicated TDX VM image for private, secure benchmarking of partner
proprietary workloads under NDA. The partner operates entirely inside the
guest VM (SSH from external machine). We operate only the bare-metal host
(hypervisor, bridge/NAT, GPU passthrough, VM lifecycle, block device
attachment). No operator SSH keys are installed in the guest. The
partner controls LUKS encryption of their data volume, all software inside
the guest, and network egress for their containers.

The benchmark VM is a stripped-down variant of the existing debug build:
same TDX + PPCIe isolation, same GPU driver stack, but without k3s, sek8s,
admission control, system-manager, or any Chutes-specific orchestration.
An attestation verification script is included so the partner can
independently verify TDX and GPU confidential-computing state.

A persistent host-side network logging service provides gap-free
connection-level metadata records for NDA compliance (no TLS payloads).

- **Packages affected**: `ansible/guest/` (image build), `host-tools/scripts/` (quick-launch, network logging), new attestation verification tooling
- **Key files**:
  - `ansible/guest/inventory.yml` — build flags
  - `ansible/guest/playbooks/chutes-miner-vm.yml` — role orchestration
  - `ansible/guest/playbooks/group_vars/host.yml` — output image path
  - `ansible/guest/roles/common/tasks/main.yml` — k3s/k8s/helm prereqs
  - `ansible/guest/roles/gpu/tasks/main.yml` — GPU device + k3s setup split
  - `ansible/guest/roles/gpu/tasks/device-setup.yml` — CUDA/driver install
  - `ansible/guest/roles/gpu/tasks/k3s-setup.yml` — k3s containerd NVIDIA config
  - `ansible/guest/roles/cleanup/tasks/main.yml` — build cleanup
  - `ansible/guest/roles/attestation-service/tasks/install-tdx-quote-generator.yml` — TDX quote tools
  - `ansible/guest/roles/sek8s/tasks/install-nvevidence-cli.yml` — GPU evidence CLI
  - `host-tools/scripts/quick-launch.sh` — VM launch orchestrator
  - `src/chutes-cvm/chutes_cvm/scripts/network/setup-bridge.sh` — bridge + NAT + DNAT
  - `src/chutes-cvm/chutes_cvm/guest/qemu.py` — QEMU volume attachment
- **Dependencies**: `nv-attestation-sdk` (already in `nvevidence/`), `libtdx-attest` (already in attestation-service role), `conntrack` (already in common role system packages), `trustauthority-cli` (Intel SGX/TDX repo)

---

## Design Decisions

- **`benchmark_build: true` flag** — new Ansible variable. Setting it alone is sufficient;
  `debug_build` does not need to be set separately. Benchmark mode implicitly applies
  debug-like behavior (no LUKS on root, no harden-access, no prime-vm) by extending the
  existing `when: not debug_build` conditions on those plays to also check
  `benchmark_build`. Additionally skips all Chutes-specific orchestration (k3s, sek8s,
  gpu-verify, admission, system-manager, chutes-gpu, cache-volume, config).
- **SSH key replacement at cleanup** — the Ansible build VM is provisioned with the builder's
  SSH key via cloud-init (needed for Ansible to SSH into the build VM and run roles). Cloud-init
  is purged by the existing security/cleanup roles. A new cleanup task strips the builder's
  residual `authorized_keys` and writes only the partner-provided keys from `benchmark_ssh_keys`.
  A verification task asserts the final file contains exactly the expected keys.
- **Bridge + TAP networking** — required because the partner SSHes from an external machine
  (no host access). Existing `setup-bridge.sh` handles DNAT (public:2222 to guest:22) and
  MASQUERADE (guest outbound NAT for internet access). Extra DNAT rules for k3s/NodePorts are
  harmless (nothing listening in the benchmark image) — no changes to the bridge script.
- **Persistent host-side network logging** — a systemd service running `conntrack` in event
  mode on the bridge, logging connection metadata (timestamps, 5-tuple, byte/packet counts)
  to rotated log files. No payload capture. Provides continuous, gap-free NDA compliance records.
- **Full attestation verification + measurement dump** — a Python script installed in the guest
  image that: (a) generates a TDX quote, (b) parses and displays MRTD/RTMR0-3 measurements,
  (c) gathers GPU evidence via `chutes-nvevidence`, (d) verifies GPU evidence remotely via
  `nv_attestation_sdk`, (e) verifies TDX quote via Intel Tiber Trust Services CLI. Outputs both
  raw measurement values and pass/fail verification results.
- **Storage volume as raw block device** — reuse existing storage volume from quick-launch.
  Partner partitions, LUKS-encrypts, and mounts it from inside the guest. Size is configurable
  via `config.yaml` (multi-TB expected). No cache volume or config volume needed.
- **nvevidence CLI ownership** — uses `root:tdx-attest` (not `root:sek8s`). The `sek8s` group
  does not exist in benchmark images (the sek8s role is skipped). The `tdx-attest` group
  (GID 987) is created by `attestation-service/tasks/common-setup.yml` and owns the
  `/dev/tdx_guest` device (udev rule `MODE="0660"`), so processes must be in this group to
  generate TDX quotes. The benchmark attestation install reuses `common-setup.yml` to create
  the group, then sets nvevidence files to `root:tdx-attest`.
- **TDX remote verification config** — `/etc/tdx-attest-config.json` is **not** baked into the
  image. The partner creates it after SSH-ing in, providing their own Intel Tiber Trust Services
  API key. The verification script documents the expected format and exits gracefully when the
  config is missing (measurement dump still works). This aligns with "partner controls everything
  inside the guest."
- **Image naming**: `{version}-benchmark.qcow2` (parallels `-debug` suffix).
- **No changes to `setup-bridge.sh`** — extra DNAT rules for k3s API (6443), NodePorts
  (30000-32767), and status (8080) are harmless when no service listens. Avoids modifying a
  well-tested networking script.

---

## API Changes

- **New endpoints**: None
- **Schema changes**: None
- **Migrations**: None

---

## Goal

Success =

1. `benchmark_build: true` + `debug_build: true` produces a VM image that boots in TDX with
   GPUs in PPCIe confidential-computing mode.
2. Final image contains **only** the SSH keys listed in `benchmark_ssh_keys` — zero
   builder/operator keys.
3. Image includes: NVIDIA driver + CUDA + Docker (with NVIDIA runtime configured),
   `libtdx-attest`, TDX quote generator binary, `chutes-nvevidence` CLI, `trustauthority-cli`,
   and the attestation verification script.
4. Image does **not** contain: k3s, sek8s, gpu-verify, admission-controller, system-manager,
   OPA, cosign, chutes-gpu operator, config-manager, cache-volume services, miner credentials.
5. Cloud-init is fully purged (existing behavior — no change).
6. Partner SSHes from external machine via public:2222 DNAT to guest:22 over bridge+TAP.
7. Guest has outbound internet via NAT (for pulling artifacts, packages, etc).
8. Host runs a persistent conntrack-based network logging service capturing all VM connection
   metadata to rotated log files — no payload capture.
9. Storage volume attached as raw virtio block device — partner partitions, LUKS-encrypts,
   and mounts from inside guest.
10. Attestation verification script produces both raw MRTD/RTMR measurements and full remote
    verification results against Intel and NVIDIA attestation services.
11. Production and debug image builds are completely unaffected when `benchmark_build` is
    false or unset.

---

## Constraints

- No new Python dependencies beyond what is already in `nvevidence/` (`nv-attestation-sdk`).
- No behavioral changes when `benchmark_build` is false/unset (default).
- `benchmark_build: true` does not require `debug_build: true` — benchmark mode implicitly
  applies the same LUKS/harden-access/prime-vm skips by extending those conditions.
- `benchmark_ssh_keys` must be non-empty when `benchmark_build: true` — build fails early
  otherwise.
- Network logging is a host-side component (`host-tools/`), not baked into the guest image.
- Shell scripts use `set -euo pipefail`.
- No hardcoded attestation keys or measurements.
- Storage volume size is already configurable via `config.yaml` — no quick-launch changes
  needed for sizing.

---

## Output Format

### Phase 1: Ansible Guest Image Build

**1.1** Add variables to `ansible/guest/inventory.yml`:
- `benchmark_build: false`
- `benchmark_ssh_keys: []` (list of public key strings)

**1.2** Update `ansible/guest/playbooks/chutes-miner-vm.yml`:
- Add `benchmark_build` to confirmation output block.
- Add early validation: fail if `benchmark_build: true` and `debug_build` is not true.
- Add early validation: fail if `benchmark_build: true` and `benchmark_ssh_keys` is empty.
- Add `when: not benchmark_build | default(false)` to skip these plays:
  - Setup K3S clusters (k3s)
  - Setup Chutes GPU Clusters (chutes-gpu)
  - Install sek8s (entire role — nvevidence CLI installed separately in benchmark play)
  - Setup attestation-service (entire role — TDX quote generator installed separately)
  - GPU verification service (gpu-verify — sek8s-dependent, partner uses benchmark-attest)
  - Setup admission controller
  - Setup system manager
  - Setup cache volume service
  - Add dynamic config services (config role)
- Add new play: "Install benchmark attestation tools" (when `benchmark_build: true`),
  includes TDX quote generator + nvevidence CLI + trustauthority-cli + verification script.

**1.3** Update `ansible/guest/roles/common/tasks/main.yml`:
- Add `when: not benchmark_build | default(false)` to `k3s.yml`, `k8s.yml`, `helm.yml`
  includes. Keep `system.yml` and `network.yml` (always needed).

**1.4** Update `ansible/guest/roles/gpu/tasks/main.yml` and related:
- Skip `k3s-setup.yml` for benchmark.
- Move `nvidia-ctk runtime configure --runtime=docker` from `k3s-setup.yml` into
  `device-setup.yml` (device-level concern, needed for all build modes). Keep
  `--runtime=containerd` k3s-specific configure in `k3s-setup.yml`.

**1.5** Update `ansible/guest/roles/cleanup/tasks/main.yml`:
- Add `when: not benchmark_build | default(false)` to: `cleanup-k3s.yml`,
  `k3s-drop-ins.yml`, `system-manager-drop-ins.yml`, `admission-controller-drop-ins.yml`,
  `deploy-admission-webhook.yml`, `cleanup-attestation-service.yml`,
  `cleanup-gpu-verify-service.yml`.
- Add new include: `cleanup-benchmark-ssh.yml` with `when: benchmark_build | default(false)`.

**1.6** New file `ansible/guest/roles/cleanup/tasks/cleanup-benchmark-ssh.yml`:
- Remove `/root/.ssh/authorized_keys` (builder's key residual from cloud-init build phase).
- Write `benchmark_ssh_keys` entries into `/root/.ssh/authorized_keys` (mode 0600, root:root).
- Harden `sshd_config`: disable password auth, key-only auth.
- Verification task: read back `authorized_keys`, assert it contains exactly the expected
  keys and nothing else. Fail the build if mismatch.

**1.7** Update `ansible/guest/playbooks/group_vars/host.yml`:
- Extend `final_img_path`: `-benchmark` if `benchmark_build`, else `-debug` if `debug_build`,
  else empty.

**1.8** New role `ansible/guest/roles/benchmark/tasks/main.yml`:
- Uses `include_role: name: attestation-service, tasks_from: common-setup` (creates
  `tdx-attest` group/user, required directories). Cross-role reuse keeps file lookups
  (e.g. `tdx-quote-generator.c`) correctly scoped to the attestation-service role.
- Uses `include_role: name: attestation-service, tasks_from: install-tdx-quote-generator`
  (Intel SGX repo, libtdx-attest, compile quote generator, udev rules).
- Reuse `sek8s/tasks/install-nvevidence-cli.yml` pattern (nvevidence venv + CLI) but with
  `root:tdx-attest` ownership instead of `root:sek8s` (sek8s group doesn't exist in
  benchmark images; `tdx-attest` group owns `/dev/tdx_guest` device).
- Install `trustauthority-cli` from Intel SGX repo (already added by quote generator tasks).
- Install verification Python script as `/usr/local/bin/benchmark-attest` (Phase 3).
- Dedicated role keeps benchmark concerns entirely out of the attestation-service role,
  which is scoped to the production attestation FastAPI service.

### Phase 2: Host-Side Network Logging Service

**2.1** New script: `src/chutes-cvm/chutes_cvm/scripts/network/benchmark-netlog.sh`
- Uses `conntrack -E -o timestamp,extended` to stream connection events.
- Logs to `/var/log/chutes/benchmark-netlog/` with date-stamped files.
- Captures: timestamp, protocol, src IP:port, dst IP:port, state, bytes, packets.
- Filters to bridge subnet traffic (configurable via environment).
- No TLS payloads — connection metadata only.
- Runs `modprobe nf_conntrack` at start to ensure kernel module is loaded.

**2.2** New systemd service: `src/chutes-cvm/chutes_cvm/scripts/network/benchmark-netlog.service`
- `Type=simple`, runs `benchmark-netlog.sh`.
- `Restart=always`, `RestartSec=5` — gap-free logging.
- `WantedBy=multi-user.target`.
- Configurable via `EnvironmentFile` for log directory, bridge subnet, etc.

**2.3** Log rotation: `src/chutes-cvm/chutes_cvm/scripts/network/benchmark-netlog.logrotate`
- Daily rotation, compress old files, configurable retention.

**2.4** Update `quick-launch.sh` benchmark mode:
- Start `benchmark-netlog.service` after bridge setup.
- Stop it on `--clean`.

### Phase 3: Attestation Verification Script

**3.1** New file: `ansible/guest/roles/benchmark/files/attest.py`

Two modes — measurement dump and full verification:

**Measurement dump** (no internet required):
- Invoke `/usr/bin/tdx-quote-generator` to produce a binary TDX quote.
- Parse the quote binary to extract MRTD, RTMR0-3, MRSEAM, MRSIGNERSEAM, CPUSVN.
  (TDX quote structure: REPORTMACSTRUCT at 0x0, TEE_TCB_INFO at 0x100, TDINFO at 0x200
  within the quote body — reference: `tdx/tests/lib/tdx-tools/src/tdxtools/tdreport.py`.)
- Output human-readable table of all measurements for manual comparison against expected
  values provided out-of-band.

**Full remote verification** (requires internet):
- GPU attestation: gather evidence via `chutes-nvevidence gather-evidence`, verify via
  `nv_attestation_sdk` `Attestation.attest()` against NVIDIA remote service (same pattern
  as `nvevidence/` for evidence + external `nv-attest` package for verification).
- TDX attestation: invoke `trustauthority-cli token --config /etc/tdx-attest-config.json`
  to verify the TDX quote against Intel Tiber Trust Services. Config file is created by
  the partner after SSH-in (not baked into image). Script exits gracefully when config is
  missing — measurement dump and GPU verification still work.
- Report pass/fail for both TDX and GPU verification alongside measurement values.

**3.2** Install as `/usr/local/bin/benchmark-attest`, using the nvevidence venv's Python
for `nv_attestation_sdk` access. CLI interface via `typer` (already a dependency of
nvevidence).

### Phase 4: Quick-Launch

**4.1** Update `host-tools/scripts/quick-launch.sh`:
- Add `--benchmark` CLI flag.
- When benchmark mode:
  - Skip `MINER_SS58` and `MINER_SEED` validation (set to dummy/placeholder values).
  - Skip cache volume creation and attachment.
  - Skip config volume creation and attachment.
  - Default base image to the benchmark image set `/var/lib/chutes/base-images/tdx-guest-benchmark/` (assembled with `chutes_cvm.guest.image_set manifest`, like any other image set).
  - Keep bridge+TAP networking (default, unchanged).
  - Keep storage volume creation and attachment (raw block device for partner).
  - Start `benchmark-netlog.service` after bridge setup.
  - Pass `chutes-cvm guest launch` without `--config-volume` or `--cache-volume`, only `--storage-volume`.

**4.2** New `host-tools/scripts/config/config.benchmark.example.yaml`:
- Minimal config: hostname, network (tap mode), storage volume (multi-TB), no miner
  credentials, no cache volume, no config volume.

### Phase 5: Documentation

**5.1** New `docs/benchmark-mode.md`:
- Purpose, how to build, how to launch.
- What is included vs. excluded vs. debug vs. production.
- Partner workflow: SSH in, see raw block device, partition, LUKS-encrypt, mount, pull
  artifacts, run containers with `--network=none`, run attestation verification.
- Storage volume setup walkthrough.
- Attestation verification usage (measurement dump + full verification).
- Network logging: what is captured, where logs live, retention.

**5.2** Cross-reference from `docs/debug-mode.md`.

### Phase 6: Version and Changelog

**6.1** Bump `ansible/guest/VERSION` (VM domain change).

**6.2** Changelog fragments:
- `changelogs/vm/unreleased/` — benchmark build mode, attestation script.
- `changelogs/ops/unreleased/` — quick-launch benchmark mode, network logging service.

---

## Files Impacted

| File | Change |
|------|--------|
| `ansible/guest/inventory.yml` | Add `benchmark_build`, `benchmark_ssh_keys` |
| `ansible/guest/playbooks/chutes-miner-vm.yml` | Validation + conditionals on ~10 plays + new benchmark attestation play |
| `ansible/guest/playbooks/group_vars/host.yml` | Extend `final_img_path` for `-benchmark` suffix |
| `ansible/guest/roles/common/tasks/main.yml` | Skip k3s/k8s/helm for benchmark |
| `ansible/guest/roles/gpu/tasks/main.yml` | Skip k3s-setup for benchmark |
| `ansible/guest/roles/gpu/tasks/device-setup.yml` | Add Docker nvidia-ctk configure (moved from k3s-setup) |
| `ansible/guest/roles/gpu/tasks/k3s-setup.yml` | Remove Docker runtime configure (now in device-setup) |
| `ansible/guest/roles/cleanup/tasks/main.yml` | Skip k3s/sek8s/gpu-verify cleanups + add benchmark SSH cleanup |
| `ansible/guest/roles/cleanup/tasks/cleanup-benchmark-ssh.yml` | **New** — SSH key replacement + verification |
| `ansible/guest/roles/benchmark/tasks/main.yml` | **New** — install TDX quote gen + trustauthority-cli + nvevidence CLI + verification script |
| `ansible/guest/roles/benchmark/files/attest.py` | **New** — attestation verification script (Phase 3) |
| `ansible/guest/roles/benchmark/files/luks-setup.py` | **New** — LUKS encryption helper script |
| `src/chutes-cvm/chutes_cvm/scripts/network/benchmark-netlog.sh` | **New** — conntrack event logger |
| `src/chutes-cvm/chutes_cvm/scripts/network/benchmark-netlog.service` | **New** — systemd unit for persistent logging |
| `src/chutes-cvm/chutes_cvm/scripts/network/benchmark-netlog.logrotate` | **New** — log rotation config |
| `host-tools/scripts/quick-launch.sh` | Add `--benchmark`, `--download-benchmark` modes |
| `host-tools/scripts/config/config.benchmark.example.yaml` | **New** — benchmark config template |
| `docs/benchmark-mode.md` | **New** — full documentation |
| `docs/debug-mode.md` | Cross-reference to benchmark mode |
| `ansible/guest/VERSION` | Version bump |
| `changelogs/vm/unreleased/*.md` | **New** changelog fragment |
| `changelogs/ops/unreleased/*.md` | **New** changelog fragment |

---

## Failure Conditions

- Production or debug image builds produce different output when `benchmark_build` is
  false/unset.
- The benchmark image contains any SSH keys other than those specified in
  `benchmark_ssh_keys`.
- The benchmark image contains k3s, sek8s binaries, gpu-verify, OPA, cosign,
  admission-controller, system-manager, or miner credential services.
- The benchmark image fails to boot or lacks GPU drivers, Docker, or `libtdx-attest`.
- Docker NVIDIA runtime is not configured in the benchmark image (containers cannot access
  GPUs).
- `quick-launch.sh` without `--benchmark` flag changes behavior in any way.
- Storage volume is not visible as a virtio block device inside the guest.
- Network logging service has gaps (crashes without restart, loses events).
- Attestation verification script fails to produce measurement dump when run offline.
- Attestation verification script fails to perform remote verification when internet is
  available.

---

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Builder SSH key leak — if cleanup task fails, builder key ships in image | **High** | Verification task reads back `authorized_keys` and asserts exact match against `benchmark_ssh_keys`. Build fails on mismatch. |
| Docker NVIDIA runtime gap — moving `nvidia-ctk --runtime=docker` to device-setup may affect prod/debug | Medium | Command is idempotent. Test prod + debug builds after refactor. |
| conntrack logging gaps — if process dies, gap in logs | Medium | systemd `Restart=always` + `RestartSec=5`. Script validates conntrack module at start. |
| conntrack not loaded on host — kernel module may not be loaded before VM starts | Low | `benchmark-netlog.sh` runs `modprobe nf_conntrack` at start. Bridge iptables NAT rules also trigger module load. |
| trustauthority-cli availability — may not be in Intel SGX repo for target Ubuntu version | Medium | Verify during implementation. Fallback: measurement dump still works without it; GPU verification still works. TDX remote verification is additive. |
| Existing flow regression — new conditionals alongside debug_build | Medium | All new conditionals default to false. Test matrix: production, debug, benchmark. |
| Cleanup ordering — benchmark SSH cleanup must run after provisioning but before VM shutdown | Low | Place after `cleanup-cloud-init`, before `cleanup-build` in cleanup/main.yml. |
| Bridge extra DNAT rules — k3s/NodePort/status forwards exist but nothing listening | None | Packets dropped by guest kernel. Zero security exposure. Document that only SSH (2222) is reachable. |
| Log volume from conntrack — long eval could produce large logs | Low | Logrotate with daily rotation, compression, configurable retention. Estimated 10-50 MB/day for connection metadata. |

---

## Rollout Notes

- **Ansible variables**: Set `benchmark_build: true` and populate `benchmark_ssh_keys`
  with the partner's public key(s) before building. `debug_build` does not need to be set.
- **Image distribution**: The benchmark image is built directly on the target server via
  Ansible (same as production/debug). It is not uploaded or distributed. Point `base_image`
  in `config.benchmark.yaml` to the built image path (`guest-tools/image/<env>/<version>-benchmark.qcow2`).
- **Network logging**: `benchmark-netlog.service` is started by quick-launch in benchmark
  mode. Logs persist across VM restarts at `/var/log/chutes/benchmark-netlog/`. Stop and
  archive on evaluation teardown.
- **Teardown procedure**: On evaluation end, stop VM, delete qcow2 overlay, delete storage
  volume backing file (partner's LUKS volume is opaque — deletion destroys ciphertext),
  stop and archive netlog, remove bridge.
- **Backward compatible**: All changes are behind `benchmark_build` flag (default false).
  Production and debug builds are unaffected.
- **Expected deliverables to partner before evaluation start**:
  1. Link to public repo branch with the benchmark image build.
  2. Attestation verification script usage instructions.
  3. Expected MRTD/RTMR measurement values from preliminary build.
  4. Written teardown procedure.
