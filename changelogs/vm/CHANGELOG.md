# Changelog

All notable changes to the VM / guest image will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

Version source of truth: `ansible/guest/VERSION`

## [1.4.0] - 2026-09-10

### Added
- **Boot-time miner-hotkey proof-of-possession (guest side).** A small, static, pinned-toolchain (musl) sr25519 signer (`src/sr25519`; Schnorr/Ristretto, which openssl cannot do) is built and staged into the guest initramfs (measured into RTMR2). The boot flow now derives the hotkey from the config-volume seed (`/run/tdx-config/miner-seed`) rather than trusting the claimed `miner-ss58`, and signs each chained server nonce — `/boot/attestation`, `/provision`, and `/provision/confirm` — sending the sr25519 proof in `X-Chutes-Signature`. This closes a cross-miner LUKS-brick vector where a peer could assert a victim's `(hotkey, vm_name)` and rotate its passphrase. The seed is stashed to `/run` for the init-bottom calls and shredded before the initramfs `/run` is moved into userspace. Pairs with a matching server-side signature check (chutes-api). Also fixes a migration miss: root-rotation confirm now uses `/provision/confirm` (the legacy `/luks/confirm` is deprecated).
- **Reproducible builds from public inputs.** The root RSA public key is fetched from R2 at build time (`root_signing_key_url`) instead of a required local file, and a new `ansible/guest/inventory-reproduce.yml` builds the prod image with only non-secret inputs (published LUKS passphrase, R2-fetched key) so an independent party can reproduce the image and verify the published measurements.
- New initramfs script `write-validator-auth` (init-bottom) writes the per-VM ephemeral validator auth SS58 to `/run/chutes/validator-auth.env` — directly in the initramfs `/run` tmpfs, which `initramfs-tools` moves to the real root's `/run` before exec'ing init. The file is fully ephemeral (cleared on every reboot, never touches the root filesystem), and the write logic is measured into RTMR2. VM powers off on invalid or missing SS58.
- New cluster-init script `03-k3s-validator-auth.sh`: creates or updates the `validator-auth` K8s Secret in the `attestation-system` namespace with the per-VM ephemeral SS58 on every boot (no run-once marker), then restarts the attestation-proxy DaemonSet to apply the new `ALLOWED_VALIDATORS` value. Added to `SECURITY_CRITICAL_SCRIPTS` so a failure causes VM poweroff.
- `system-manager.service` now loads `/run/chutes/validator-auth.env` as a second `EnvironmentFile`. Since this file is ephemeral and can never be present if `write-validator-auth` did not run, the service correctly fails to start if the initramfs script was skipped — safe failure by design.
- RTMR3 measurement hardening: added `/etc/system-manager/system-manager.env`, `/etc/admission-controller/admission-controller.env`, `/etc/admission-controller/cosign-registries.json`, `/etc/docker/daemon.json`, `/etc/rancher/k3s/registries.yaml`, `/etc/hosts`, and `/etc/tdx-luks.conf` to `tdx-measure-miner.conf`. These were previously unmeasured, allowing offline tamper of registry allowlists, cosign config, or attestation endpoints without detection.
- New Ansible role `apparmor-hardening`: installs AppArmor profiles, abstractions, systemd drop-ins, and a boot-time profile verification service (`lock-mac-caps.service`).
- AppArmor abstraction `sek8s-cache-deny`: denies shell/interpreter access to the HF model cache volume (`/var/snap/cache/`). Debug builds use `audit deny` for kernel audit logging; production builds use silent `deny`.
- AppArmor abstraction `sek8s-secrets-deny`: denies shell/interpreter access to boot secrets (`/run/chutes/`), containerd socket, k3s token, and miner credentials.
- AppArmor profile `sek8s.system-manager`: named profile applied via systemd `AppArmorProfile=` — grants cache rw, credential read, containerd socket, and network access.
- AppArmor profile `sek8s.setup-cache`: named profile for the setup-cache service — grants cache rw and coreutils, no network or credentials.
- AppArmor profile `sek8s.deny-sensitive-default`: auto-attaches to common shells, interpreters, and data-transfer tools (bash, dash, sh, cat, cp, tar, rsync, curl, wget, perl, etc.) — includes both deny abstractions to block access to protected paths.
- `verify-apparmor-profiles.service`: oneshot that verifies all sek8s AppArmor profiles are loaded in enforce mode at boot. Powers off the VM on failure.
- RTMR3 progress logging: per-directory collection progress and periodic hashing progress (every 200 files) logged to `/dev/kmsg` during the expanded measurement phase.
- `ansible/guest/roles/luks/files/initramfs/luks-helpers`: shared initramfs shell library with `write_key_file`, `shred_key_file`, `luks_add_key`, `luks_remove_key` — sourced by both `fetch_key_and_unlock` (init-premount) and `setup_storage` (init-bottom).
- Root LUKS passphrase rotation in `fetch_key_and_unlock` (init-premount): detects first-boot LUKS2 token (id 15, type `chutes-first-boot`), sends `first_boot` flag in boot attestation POST, enforces mandatory rotation on every boot — adds new key slot, confirms with API, then kills all pre-existing slots by number to ensure no stale keys remain on the device.
- `ansible/guest/roles/rtmr3-measure/files/tdx-measure-miner.conf` and `tdx-measure-gpu.conf`: extended RTMR3 measurement coverage to additional filesystem paths not previously included:
  - `/usr/lib/systemd/system`
  - `/etc/fstab`
  - `/var/spool/cron/crontabs`
  - `/etc/init.d`
  - `/etc/rc.local`
  - `/root/.bashrc`, `/root/.bash_profile`, `/root/.profile`
- `tdx-measure-gpu.conf` aligned to the same measurement tiers as `tdx-measure-miner.conf`: systemd unit dirs, ld.so config, modprobe, sysctl, profile, environment, fstab, crontabs, init scripts, root shell startup files, and the `/usr/local/bin`, `/usr/local/sbin`, `/usr/bin`, `/usr/sbin`, `/usr/local/lib` binary tiers.
- `ansible/guest/roles/signing-keys/` — new role for root-of-trust PGP key installation and initramfs key-fetch machinery.
- `vm-tls` role with `setup_vm_tls`, an initramfs `init-bottom` script
  (`PREREQ=setup_storage`) that owns the full VM mTLS cert lifecycle — per-boot
  4096-bit VM root CA generation, validator registration
  (`PUT /servers/{vm_name}/vm-root-ca`, TDX-attested, mTLS using the CA cert
  itself as the client credential), attestation-proxy server cert, registry mTLS
  client cert, and `ca.key` deletion — all within the RTMR2-measured initramfs
  before `pivot_root`. `ca.key` never exists in userspace.
- Attestation proxy server cert (`/run/chutes/proxy-tls/server.{key,crt}`) and
  registry mTLS client cert (`/run/chutes/registry-tls/client.{key,crt}`)
  generated on tmpfs each boot; containerd reads the client cert for direct mTLS
  pulls from `registry.chutes.ai`, and cosign reads it via
  `/etc/docker/certs.d/registry.chutes.ai/` symlinks.
- `sek8s.attestation-proxy` AppArmor profile confining the proxy container to
  its required paths; added to the apparmor-hardening install/verify wiring and
  to the RTMR3 measurement chain (`tdx-measure-miner.conf`).
- **Build-time RTMR computation** — `chutes-miner-vm.yml` now computes all expected
  build-time RTMRs (1, 2, 3) from the finalized image before LUKS encryption, in one
  `compute-rtmrs` role that composes `stage-boot-artifacts`, `compute-rtmr3`,
  `tdx-measure` (fork provisioning), and `compute-rtmr1-2`. Emits `<image>.rtmr1`,
  `.rtmr2`, `.rtmr3` (bare uppercase hex). RTMR1/2 are version-level
  (topology-independent) and come from the prod image — the debug image's initrd
  differs, so its RTMR2 would be wrong. The role ensures its own build-host
  prerequisites (`libguestfs-tools`, `git`, and `cargo` only when the build user has
  none); `tdx-measure` clones/builds the `chutesai/tdx-measure` fork (reusing an
  existing checkout), overridable via `tdx_measure_bin`.
- **`stage-boot-artifacts`** — extracts the direct-boot kernel/initrd/cmdline from the
  finalized image once and persists them next to it as `<image>.vmlinuz`, `.initrd`,
  `.cmdline`. Published to R2 with the qcow2 and read by both `compute-rtmr1-2` (build)
  and the launcher (deploy), so the pinned RTMR1/2 match the running VM by construction.
- **`capture-measurement-baseline.yml`** — a local build-server step that captures the
  offline-measurement baseline (the RTMR0 inputs) from the freshly-built debug image:
  copies it to `/tmp` so the publishable artifact is never mutated, TDX-boots the copy,
  captures the CCEL + fw_cfg ACPI/SMBIOS preimages into the top-level
  `measurements/<version>/`, verifies the CCEL actually landed, and tears down.
- **`guest-tools/measurement/`** — offline RTMR0 measurement/verification tooling:
  `ccel_replay.py` (CC event-log parse + SHA-384 RTMR replay, with a per-register
  `diff`), `capture-measurement-artifacts.sh` (capture the CCEL + preimages),
  `extract-measurements.sh` (report a running guest's live MRTD + RTMR0-3 from a fresh
  quote), and `utils/` (SMBIOS-event preimage matcher, per-table ACPI byte-diff). Reuses
  the launcher's QEMU-arg builders and the `virtee/tdx-measure` fork.
- **`docs/specs/tdx-measurement-verification.md`** — how TDX guest measurements are
  structured, why RTMR0 is the only per-topology register, and how they are
  independently reproduced and verified.
- **Chute log shipper service (guest image, Phase 1).** New `chute-log-shipper` systemd service +
  Ansible role in the attested guest image, running the `sek8s.log_shipper` agent as a dedicated
  non-root uid. Ships crash/warmup logs of chute pods to the validator before instance registration.
  - New Ansible role `chute-log-shipper` (registered in `chutes-miner-vm.yml`): hardened systemd
    unit, rendered env, a restricted `crictl-pods-helper` wrapper (read-only `pods`/`ps` JSON), the
    dedicated uid, and boot wiring (group/ACL for the CRI socket + `/var/log/pods` + the registry-tls
    leaf, cursor/checkpoint state dir). No new leaf is minted — a boot-time path unit re-groups the
    existing per-boot CVM mTLS leaf for the service's uid.
  - `sek8s.chute-log-shipper` AppArmor profile delivered via `apparmor-hardening`, confining the
    service to the chute log paths, the CRI socket, the registry-tls leaf, the checkpoint dir, and
    egress to the validator.
  - **Measurement:** adds guest image content (package + systemd unit + crictl wrapper + AppArmor
    profile) → shifts **RTMR3**. Regenerate expected-measurement baselines before rollout.
- RC gate for debug/RC VMs: the debug image boots a fail-open initramfs that provisions
  against the production network (validator auth, VM root CA registration, k3s encryption)
  by proving possession of an authorized operator key — a detached RSA signature over the
  boot nonce sent in `X-Operator-Signature`. Only an authorized operator can bring a debug
  VM up against prod, and it can never join real traffic; the debug initramfs carries a
  distinct measurement (registered `rc: true`).
- Offline per-topology RTMR0 generation (`guest-tools/measurement/generate_measurements.py`,
  wired via the new `compute-rtmr0` role): reconstructs RTMR0 for every supported GPU
  topology by splicing per-topology events from the `tdx-measure` fork into a captured
  baseline CCEL — no per-topology hardware boot. `measurement_profile` selects one profile
  or, when empty, all profiles.
- Debug images now compute full RTMR1/2/3 (registered `rc: true`) so they attest under the
  RC gate.
- **Fully offline RTMR0 generation.** The `tdx-measure` fork now self-generates the
  complete 15-event RTMR0 for each topology — firmware (MRTD/CFV/secure-boot), the
  QEMU-generated ACPI (loader/rsdp/tables), the `etc/extra-pci-roots`/BootMenu/bootorder
  fw_cfg events, and the SMBIOS handoff — with **no captured CCEL and no TDX hardware**.
  `measurements=offline` now yields COMPLETE measurements on any x86-64 host; the
  `capture-ccel` step is retained only as a `measurements=full` cross-validation against a
  real quote, not a build dependency.
- **Cross-host measurement determinism via the topology fingerprint.** RTMR0 is the only
  CPU-dependent measurement, and only two things move it: the guest vendor drives QEMU's
  SRAT memory-map (AMD guests get a 1 TiB memory hole) and the CPUID leaf-1 becomes the
  SMBIOS Type-4 Processor ID. Offline measurement generation pins the fingerprint's
  `cpu_vendor` into the measurement `-cpu` and patches `cpu_processor_id` into the dumped
  SMBIOS via the fork, so any host — including non-Intel — regenerates the exact production
  RTMR0. (phys-bits was measured to not affect RTMR0 and is not carried.) Launch keeps
  plain `-cpu host` (real silicon, features, transparency).
- The launcher **refuses to boot a host whose fingerprint isn't in the profile's baselined
  set** (exact match on CPU + mem + device layout) — one check that subsumes the former
  separate CPU-identity guard; an unbaselined host's RTMR0 would diverge and never attest.
  A profile whose fingerprint is a placeholder (`cpu_processor_id=None`, pending a
  discover-profile.sh capture) never matches a live host, so it is refused until captured.

### Changed
- Split cosign signature verification into two keys: `chutes.pub` for the private localregistry (and wildcard fallback), `dockerhub.pub` for Docker Hub `parachutes/*` images
- Renamed Ansible inventory vars: `cosign_public_key_path` -> `cosign_chutes_public_key_path` (`~/.cosign/chutes.pub`) and added `cosign_dockerhub_public_key_path` (`~/.cosign/dockerhub.pub`)
- Renamed admission controller env vars: `CHUTES_COSIGN_PUBLIC_KEY_PATH` -> `CHUTES_PUBLIC_KEY_PATH`, added `DOCKERHUB_PUBLIC_KEY_PATH`
- Generalised `_require_ctx_key` to validate against a set of trusted key paths (`required_key_paths`) rather than a single path
- `chutes_chart_version` bumped from `0.2.7` to `0.3.0` (`chutes-miner-gpu` Helm chart). The new chart replaces the per-validator nginx `map` routing with a single static `set $upstream_host` directive and a single `chutes-registry` NodePort Service, required for the static `localregistry.chutes.ai` hostname. See [chutes-miner#134](https://github.com/chutesai/chutes-miner/pull/134).
- Registry hostname decoupled from the validator hotkey: all `{{ validator | lower }}.localregistry.chutes.ai` references replaced with the static hostname `localregistry.chutes.ai` across k3s-prereqs.yml, registries.yaml.j2, configure-cosign.yml, opa-config-data.json.j2, cosign-registries.json.j2, admission-controller/defaults/main.yml, and system-manager.env.j2. Validator hotkey rotation no longer invalidates cosign signatures or requires a VM rebuild.
- `fetch_key_and_unlock` (initramfs, init-premount): now parses `vm_auth_ss58` from the boot attestation API response and saves it to `/run/chutes/validator-ss58` (mode 600). Boot fails with poweroff if the field is absent from the response.
- `proxy-manifests.yaml.j2`: removed the baked-in `validator-auth` Secret definition (it contained a hard-coded validator hotkey and is in the RTMR3-measured manifests directory). The Secret is now created at runtime by `03-k3s-validator-auth.sh` and consumed by the attestation-proxy via `secretKeyRef` (`ALLOWED_VALIDATORS`), which the kubelet injects over its own host-network API client — so no in-pod secret read (and thus no `secret-reader` RBAC or `wait-for-credentials` init container) is needed. Socket-readiness is gated separately by the `wait-for-attestation-socket` init container.
- `system-manager.env.j2`: removed `ALLOWED_VALIDATORS` (now in unmeasured `validator-auth.env`). `IMAGE_PULL_ALLOWED_REGISTRIES` updated to use static `localregistry.chutes.ai` hostname. `system-manager.env` is now fully deterministic at build time and safe to include in RTMR3 measurement.
- `tdx-measure-miner.conf`: added three-tier RTMR3 measurement expansion — Tier 1 (custom binaries in `/usr/local/{bin,sbin}`), Tier 2 (code injection config paths), Tier 3 (system binaries in `/usr/bin`, `/usr/sbin`, and custom shared libs in `/usr/local/lib`). Also added service configs, AppArmor profiles, and systemd units not previously measured.
- `tdx-measure-miner.conf`: removed `/etc/rancher/k3s/registries.yaml` (runtime-modified by `process-config.py`, persists across reboots; security properties independently measured through other files).
- `pods.rego`: added `MAC_ADMIN` and `MAC_OVERRIDE` to `dangerous_capabilities` to prevent containers from modifying AppArmor profiles.
- `chutes-miner-vm.yml`: inserted `apparmor-hardening` role after `cache-volume` and before dynamic config services.
- `ansible/guest/roles/luks/tasks/luks_encrypt.yml`: added `type: luks2` to the LUKS container creation task (previously relied on cryptsetup default); added first-boot LUKS2 token task (`chutes-first-boot`, id 15) after container creation; added task to copy shared `luks-helpers` script into the initramfs.
- `ansible/guest/roles/luks/files/initramfs/fetch_key_and_unlock`: updated boot attestation POST body to include `first_boot` flag; added slot enumeration and `luksKillSlot`-based cleanup after successful rotation confirm; rotation confirm failure now rolls back cleanly and powers off; any key slot cleanup failure powers off rather than proceeding with stale slots.
- `ansible/guest/roles/luks/files/initramfs/setup_storage`: extracted LUKS helpers to shared `luks-helpers` file; `finalize_rotation` now uses `luksKillSlot` by slot number (cleaning up stale slots from prior incomplete rotations); any slot cleanup or rollback failure powers off.
- Cosign public keys (`chutes.pub`, `dockerhub.pub`) and the Helm PGP keyring (`helm-pubkey.gpg`) are no longer baked into the VM image. They are now fetched dynamically at boot from `VALIDATOR_BASE_URL/servers/signing-keys`, verified against an attested root PGP key, and written to `/run/chutes/signing-keys/` (ephemeral tmpfs). Key rotation no longer requires an image rebuild or RTMR3 change.
- New `signing-keys` Ansible role installs the root PGP public key to `/etc/chutes/root-signing-key.gpg` (measured in RTMR3), deploys `signing-keys.conf` with the API URL, and installs the `fetch-signing-keys` initramfs init-bottom script and its hook.
- `fetch-signing-keys` initramfs script verifies each key's detached PGP signature with `gpgv` against the attested root key before writing to tmpfs. Any signature failure powers off the VM (fail-closed).
- `admission-controller.env` and `cosign-registries.json` updated to reference `/run/chutes/signing-keys/cosign/` paths.
- Helm chart provenance verification (`04-helm-chart-upgrade.sh`) updated to read keyring from `/run/chutes/signing-keys/helm-pubkey.gpg`.
- Build-time Helm keyring is now fetched from the signing-keys API and PGP-verified on the build host (same trust chain as boot-time fetch). The key is written to `/tmp/` for the `helm upgrade --install` call and deleted immediately after. No leaf key files (`helm-pubkey.gpg`, `chutes.pub`, `dockerhub.pub`) need to be distributed to build machines — only the root PGP public key is required.
- `/etc/admission-controller/cosign` removed from RTMR3 measurement path list (`tdx-measure-miner.conf`). Trust in cosign keys is now delegated to the PGP chain rooted at the measured `/etc/chutes/root-signing-key.gpg`.
- AppArmor profile `sek8s.system-manager` updated to allow reads from `/run/chutes/signing-keys/`.
- `fetch_key_and_unlock` (initramfs init-premount): the boot nonce endpoint (`/servers/nonce`) is
  now fetched via the mTLS proxy (`TDX_BASE_URL`) instead of the regular TLS API
  (`VALIDATOR_BASE_URL`), matching the API-side change that validates the miner cert during nonce
  issuance.
- `fetch_key_and_unlock`: the nonce request now includes the miner hotkey as the `miner_hotkey`
  query parameter (`?miner_hotkey=<hotkey>`), binding the nonce to the requesting miner. The API
  enforces that the same hotkey appears in the subsequent boot attestation POST body; nonces issued
  without a hotkey are rejected by the server as legacy.
- All boot-sensitive initramfs API calls now go through the mTLS proxy (`TDX_BASE_URL`). The LUKS
  root-rotation confirm (`fetch_key_and_unlock`) and storage/cache rotation confirm
  (`setup_storage`) previously used the regular TLS API; both now use `TDX_BASE_URL` with the
  ephemeral client certificate. In `setup_storage` the mTLS cert deletion is deferred from
  `post_sync_keys` to `confirm_rotation` so the cert is available for the confirm call; it is also
  cleaned up in `clear_sensitive_data` as a safety net for boots where confirm is skipped.
  Exception: `fetch-signing-keys` continues to use `VALIDATOR_BASE_URL` — the signing-keys
  endpoint is intentionally public and does not require mTLS.
- `VALIDATOR_BASE_URL` is no longer required or validated by `fetch_key_and_unlock`. It remains in
  `tdx-luks.conf` for `fetch-signing-keys` (signing keys bundle fetch) and post-boot services
  (system-manager).
- Attestation proxy init container migrated from `bitnami/kubectl:latest` (unsigned, unpinned) to `parachutes/kubectl` (cosign-signed with `dockerhub.pub`). Removed the `require_signature: false` exception for `bitnami/kubectl` from the cosign registry config and removed `bitnami` from the OPA registry allowlist.
- Bump VM version to 1.3.1 for new RTMR0 measurements. The guest image is
  unchanged; RTMR0 changes because QEMU now pins SMBIOS type 1/2/3 identity to
  static values, removing per-server motherboard drift from RTMR0 within a
  profile. Topology-driven variance (type 4/17) is still absorbed per-profile.
- Private registry pull auth moves from miner-hotkey-scoped (nginx proxy
  DaemonSet on NodePort 30500 at `localregistry.chutes.ai:30500`) to per-VM mTLS
  against `registry.chutes.ai`. Only an attested VM presenting a CA-signed client
  cert can pull. Backward compatibility is DUAL-AUTH and lives server-side (the
  validator/registry): old VMs keep the legacy miner-proxy path, new VMs present
  a client cert. The guest image carries no dual-path code.
- `registries.yaml.j2`: replaced the `localregistry.chutes.ai:30500` local-proxy
  mirror with a `configs: "registry.chutes.ai"` mTLS block pointing at the
  initramfs-written tmpfs client cert/key (no insecure-registry, no NodePort).
- `proxy-manifests.yaml.j2`: `host-certs` hostPath moved from
  `/etc/attestation-service/certs` to `/run/chutes/proxy-tls`; added the
  attestation-proxy AppArmor annotation.
- `cosign-registries.json.j2`, `opa-config-data.json.j2`, admission
  `allowed_registries`, and system-manager `IMAGE_PULL_ALLOWED_REGISTRIES`
  updated from `localregistry.chutes.ai:30500` to `registry.chutes.ai`. Removed
  the `allow_http` / `allow_insecure` cosign flags now that pulls use real TLS.
- `configure-cosign.yml`: removed the `127.0.0.1 localregistry.chutes.ai`
  `/etc/hosts` alias and the `insecure-registries` Docker daemon config that
  supported the old local proxy.
- Build-pipeline-only scripts moved from `guest-tools/scripts/` into their Ansible role
  `files/` (invoked exclusively by the build): `compute-rtmr3.sh`, `compute-rtmr1-2.sh`,
  `stage-boot-artifacts.sh`, and `extract-vm-measurements.sh`. `guest-tools/scripts/` now
  holds only the standalone release tool `publish-image.sh`.
- The per-boot VM root CA is now generated up front in `fetch_key_and_unlock` (init-premount) and used as the VM's single mTLS client identity for every boot API call (`GET /nonce`, `POST /boot/attestation`, root `POST /luks/confirm`, and the runtime storage attestation). This replaces the throwaway self-signed client cert (`CN=tdx-vm-<ts>`) that was previously minted for the boot/luks calls.
- `setup_storage` now calls the new `POST /servers/{vm}/provision` and `POST /servers/{vm}/provision/confirm` endpoints (replacing `/luks/attest` and the storage `/luks/confirm`). The provision quote binds `SHA256(CA pubkey)` after RTMR3 is extended, so the validator records the VM root CA implicitly from that RTMR3-attested call — no separate registration round-trip.
- VM root CA and both leaf certs (attestation-proxy server cert, registry mTLS client cert) now use a 365-day validity instead of 1 day, so long-running VMs (which reboot only on image updates) do not hit cert expiry mid-run. Per-boot rotation is unchanged — all certs are still regenerated fresh on every boot and live in tmpfs only.
- Signing-keys bundle verification switched from detached OpenPGP (`gpgv`) to
  raw RSA (PKCS#1 v1.5, SHA-256). The `fetch-signing-keys` initramfs script and
  the build-time Helm-key verification in the `chutes-gpu` role now verify each
  key's signature over its raw (base64-decoded) bytes with
  `openssl dgst -sha256 -verify`. The trust chain (RTMR1 + RTMR3 attest the
  baked-in root key → root key verifies the signature → signature authenticates
  the leaf key), the JSON bundle shape, the `/run/chutes/signing-keys/` output
  paths, and the fail-closed behavior are unchanged. `helm-pubkey.gpg` stays a
  byte-identical OpenPGP key file for Helm; only the signature over it changed.
- The root trust anchor is now the RSA public key
  `/etc/chutes/root-signing-key.pem`, replacing `root-signing-key.gpg`. It is
  baked into the same measured locations (initramfs → RTMR1, `/etc/chutes/` →
  RTMR3), so tampering still changes the measurement.
- **CVM mTLS client cert CN generalized.** The per-boot mTLS client leaf minted by the vm-tls
  initramfs `setup_vm_tls` script now uses a generic subject (`CN=sek8s-cvm-mtls-client`) instead of
  `sek8s-vm-registry-client`. That leaf is the shared identity for *all* CVM mTLS (registry pulls,
  the log shipper, …), not registry-specific, so the old name was misleading. Identity is **not**
  carried in the CN — the validator resolves `(miner_hotkey, vm_name)` by verifying the leaf against
  the registered per-boot VM CA — so the CN is intentionally generic, not per-VM. Edits initramfs →
  shifts **RTMR2**; regenerate measurement baselines before rollout.
- The `luks` role now runs for **all** guest images and gates internally on the build type:
  prod encrypts the root filesystem and installs the fail-closed initramfs; debug installs
  the fail-open RC initramfs and performs no encryption. Prod and debug carry distinct
  initramfs measurements.
- Boot and storage-provisioning logic refactored into shared initramfs libraries
  (`attest-common`, `provision-common`) sourced by both prod and debug entry scripts, so the
  two stay in sync without leaking debug code into prod.
- Measurement pipeline restructured into explicit phases with one peer role per register —
  gather (`stage-boot-artifacts` + `capture-ccel`) then compute (`compute-rtmr1-2` +
  `compute-rtmr0`); RTMR1/2 now computed post-luks (after the initrd is final). Measurement
  controls collapsed to a single `measurements: none | offline | full` flag.
- CVM mTLS operations now use the `cvm.chutes.ai` domain.
- HWE kernel bumped to `7.0.0-28.28~24.04.1`.
- The compute phase now aggregates every register into a single
  `measurements/<version>/measurements.yaml` (teeMeasurements-shaped, ready to merge into
  chutes-ops values) instead of scattered per-register files; the raw registers are carried
  as in-play facts, and per-topology hardware entries are named from each topology
  fingerprint (computed, not hand-curated). A single `compute-measurements` tag runs the
  whole phase (gather → compute → aggregate); a `build` tag runs the image-production plays.
- The attestation proxy's init container now runs a cosign-signed `parachutes/busybox`
  image (verified with `dockerhub.pub`) so it passes the admission controller instead of
  being rejected as an unsigned image.
- **Host-instance facts moved from `GpuProfile` into the topology fingerprint**, which is
  now `TopologyFingerprint(cpu: CpuTopology, mem_gb: int, gpu: GpuTopology)` — the three
  host axes that move RTMR0. `CpuTopology` carries the guest `-smp` (vcpus + sockets) and
  CPU identity (vendor + Processor ID); `mem_gb` is guest RAM; `NumaTopology`/`FlatTopology`
  carry only the device layout. These are derived from the LIVE host at detection (`vcpus =
  host_cpus − host_reserved_cpus`; mem via a per-profile `guest_mem_gb` rule; CPU via
  `/proc/cpuinfo`), and the known values live in `gpu/known_topologies.py` so profiles just
  import them. A `GpuProfile` now holds only
  GPU-model policy plus `host_reserved_cpus` / `guest_mem_gb`. Consequently **`B200Profile`
  and `B200Xeon6Profile` collapse into one `B200Profile`** — the 192-CPU Xeon and 288-CPU
  Xeon 6 hosts are two fingerprints, not two profiles — and an off-nominal host (e.g. a
  192-CPU H200) now derives its own fingerprint/measurement instead of being pinned to the
  nominal one. Published measurement names gain the host shape, e.g.
  `8xb200 [10.2.1, numa-176c-1944g]`.
- `compute-rtmr0` no longer requires a baseline CCEL; it runs the fork to self-generate
  RTMR0 directly. `generate_measurements.py generate` drops the CCEL splice and folds the
  fork's own rtmr0; `--baseline` is now an accepted-but-ignored deprecated flag.
- `discover-profile.sh` additionally reports the host CPU identity (`cpu_vendor`,
  `cpu_processor_id`), computed host-side with no TDX and no guest capture — the Processor
  ID is CPUID leaf-1 (EAX from family/model/stepping, EDX = the TDX Module's fixed leaf-1
  baseline). These are declared once per host class in a profile's fingerprint; a
  fingerprint with `cpu_processor_id=None` stays launch-gated but refuses offline generation
  rather than silently emitting a measurement for the generating host's CPU.
- **Boot attestation failures now surface the API's reason.** The initramfs LUKS client
  (`attest-common`, `setup_storage`) previously logged only a generic string and the HTTP
  status (e.g. `Authentication failed (HTTP 403)`) when the nonce fetch, attestation POST, or
  rotation confirm failed. It now reads the response body for a `detail` / `message` / `error`
  field (FastAPI's `detail` first) and appends it — single-lined and capped at 300 chars so a
  body can't mangle the console — so a miner sees the actual cause. The 401/403 case on the
  attestation POST is relabeled `Attestation rejected` (it is a measurement verdict, not an
  auth failure). Falls back to the prior generic string when the body carries no message.
- **The guest-image build's measurement step now sources known host classes from the API.**
  `chutes-cvm measurements generate` reads the published host profiles (and their fingerprints)
  from the control plane instead of an in-repo baseline registry, so the build host must reach the
  API. The `chutes-miner-vm` build passes `--api-base` (var `measurements_api_base`, default
  `https://api.chutes.ai`); override it for an isolated build environment. The GPU-VM build's
  `measurements generate --register rtmr3` step is unaffected (RTMR3 is image-only, no API call).
- **The guest-image build's measurement step now sources known host classes from the API.**
  `chutes-cvm measurements generate` reads the published host profiles (and their fingerprints)
  from the control plane instead of an in-repo baseline registry, so the build host must reach the
  API. The `chutes-miner-vm` build passes `--api-base` (var `measurements_api_base`, default
  `https://api.chutes.ai`); override it for an isolated build environment. It also passes
  `--include-pending` so the build (the authoritative generator) processes host classes awaiting
  generation — turning newly submitted profiles into published measurements — where a third-party
  verification run would see measured classes only. The GPU-VM build's
  `measurements generate --register rtmr3` step is unaffected (RTMR3 is image-only, no API call).
- OPA per-decision logging is now off by default. `opa-config.yaml` hardcoded
  `decision_logs.console: true`, which wrote the full AdmissionReview input (complete pod specs) to the
  journal for every admitted object — high-volume noise that evicted boot/attestation history from the
  journal window, and tenant workload detail in a log the miner can read over the status API. The config
  and unit are now templated, so the existing `opa_decision_logs` and `opa_log_level` variables are live
  rather than dead; debug builds can opt back in with `-e opa_decision_logs=true`.
- **The `luks` guest-build role is now `prepare-boot-image`.** It always did more than LUKS —
  encryption/debug-init, mount-config rewrite, boot + attestation initramfs scripts, the RTMR3
  canonical manifest, and the final measured initramfs — and the RTMR3 manifest generation moved
  into it (it can't be separated from building the post-encryption initramfs). Operators selecting
  this stage by tag now use `--tags prepare-boot-image` instead of `--tags luks`.
- `system-manager`'s uid/gid are pinned to the literal `10150` instead of
  `{{ system_manager_uid | default(10150) }}`. Nothing ever defined those variables, and
  `setup-cache.sh` now chowns the XDG cache dir numerically, so the value must not vary.
- AppArmor shell policy is now composed rather than monolithic. A new `sek8s-shell-base`
  abstraction holds the permissions every shell profile shares, so a profile is expressed as
  "this base, plus which denies apply" instead of a hand-written allowlist — which matters under
  poweroff-on-failure orchestration, where an allowlist fails the VM for every rule its author
  forgot. `sek8s.deny-sensitive-default` is unchanged in posture: it still denies both the model
  cache and `/run/chutes`, and additionally hides the staging area described below.
- Cluster-init scripts that need a file from `/run/chutes` now run under a
  `sek8s.k3s-init.<script>` profile applied via `aa-exec`. Each still denies the model cache and
  still denies `/run/chutes` itself; the unconfined `k3s-post-start.sh` wrapper stages only that
  script's declared files into `/run/k3s-init/<script>/`, read-only, and removes them when the
  script exits. `/run/chutes` is never narrowed to make room, because AppArmor gives `deny`
  precedence over any allow and carving out exceptions would turn a fail-closed blanket into a
  denylist that silently fails open when a secret is added. Cross-script isolation comes from the
  wrapper running scripts sequentially and removing each directory before the next starts, not
  from the profiles themselves.
- Per-script access is declared in three places by design — the profile in
  `/etc/apparmor.d/sek8s.k3s-init`, the staging map in `k3s-post-start.sh`, and the check list in
  `verify-apparmor-profiles.sh`. A script absent from all three stays on the restrictive default
  profile; missing one of the three is a loud boot failure rather than a silent grant.
- `k3s-post-start.sh` no longer powers the VM off on debug builds. A failed init script previously
  powered off regardless of build type, which made the debug image unusable for diagnosing exactly
  those failures. Gated on `K3S_POST_START_DEBUG`, set by a systemd drop-in written on both builds
  and covered by the existing `/etc/systemd/system` measurement; unset means false.
- Cluster-init scripts can once again honour their own fail-closed handler. The blanket profile
  denies `capability sys_boot`, so every `FATAL: powering off VM` silently failed with
  `Failed to poweroff: Operation not permitted` and the run continued. The per-script profiles do
  not deny it.
- The RTMR3 manifest generator pins `LC_ALL=C` when sorting directory contents. `sort` is
  locale-sensitive, so collation differences between build hosts would reorder the manifest,
  changing its bytes and the initramfs that embeds it — moving RTMR2 with no content change.
- Admission control now restricts which container may mount the shared cache root. The
  `/var/snap/cache` hostPath volume exists so the cache-cleaner init container can scan the tree
  for eviction; because volumes are pod-scoped, any other container in the same pod could mount it
  too. Only the cache-cleaner init container may now do so, matched on the same container name and
  signed image that already gate its root privilege. Containers keep unrestricted access to their
  own per-chute cache directory.
- Container capability admission is now an allowlist instead of a denylist. Only `IPC_LOCK` and
  `NET_BIND_SERVICE` may be added; every other capability is rejected, including the literal
  `ALL` and any spelling variant (`CAP_` prefix, lowercase). Previously the rule matched against
  a fixed list of eight names, so capabilities outside that list were admitted.
- Chute pods may no longer set container lifecycle hooks (`postStart`/`preStop`). Hooks take
  arbitrary arguments and run as a separate process, so they bypassed the rule that restricts
  containers to their signed image's entrypoint. Applies to all containers, init containers and
  ephemeral containers across Pod, Deployment, StatefulSet, DaemonSet, ReplicaSet, Job and
  CronJob. No existing chute spec uses lifecycle hooks; exec probes are unaffected.
- Chute pod probes that use `exec` must now be a plain localhost `curl`. The port and path stay
  free so the health endpoint can change without a policy change, but the command is pinned to
  `/bin/sh -c` with an anchored form, an explicit curl flag set, and a `127.0.0.1` target, so a
  probe cannot carry additional shell commands or reach off the pod. `httpGet` and `tcpSocket`
  probes are unaffected.
- Admission control can now bind a chute pod's per-chute cache directory to the chute identified in
  its launch token. The token's signature is verified in policy, and the mount must match the
  `chute_id` it carries, so the directory a pod may open is no longer determined by a value the
  operator writes. The check activates once the launch-config public key is provisioned into the
  admission controller's config data; it is inactive until then.
- The environment-variable allowlist now applies to init containers and ephemeral containers, not
  only to a pod's main containers. Previously those were unchecked, so an init container could
  carry any variable — including ones explicitly forbidden elsewhere. The cache-cleaner init
  container's own variables were added to the allowlist as part of this.
- Chute workloads may no longer declare `projected` volumes. A projected volume can carry a
  `serviceAccountToken` source, which is honoured regardless of `automountServiceAccountToken:
  false`, so chute code could obtain a Kubernetes API token. Other pods in the namespace are
  unaffected — the cluster's own cleanup job legitimately uses one.
- Chute workloads may no longer select a service account. Previously nothing restricted
  `serviceAccountName`, so a chute could run as an account with broader namespace permissions.
  Chutes now run as the unprivileged default account, matching what they were already deployed
  with. Other pods in the namespace are unaffected.
- The boot-time AppArmor enforcement check is now a hard dependency of `k3s.service` and
  `system-manager.service`. It previously ran only because it was enabled for
  `multi-user.target`, and systemd enablement lives in symlinks, which the RTMR3 measurement
  does not cover — so deleting one symlink disabled the check without changing the measurement.
  The new dependencies live in drop-ins under `/etc/systemd/system`, which is measured, matching
  how `rtmr3-verify.service` is already anchored. Neither service can start unless every sek8s
  profile is loaded in enforce mode.
- Chute workloads must now declare a container named `chute` and a non-empty
  `chutes/config-id` label. The in-guest log shipper finds a chute pod by that label and
  reads only that container, so a pod spec missing either was captured by nothing —
  and because the pod spec is authored outside the guest, that was a way to opt out of
  log capture entirely. Nothing else consulted either value, so neither omission was
  visible before. Real chute specs already set both.
- `/etc/chute-log-shipper` is now covered by the RTMR3 measurement. It holds the validator
  URL logs are sent to and the settings that decide which pods are captured, and it was the
  only sek8s service configuration left out — so an edit to it was the one that would not
  have shown up in the measurement.
- `/usr/lib` is now covered by the RTMR3 measurement on both images. The measured set
  previously included the system binaries but not the shared libraries those binaries load
  — half the executable surface, and the one the confined services are granted read and
  execute access to. It also brings the kernel modules on disk into the measurement, which
  the boot-time measurement did not reach. Costs roughly ten seconds of boot.
- Chute workloads may no longer set container `args`. Overriding a container's command was
  already blocked, but Kubernetes offers two ways to shape what a container runs: with the
  command omitted, `args` replaces the image's own default and is handed to its entrypoint.
  Restricting one and not the other enforced half the guarantee. Real chute specs set no args
  at all: the init container runs its image entrypoint configured by environment variables,
  and the chute container uses the sanctioned command form, which already carries the
  arguments it needs.
  No image shipped today acts on such arguments, so nothing was exploitable; the rule closes
  the asymmetry so a future image that does act on them cannot slip through unnoticed.
  Applies to main, init and ephemeral containers; other pods in the namespace are unaffected.

### Fixed
- `nvidia-fabricmanager` is no longer reported as unhealthy when it is intentionally masked (valid on non-NVLink hosts). The services overview now returns `ok` in this configuration instead of incorrectly reporting `degraded`.
- Debug guest images (`debug_build: true`) shipped key-only: the debug-credentials play
  edited the main `sshd_config`, but Ubuntu's `sshd_config.d/50-cloud-init.conf` drop-in
  (`PasswordAuthentication no`) is Included first and won first-match precedence, so
  password/console access never took effect. The play now writes a `00-debug-access.conf`
  drop-in that sorts ahead of the cloud-init one, restoring root password SSH login.
- AppArmor service profiles now load and enforce — they were missing
  `include <tunables/global>`, so the policy failed to parse and silently did not confine.
  Each profile now carries a least-privilege capability set (e.g. `setup-cache` gets
  `chown`/`fowner`/`fsetid`; the shared default allows the base set but denies
  `sys_module`/`mac_admin`/`mac_override`/`sys_rawio`/`sys_boot`). Debug builds load the
  profiles in complain mode so a policy gap logs a denial instead of poweroff-bricking the VM.
- Production guests no longer power off during k3s cluster init. `03-k3s-validator-auth.sh` read
  `/run/chutes/validator-ss58` directly, but cluster-init scripts are launched as `bash <script>` —
  exec'ing `/usr/bin/bash` by name, which `@{confined_bins}` auto-attaches to
  `sek8s.deny-sensitive-default`, whose `sek8s-secrets-deny` abstraction denies `/run/chutes/**`.
  The read returned EACCES, the script exited non-zero, and the wrapper's fatal handler powered the
  VM off. The `k3s-post-start.sh` wrapper runs unconfined, so it now reads the value and exports
  `VALIDATOR_SS58` to the init scripts, crossing the profile boundary the filesystem cannot.
- `system-manager` no longer fails to start in production. `/var/snap/cache/.xdg-cache` was created
  by a `+`-prefixed `ExecStartPre` in the `cache-volume.conf` drop-in; the `+` prefix makes systemd
  skip `AppArmorProfile=`, so `/bin/bash` auto-attached `sek8s.deny-sensitive-default` and was
  denied `/var/snap/cache/**`. The directory is now created by `setup-cache.sh`, which owns the
  cache-volume layout and already runs as root under `sek8s.setup-cache`.
Both failures were invisible on debug images, which load the sek8s profiles in complain mode.
- Guest image builds are reproducible across rebuilds of identical source again. Five values
  changed on every build and all reached RTMR2, so no two builds produced the same measurements —
  defeating the third-party verification `inventory-reproduce.yml` documents. Continues the same
  effort as the earlier admission-controller TLS and pinned-kernel fixes.
  - **LUKS container UUID** — random per `luksFormat`, and written into `cryptroot/crypttab`
    inside the initramfs. Now derived from version + build type via `to_uuid` and applied with
    `cryptsetup luksUUID`, with an assertion that the pin took. (`community.crypto.luks_device`'s
    `uuid:` parameter cannot do this — it is a selector for *finding* a container, ignored
    entirely when `device:` is given, so setting it fails silently.)
  - **ext4 root filesystem UUID** — random per `mkfs.ext4`, and written into the kernel cmdline as
    `root=UUID=` by `stage-boot-artifacts`. Pinned the same way.
  - **`k3s-install.sh`** — fetched unpinned from `get.k3s.io` into `/usr/local/bin`, which is
    measured wholesale, and never used again after install. Removed once k3s is installed, so
    image measurements no longer depend on what upstream happened to serve at build time. The k3s
    binary itself stays version-pinned via `INSTALL_K3S_VERSION`.
  - **`overlayroot` and `mdadm` initramfs hooks** — both unused cloud-image features that wrote
    per-build data into the initramfs: `overlayroot` a fresh `/.random-seed`, `mdadm` a generation
    timestamp in `mdadm.conf`. Their hooks are now removed before the final `update-initramfs`.
    RAID is a host concern here (`ansible/host/playbooks/storage-setup.yml` builds `md0`); the
    guest is handed individual virtio-blk devices. Removing the hooks rather than the packages
    avoids dependency risk and is durable, since no apt operation follows in the build.
  Neither pinned UUID is secret: the LUKS key is rotated on first boot and the base image is copied
  per VM. Deriving both from version and build type keeps them stable across rebuilds, distinct per
  version, and never shared between a debug and a production image.
- The initramfs is packed reproducibly, which was the last source of RTMR2 drift. Even with
  byte-identical content, two builds produced different archive bytes: `mkinitramfs` stages files
  with `cp -pP`, so every cpio member carries its source mtime, and those vary per build.
  Confirmed by extracting two builds' initramfs images — no differing files, no size differences,
  different hashes.
  `SOURCE_DATE_EPOCH` is now exported for `update-initramfs`. `mkinitramfs` itself never mentions
  the variable, which is misleading: it builds a sorted manifest (`LC_ALL=C sort | uniq`, so member
  ordering was already deterministic) and hands it to `3cpio --create`, and `3cpio` is what reads
  `SOURCE_DATE_EPOCH` to fix member mtimes. The `amd64_microcode` hook drives it the same way.
- The initramfs hook now stages every binary its boot scripts use. `fetch_key` copied `curl`, `jq`,
  `openssl` and friends but not `head`, `tr`, `cut`, `sed`, `awk` or `wc` — those were present only
  because an unrelated package hook happened to stage them. Removing the unused `overlayroot` hook
  took them away, and the guest failed to boot with
  `fetch_key_and_unlock: line 153: head: not found`, aborting the LUKS unlock. Ubuntu initramfs
  ships klibc-utils rather than busybox, which provides none of them.
  A build-time check now lists the produced initramfs and fails the build if any required binary is
  absent, so this class of breakage surfaces at build time instead of at boot.
- `chute-log-shipper` can capture chute pod logs in production again. It failed with
  `[Errno 13] Permission denied: /var/log/pods/chutes_<pod>/<container>`, and the cause was the
  AppArmor profile, not the unit: the `10-security.conf` drop-in already grants
  `CAP_DAC_READ_SEARCH` so the unprivileged service can traverse kubelet's root-owned `0750` log
  dirs, but the profile never permitted the capability's use. Systemd granting a capability does
  not make AppArmor allow it. Adding `capability dac_read_search` to the profile fixes it.
  This only ever failed in production: debug images load the profile in complain mode, where the
  capability is permitted, so shipping worked there and the matching `dac_read_search` entry looked
  like harmless audit noise.
- `/run/chutes` is now unreadable except to the services that declare what they need from
  it. Its permissions decide whether four non-root services can reach secrets like the mTLS
  client key, but no script actually set them: the directory was created as a side effect of
  `mkdir -p` on a subdirectory, which leaves intermediate components at the umask default,
  and six later attempts to set the mode were silently no-ops because it already existed. It
  is now created 0700 everywhere, and the three services that genuinely read from it each get
  a private view containing only their own subtree. The fourth turned out to need nothing at
  all — its config file is read by the init system before it drops privileges — so its two
  read grants have been removed.
- The log shipper can no longer reach the container runtime socket directly. It discovers
  pods through a wrapper that allows two read-only commands, but the wrapper ran with the
  service's own permissions, so anything that compromised the service could skip it and drive
  containerd itself — pulling and running any image, with neither admission control nor
  signature verification in the way. Pod discovery now transitions into a separate, tighter
  profile that owns the socket, and the service's own profile has neither the socket nor a
  shell. The wrapper itself was rewritten from bash to POSIX sh in the same change: bash runs
  whatever `$BASH_ENV` points at before the script starts, which would have let a caller run
  its own code inside the tighter profile without passing the allowlist at all. This matters
  because the log lines the service parses are written by the chutes it watches, which makes
  it the one component with a genuinely hostile input.
- Cache setup no longer fails to boot when the model cache contains a directory it cannot read.
  The recursive ownership pass ran before the mode repair and the service is configured to power
  the VM off on failure, so a workload leaving an unreadable directory in the cache prevented the
  VM from starting again. Directory modes are now repaired before ownership is changed, so the
  walk can descend regardless of what a workload left behind.
- The RTMR3 measurement now has a single implementation. Four separate pieces of code decided
  which files RTMR3 covers, in what order, and how each is hashed — the initramfs measurer, the
  build-time manifest generator, `rtmr3-verify`, and the host-side measurement predictor — kept in
  step by a comment asking maintainers to update them together. They had drifted apart in four
  ways: only some stripped inline comments and trailing whitespace from `tdx-measure.conf`; a conf
  entry that was itself a symlink was measured by two of them and skipped by the other two (and for
  a symlinked directory the boot measurer silently covered *nothing* while the verifier expected
  the whole tree); the boot-time sort was not pinned to `LC_ALL=C`, so a locale could reorder the
  extend chain; and the build manifest hashed with `sha384sum FILE`, which prefixes its output with
  a backslash for systemd's `\x2d`-escaped unit names, so such a file could never match the hash
  computed at boot and would have powered the VM off on every boot. All four now run one
  `tdx-measure` script, which also refuses a symlinked conf entry outright rather than resolving it
  differently in different places. Measured values are unchanged for the current path list.
- Fixed a trap in the measured-file list: a directory and a file beneath it could both be
  listed, and those files were then measured twice. Nothing was left uncovered, but the
  measurement depended on that redundancy — removing an entry that looked superfluous would
  have changed the measurement, and a mismatch powers the machine off, so a tidy-up would
  have stopped every VM booting and looked like tampering. Each file is now measured once
  however the list names it.
- Cluster-init secrets are now staged root-only. Three secrets are copied into a temporary
  directory for the one boot step that needs each, then removed — but they were copied
  world-readable into a world-traversable directory, widening files that are otherwise
  owner-only for as long as that step ran. Every step already runs as root, so nothing
  needed the wider access.
- The two units that fix up permissions under `/run/chutes` now run their tools directly
  instead of through `/bin/sh`. A shell there auto-attaches the AppArmor profile written for
  stray and escaped shells, which denies reading that directory, so any recursive walk failed
  unless the unit happened to run before AppArmor loaded. `registry-tls-config` was not
  failing — all its arguments are named paths — but the trap is removed so a future recursive
  change cannot hit it, where the group it sets is load-bearing.
- The guest image build no longer aborts at "Configure libvirt to enable dynamic ownership"
  with `The requested handler 'restart libvirtd' was not found`. The handler that restarts
  libvirt was written as a named block, and a block in a handlers file is not notifiable by
  name — so the very first build task that touched libvirt config failed the run outright. It
  is now two handlers sharing a `listen` topic, which keeps the existing modular-libvirt
  (`virtqemud`) first, monolithic (`libvirtd`) fallback behaviour.

### Removed
- Hard-coded validator SS58 (`5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ`) removed from all Ansible role defaults (`common`, `admission-controller`, `attestation-service`, `system-manager`) and inventory files (`ansible/guest/inventory.yml`, `local/inventory.prod.yml`). The `validator` Ansible variable is no longer used anywhere in the guest image build.
- `cosign_chutes_public_key_path`, `cosign_dockerhub_public_key_path`, and `helm_chart_public_key_path` inventory variables removed. Build machines now only require the root PGP public key (`root_signing_key_path`).
- `setup-tls-certs.sh` userspace proxy-cert generator and its wiring in
  `attestation-service-init.service` / `install-attestation-init-service.yml`.
  The proxy server cert is now minted in the initramfs by `setup_vm_tls`.
- `guest-tools/scripts/extract-acpi.sh` — dead: the old host-side ACPI dump that had to
  be hand-synced with the launcher. Superseded by offline generation that shares the
  launcher's exact `QemuCommand` and generates ACPI via `tdx-measure --create-acpi-tables`.
- `guest-tools/scripts/run-image.sh` — dead, unreferenced libvirt/VNC/cloud-init test-boot
  script predating the current `run-td` flow.
- `guest-tools/README.md` — the old manual step-by-step measurement guide, superseded by
  build-integrated `compute-rtmrs` + the `guest-tools/measurement/` tooling; the concepts
  now live in `docs/specs/tdx-measurement-verification.md`.
- `setup_vm_tls` no longer generates or registers the VM root CA. It now only signs the leaf certs from the CA generated in init-premount and deletes `ca.key` before `pivot_root`. The dedicated `PUT /servers/{vm}/vm-root-ca` registration call (and its nonce-less quote) is gone.
- The `signing-keys` role no longer installs or stages `gpgv`; the RSA verifier
  (`openssl`) is already staged for the LUKS/TLS paths and is reused.
- Retired the userspace debug k3s secrets-encryption path (build-time static key baked at
  `/etc/chutes` + k3s systemd drop-in). Debug now writes the k3s EncryptionConfiguration from
  initramfs like prod (from a static well-known key), so debug and prod share the boot flow.
- Dropped references to a `k3s-agent` service that the guest never runs. Seven units ordered
  themselves before it and an Ansible handler restarted it; the guest has always installed k3s in
  server mode, so systemd silently ignored the ordering and nothing ever notified the handler.
  `setup-cache.service` was the only unit whose sole ordering constraint was the dead one, and now
  orders against the real service. Also removed an unused `workers` group_vars file, which
  contained only commented-out examples and never loaded.
- Removed the boot-time unit that re-grouped the fetched signing keys, along with the
  `chutes-keys` group it populated. Those keys are public verification keys, written
  world-readable by the initramfs that fetches and signature-verifies them, so the group
  gated nothing — its only observable effect was a unit that intermittently failed. The
  admission controller reads the keys exactly as before, through the bind mount that was
  already doing the work.
- Removed an unfinished module-attestation and module-signing subsystem from the guest
  security role. Neither was ever installed — both task includes were commented out — but
  the leftovers read as live security controls, including a "reporting thresholds" block
  setting a maximum acceptable number of unsigned kernel modules. Security posture here is
  all-or-nothing and is enforced by measurement and AppArmor, so a threshold knob was
  misleading as well as unused. Twelve files, four unused variables and a README entry
  documenting a setting that configured nothing. The binary-attestation timer, which is
  installed and does run, is unaffected.

### Notes
- This change alters the RTMR3 measurement baseline (new AppArmor profile, edited
  service configs) and adds an RTMR2-measured initramfs script; measurement
  re-baselining is handled at release time.
- The chutes-miner chart registry DaemonSet/Service is intentionally NOT removed
  in this change — that retirement is a later, separate step gated on full fleet
  migration.

### Security
- **RTMR3 canonical hashes now cover every measured file, including privileged ones.** The
  boot-time integrity gate (`/etc/tdx-rtmr3-expected-hashes`) is now generated over the fully
  finalized image root — while assembling the boot image, right before the final initramfs is built
  — instead
  of mid-build. Previously it was computed while later build stages could still change files, which
  forced excluding files that aren't final yet (notably `/root/.ssh`, which gates privileged
  access) from pre-verification. Those files were measured into RTMR3 but not checked against a
  build-time constant at boot, so offline tampering wasn't caught by the local power-off gate. The
  gate now hashes each measured file in its true on-disk state, so nothing is excluded and a
  tampered `/root/.ssh`, `/etc/fstab`, or verification tool aborts boot before RTMR3 is extended.
- The `overlayroot` initramfs hook is no longer shipped in the guest. Besides its per-build random
  seed, it installed an `init-bottom` script capable of mounting an overlay over the root
  filesystem, and it sorted ahead of `rtmr3-measure` — an unused root-remount mechanism in the
  measured boot path, ahead of the step that measures the root. Triggering it required either the
  `overlayroot=` kernel cmdline (measured into RTMR2, so detectable) or an edit to
  `/etc/overlayroot.conf` (behind the LUKS key, so post-attestation), but it had no reason to be
  in the boot chain.

## [1.3.1] - 2026-06-20

### Added
- `libnvidia-gpucomp` and `nvidia-persistenced` packages to guest NVIDIA driver install (required by B300 driver stack).
- `nvidia-modprobe` package to DKMS kernel module install (fixes module load on boot).
- NVIDIA apt version pin (`/etc/apt/preferences.d/nvidia-version-pin`, `Pin-Priority: 1001`) locking all `nvidia-*`/`libnvidia-*` packages to `nvidia_pkg_version` so apt cannot pull a mismatched driver build.
- New every-boot post-start step `98-clear-terminal-pods.sh` clears stale pod records, ordered just before `99-purge-kubeconfig.sh` so it still has the admin kubeconfig. For pods whose **controller** owner is a `ReplicaSet` (Deployment), `DaemonSet`, or `StatefulSet`, it deletes terminal `Failed`/`Succeeded` tombstones (graceful delete) and force-deletes (`--force --grace-period=0`) stuck `phase=Unknown` / `status.reason=NodeLost` pods the kubelet/node never confirms — so a controller-managed pod orphaned by an ungraceful reboot (e.g. an attestation-proxy DaemonSet pod) is recreated automatically instead of staying down until a manual `kubectl rollout restart`, and terminal tombstones don't accumulate across reboots. It deliberately **keeps** Job/CronJob pods (retention governed by the Job's own history limits, e.g. `failed-chute-cleanup`), operator one-shot pods not owned by those controllers (e.g. gpu-operator `nvidia-cuda-validator`), bare pods, and Running/Pending pods. Supersedes the never-shipped `05-attestation-proxy-recovery.sh`. The `terminated-pod-gc-threshold` below only caps growth and won't reap a sub-threshold handful, so this is what gives a clean slate each boot. Best-effort and always exits 0 (the post-start runner powers off the VM on any non-zero script).
- `tests/integration/test_reencrypt_secrets_k3s.py`: opt-in (`SEK8S_K3S_IT=1`) end-to-end
  test that drives the real script against a throwaway k3s, reproducing the fresh-volume
  flow (unencrypted boot with a deleted-secret tombstone, then encrypted boot) and
  asserting it finalizes. Added kine `deleted`-column tombstone regression cases to
  `tests/shell/test_reencrypt_verifier.py`.

### Changed
- System manager env: replaced `HF_HUB_DISABLE_XET=1` + `HF_HUB_ENABLE_HF_TRANSFER=1` with throttled XET tuning (`HF_XET_FIXED_DOWNLOAD_CONCURRENCY=16`, `TOKIO_WORKER_THREADS=8`). Enabling XET also requires its cache directory (`/var/snap/cache/.xdg-cache`), now created at runtime via a `system-manager.service.d/cache-volume.conf` drop-in — `ExecStartPre` uses the `+` prefix to run outside the unit sandbox so the chown to `system-manager:tdx` has `CAP_CHOWN`, and `ReadWritePaths=/var/snap/cache` is scoped to the drop-in since the cache volume only exists at runtime, not at image build.
- OPA admission policy: added `HF_XET_FIXED_DOWNLOAD_CONCURRENCY` and `TOKIO_WORKER_THREADS` to allowed pod env vars
- Admission controller source repository moved from `rayonlabs/sek8s` to `chutesai/sek8s` (`admission_controller_repo` default).
- LUKS build dependencies (`cryptsetup`, `dhcpcd-base`, `openssl`, `xfsprogs`, `e2fsprogs`) are now installed via `system_packages` during base image setup instead of a late `chroot apt-get install` in `luks_encrypt.yml`, removing a network-dependent install step from the encryption stage. Packages still land in the final image via the rootfs backup/restore.
- Renamed the k3s boot helpers for clarity: `k3s-config-init.{sh,service}` → `k3s-pre-start.{sh,service}` and `k3s-cluster-init.sh` / `k3s-cluster-init.service.j2` → `k3s-post-start.{sh,service}` (Ansible tasks `setup_config_init.yml` / `setup_cluster_init.yml` → `setup_pre_start.yml` / `setup_post_start.yml`; RTMR3 measurement paths updated to match). Pre-start regenerates the k3s config before the daemon starts; post-start runs the `cluster-init/` scripts afterward.
- k3s now sets `kube-controller-manager-arg: terminated-pod-gc-threshold=50` (default is 12500, so terminal-phase pods effectively never get reaped on a single-node miner). This bounds the `Completed`/`Error` pods and graceful-shutdown tombstones that accumulate across reboots. Set in both `k3s-pre-start.sh` (the authoritative config regenerated each boot) and the role default.
- Pinned the guest HWE kernel to an exact version (`guest_hwe_kernel_version`, currently `6.17.0-35.35~24.04.1`) instead of riding the rolling `linux-image-generic-hwe-24.04` meta. The build installs the meta at the pinned version and drops an apt preferences pin (priority 1001), so the guest kernel — and therefore the RTMR/measurement baseline — is reproducible across rebuilds rather than silently advancing when Canonical ships a new HWE kernel. This is **not** a kernel change versus the previously-running image (it was already on `6.17.0-35`, the current latest HWE for noble; `6.18` is not yet in the archive). Constraints: the kernel must stay `>= 6.16` for the RTMR3 `tsm-mr` sysfs interface; bumping is now a deliberate one-line `guest_hwe_kernel_version` change and opts out of automatic HWE kernel security updates.
- Consolidated boot-time Helm chart management into one source of truth per chart. `04-helm-chart-upgrade.sh` now reconciles each `/etc/chutes/charts/<name>.conf` (version + values + flags) whenever the conf's content hash changes (or the release is `failed`), replacing the three separate mechanisms it had before — `chart-versions/` markers, `chart-configs/`, and `chart-upgrade-overrides/` scripts. Because the trigger is the spec hash, a version bump and a values change are handled identically: values changes now apply (the previous version-drift check skipped them), so the GPU operator's `--disable-openapi-validation` is an `EXTRA_FLAGS` field rather than a bespoke override script. Applied-state markers live on the persisted storage volume, so reconcile is once-per-spec-change with no helm-revision churn.
- Measured the chart specs into RTMR3 (`/etc/chutes/charts` added to the chutes-miner-vm measurement list) so the pinned chart versions, values, and flags cannot be tampered with on the root image.
- Every chart spec must pin an exact `VERSION` — the reconciler refuses to run helm without one (no "latest"/installed resolution), so the measured spec fully determines what runs and a third party can reproduce/audit it. Prometheus is now pinned via a new `prometheus_chart_version` var (build-time install and boot-time reconcile both use it; the monitoring role asserts it is set).
- Chart values live in readable YAML files at `/etc/chutes/charts/values/<chart>.yaml` (loaded via `helm -f`) instead of inline `--set` strings, so values are diffable across versions. The build-time install and the boot-time reconcile use the same values file (single source of truth), and the file is hashed alongside the `.conf` so a values change re-triggers a reconcile. The values dir is under `/etc/chutes/charts`, so it is measured into RTMR3 with the specs.
- The boot-time Helm chart reconcile (`04-helm-chart-upgrade.sh`) is fail-closed for **every** chart, Prometheus/monitoring included: any chart that cannot be reconciled to its measured spec — a failed upgrade, a missing/uninstalled release, an unreadable helm/API state, or a conf missing `RELEASE`/`NAMESPACE`/`CHART` — powers the VM off instead of being skipped, so an attested node either converges to the measured charts or goes down. Each chart first gets a few in-boot retries (`RECONCILE_ATTEMPTS`, default 3; `RECONCILE_RETRY_DELAY`, default 5s) to absorb transients against the local cluster; an unchanged spec is skipped without touching helm.
- Boot-time at-rest re-encryption (`00-reencrypt-secrets.sh`) now **verifies** every secret and configmap is actually encrypted at rest — by inspecting the kine rows directly — before purging history or writing its run-once "done" marker, rather than trusting that `kubectl replace` ran. The verifier returns a boolean status (not a raw failure count) and ignores kine tombstone rows (`deleted != 0`), so secrets/configmaps created and deleted at build time — which the online re-encrypt loop can't reach because the apiserver no longer lists them — aren't misread as live plaintext on a fresh volume; the purge step also scrubs any plaintext left in those tombstone values. The marker is self-validating (honored only when rows are verified sealed), so a stale marker from an earlier false-success run re-runs instead of permanently skipping.
- Hardened the system-manager privileged-remove grant in the guest image. The sudoers rule no longer allows bare `/usr/bin/rm` (root `rm` with any arguments); it now allows only `/usr/local/bin/cache-rm`, a new path-restricted wrapper that refuses to remove anything that isn't a direct child of the HF cache base (rejecting the base itself, deeper subtrees, and symlink/`..` escapes). The wrapper is installed by the system-manager role and measured into RTMR3 (`tdx-measure-miner.conf`) so the root binary the grant points at cannot be swapped on the root image. Only the chutes-miner-vm build installs system-manager, so the wrapper is not present on (or measured in) the tee-gpu-vm image.
- Admission-controller TLS is now fully ephemeral and generated per-VM at boot. `generate-admission-cert` mints a fresh CA + server cert on every boot and injects the matching `caBundle` into the webhook manifests. This makes RTMR3 reproducible across rebuilds — the build-time CA (random key, random serial, build-clock validity dates) was previously drifting the measurement on every build, along with the two webhook manifests that embed its `caBundle`. The CA/server cert that `setup-tls` still generates at build time is a throwaway used only for the build's admission-controller health check; it is stripped before the image is sealed.
- Webhook manifests (`validating-webhook.yaml`, `mutation-webhook.yaml`) now bake a deterministic `caBundle: __ADMISSION_CA_BUNDLE__` placeholder. The real base64(ca.crt) is substituted into the storage (bind-mounted) copy at boot by `generate-admission-cert`, **after** `rtmr3-verify` confirms the placeholder copy still matches the measurement — so the live caBundle is never measured and injection never trips the RTMR3 integrity check. `generate-admission-cert.service` is reordered `Before=k3s.service` to land injection in the post-verify / pre-apply window.
- `generate-admission-cert` now fails closed (powers the VM off) if a webhook manifest is missing at boot, rather than silently skipping injection and leaving the webhook pointing at the placeholder CA. Debug builds set `GENERATE_ADMISSION_CERT_DEBUG_MODE=true` (`/etc/default/generate-admission-cert`) to warn-and-continue for troubleshooting, mirroring `rtmr3-verify` / `gpu-verify`.
- `00-reencrypt-secrets.sh` paths (`STATE_DB`, `ENCRYPTION_CONFIG`, `K3S_CONFIG`,
  `LOG_FILE`) are now environment-overridable (production defaults unchanged) so the
  script can be exercised against an isolated k3s in integration tests. Corrected the
  stale header comment that claimed the build-time `state.db` is deleted on fresh boot.
- InfiniBand setup (`gpu` role) now installs `nvlsm` pinned to `2025.10.12-1` via the new `nvlsm_version` var. NVIDIA's CUDA repo can publish a `Packages` index entry for a newer `nvlsm` before uploading the matching `.deb`, so an unpinned install can resolve a candidate version that 404s and fail the guest image build; bump `nvlsm_version` to the newest version whose `.deb` resolves when updating.
- Guest monitoring now exposes the in-VM Prometheus server on `NodePort` 30090 (`server.service.type=NodePort`, `server.service.nodePort=30090`) instead of the chart-default `ClusterIP`, so the control-plane `chutes-monitoring` federating Prometheus can scrape each TEE VM at `<vm-ip>:30090/federate`. The guest UFW rule for 30090 and the host NodePort range (30000–32767) were already in place; only the service type was missing. Because it ships as a chart spec (`prometheus.conf`), the new hash-triggered reconcile also applies this values-only change on nodes that kept their persisted k3s storage across an image update — which the old version-drift mechanism could not.

### Fixed
- Fix LUKS key confirmation on first boot: freshly provisioned volumes now set the KEY_ADDED flag so confirm_rotation sends rotated=true, preventing the API from discarding the applied passphrase and bricking the volume on subsequent boots
- Normalize PCI BDF addresses in gpu-verify to strip domain prefix and lowercase before comparison, fixing mismatches between sysfs and nvidia-smi formats (e.g. `0000:a1:00.0` vs `00000000:A1:00.0`)
- System manager no longer crash-loops waiting for k3s: made `ReadOnlyPaths=/run/k3s/containerd` optional so the service starts immediately on boot even if k3s hasn't created the socket yet
- Attestation proxy startup probe: increased tolerance from 65s to 310s as a safety net for slow boots
- k3s boot ordering: added `After=attestation-service.service` so the attestation-proxy pod isn't scheduled before the host attestation socket and TLS certs exist
- attestation-proxy no longer fails its startup health probe after a reboot. The proxy's `/health` returns 503 until the attestation Unix socket (`/run/attestation-service/attestation.sock`) is bound, but that socket is created by the host `attestation-service` — a `Type=simple` unit, so systemd considers it "started" at process fork, ~2s before uvicorn actually binds the socket. k3s only orders `After=` that unit, so the proxy could start in the gap and churn on the failing probe (surfacing as `Unknown`/`Completed` pods after reboot). The proxy DaemonSet now has a `wait-for-attestation-socket` init container that blocks on the real socket file before the main container starts.
- Debug-build secrets encryption now actually works (it was silently off). Two defects compounded:
  - The baked debug key base64-decoded to **44 bytes**; secretbox requires exactly **32**, so the apiserver rejected it (`got 44, expected one of [32]`) whenever encryption was actually wired up. Replaced with a valid 32-byte key.
  - `k3s-pre-start.sh` checked for `/run/chutes/k3s-encryption-config.yaml` to decide whether to add `encryption-provider-config`, but on debug builds that file was only copied later by k3s.service's `ExecStartPre` (after pre-start runs), so the arg was never added and the apiserver ran without encryption. Pre-start now materializes the debug key from `/etc/chutes/` into `/run/chutes/` before its own check, matching the boot stage at which prod's initramfs provides it.
- Pods (notably attestation-proxy) no longer get wedged after an ungraceful shutdown/reboot. k3s runs `KillMode=process` and the shutdown backstop force-kills `containerd-shim` to free the storage bind mount, so pods were torn down without the kubelet removing their sandboxes; because containerd's metadata lives on the persistent cache volume, those half-killed sandbox records survived reboot and accumulated, leaving the kubelet holding multiple sandboxes for one pod (a split-brain it never converges). **Graceful node shutdown** is the fix: the kubelet now drains pods on shutdown via a `shutdownGracePeriod` KubeletConfiguration drop-in (written by `k3s-pre-start.sh` into the runtime `kubelet.conf.d`, since `/etc/rancher/k3s` is an initially-empty bind mount), paired with a `logind` `InhibitDelayMaxSec` drop-in so the shutdown inhibitor honours the drain window. Clean shutdowns now remove sandboxes properly instead of force-killing shims.
- Guest image build no longer fails at "Configure NVIDIA Container Toolkit for Docker" on reused build nodes. `nvidia-ctk runtime configure --runtime=docker` reads `/etc/docker/daemon.json` first and aborts with `unable to load config for runtime docker: EOF` when that file is 0 bytes / invalid JSON. This is residual state, not a bug in the pipeline: on a clean rootfs nothing creates an empty `daemon.json` (the `docker.io` package ships none and `nvidia-ctk` starts fine from an absent file), but a build interrupted mid-write on a long-lived/reused build VM can leave a truncated file behind. The gpu role now normalizes `/etc/docker/daemon.json` to valid JSON (`{}`) before invoking `nvidia-ctk`, making the step idempotent across rebuilds.

### Removed
- Removed the attestation-proxy `wait-for-credentials` init container. It re-checked the miner-credentials secret with an in-pod `kubectl get secret`, which requires pod networking (flannel/kube-proxy) to be up — so when the API ClusterIP route wasn't ready yet (early on a fresh boot, or after a sandbox recreate) the call hung and the pod stuck in `Init:0/2`. The secret is already required by the main container via `secretKeyRef` (`MINER_SS58`, `optional: false`), which the kubelet injects over its own host-network API client with no dependency on pod networking — so the gate is preserved and the proxy now reaches Ready independent of pod-network readiness. (Supersedes the earlier `--request-timeout` mitigation, which only turned the hang into an endless retry without removing the pod-network dependency.)
- Removed the boot-time CNI/runtime wipe (`cleanup_stale_runtime_state`) from `k3s-pre-start`. It wiped CNI IPAM (`/var/lib/cni/networks`) out from under containerd's sandbox metadata, which persists on the storage volume across reboots — leaving the old sandboxes un-teardownable (CNI DEL has no IPAM record). The result was an orphaned `NotReady` sandbox pile that grew every boot and a double sandbox-create per pod on each start. Kubelet graceful node shutdown (already configured) is the correct fix: pods drain cleanly so nothing is orphaned, and kubelet reconciles/GCs leftover sandboxes on boot.
- Deleted the unused `chutes-gpu/templates/monitoring-values.yaml.j2` Helm values template. It was never referenced by any task and described a chart structure the guest does not deploy.
- Removed the build-time git-clone settings (`admission_controller_repo`, `admission_controller_version`, `update_repository`, `update_dependencies`) from the admission-controller role defaults.
- `/etc/admission-controller/certs/ca.crt` and `ca.key` are no longer measured in RTMR3 (`tdx-measure-miner.conf`). They are ephemeral per-VM material that cannot — and should not — be reproduced by a third-party auditor. The admission-controller code and webhook rule set remain measured; only the ephemeral CA trust anchor for the loopback webhook is excluded. Build-time throwaway certs are stripped from the image at cleanup so no private key material ships in the sealed image.

## [1.3.0] - 2026-05-18

### Added
- **Ephemeral k3s admin credentials**: The k3s cluster admin kubeconfig is now
  purged at two points in every boot cycle — once during initramfs before any
  userspace runs, and once after cluster initialization completes — so it exists
  only while the cluster is actively serving requests.  Each purge is followed
  by an RTMR3 measurement: if the file is absent (expected), RTMR3 is unchanged;
  if it unexpectedly persists, its hash is extended into RTMR3 and attestation
  will reject the boot.  k3s regenerates the kubeconfig at startup so cluster
  operation is unaffected.
- **LUKS passphrase rotation**: Storage and cache volume passphrases are now
  rotated on every boot.  Rotation uses a two-phase key-slot approach
  (`luksAddKey` then `luksRemoveKey` after API confirmation) so the volume always
  has at least one valid key regardless of crash timing.  A fallback key is
  returned when a previous rotation was interrupted, ensuring clean recovery
  without operator intervention.  Legacy API responses continue to work
  unchanged.
- **k3s cluster secrets encryption**: Kubernetes Secret and ConfigMap values are
  now encrypted at rest.  The encryption key is fetched from the API at boot,
  wrapped with the same boot-token protection as LUKS passphrases, and written
  exclusively to tmpfs (`/run`).  The key is never written to persistent storage.
  A new key is generated when a storage volume is initialised for the first time;
  on all subsequent boots the same key is returned so existing data remains
  readable across reboots and image upgrades.  If the API does not yet supply a
  key, an identity-only configuration is written so k3s starts cleanly without
  encryption (no regression from current behaviour).
- **Expanded RTMR3 coverage**: The TDX RTMR3 measurement chain now covers
  additional deterministic components of the runtime stack.  The initramfs
  pass (pre-pivot_root) measures the sek8s application source, OPA admission
  policies, and k3s cluster-init scripts in addition to the existing system
  files; all newly added paths are also canonical-verified against build-time
  hashes so any offline modification powers off the VM rather than allowing
  a compromised image to boot.  A new `rtmr3-runtime-measure` systemd service
  extends this chain after bind mounts are established and before k3s starts,
  measuring the k3s static manifests from their storage-volume location; this
  confirms that the content k3s actually reads matches what was synced from the
  verified image.
- **`fetch_key` initramfs hook**: Added `sha384sum` to the set of binaries
  included in the initramfs image.
- `roles/rtmr3-measure`: new standalone role that extends TDX RTMR3 at boot
  with SHA-384 hashes of a configurable list of paths (defaults: SSH keys,
  passwd, shadow, sudoers).  An `initramfs-tools` hook bakes the measurement
  script and path config into the initramfs (covered by RTMR1) so neither can
  be tampered with without changing RTMR1.  Any offline modification to a
  measured path — e.g. SSH key injection into an unencrypted image — produces
  a different RTMR3 that verifiers can detect at session start.
- `tdx-rtmr-extend`: small C binary installed to `/usr/local/bin/` that
  extends a TDX RTMR via `/dev/tdx_guest` ioctl (V2 and V3 ABIs) with a
  sysfs fallback for kernels ≥ 6.16.  Does not require libtdx-attest at
  runtime.
- `verify-access-config`: Python script installed to `/usr/local/bin/`
  for use by the partner inside the VM.  Displays SSH keys (fingerprints +
  comments), sshd config, user accounts, password status, and sudo rules.
  Replays the SHA-384 extend chain to compute the expected RTMR3, reads the
  live value from a TDX quote, and reports PASS/FAIL.  The script itself is
  in the measurement list so any tampering changes RTMR3.
- `chutes-miner-vm.yml` and `tee-gpu-vm.yml`: added `rtmr3-measure` play after
  security hardening and SSH key injection so the final on-disk state (including
  partner SSH keys) is what gets measured at boot.
- `guest-tools/scripts/compute-rtmr3.sh`: compute the expected RTMR3 at build
  time by mounting the final qcow2 read-only with `guestmount` and simulating
  the exact SHA-384 extension chain from `rtmr3-measure`. Eliminates the need
  to boot twice just to capture RTMR3 — the Ansible build runs this automatically
  and writes `<image>.rtmr3` alongside the qcow2 before the LUKS step.
- `ansible/guest/playbooks/chutes-miner-vm.yml`, `tee-gpu-vm.yml`: add
  `compute-rtmr3` play that runs `compute-rtmr3.sh` automatically after
  `finalize-vm-image` and before `luks`/`prime-vm`, writing the expected RTMR3
  to `<final_img_path>.rtmr3`.
- `playbooks/tee-gpu-vm.yml`: new dedicated TEE GPU VM build playbook, fully
  independent of `chutes-miner-vm.yml`. Includes `run-vm`, `common`, `gpu`,
  `benchmark`, `harden-ssh`, `lock-accounts`, `disable-console`, `security`,
  `rtmr3-measure`, `setup-ssh-access`, `cleanup-build-vm`, `finalize-vm-image`,
  and `prime-vm`. No k3s, admission controller, system-manager, cache-volume, or
  LUKS plays.
- `benchmark` Ansible role: self-contained TEE GPU VM image setup. Owns the full
  config volume stack (simplified `process-config.py`, `config-manager.service`,
  `var-config.mount`, `config-volume-validator.service`, `netplan-apply.service`)
  in addition to TDX/GPU attestation tools, LUKS helper, and storage setup service.
- `attest` in-VM tool: `attest dump` prints TDX hardware measurements (MRTD, RTMRs,
  MRSEAM); `attest verify` adds NVIDIA NRAS GPU attestation (ES384-signed JWT) and
  optional Intel Tiber Trust Services TDX remote verification.
- `luks-setup` in-VM tool: `luks-setup setup` performs full end-to-end LUKS2
  encryption of the benchmark storage volume (wipe, encrypt, format XFS, mount at
  `/data`); `luks-setup open` unlocks it after a reboot. Both commands default to
  `/dev/chutes-storage` and `/data`, requiring no arguments in the common case.
  The volume is intentionally not persisted to crypttab/fstab — explicit unlock
  is required on every reboot.
- Benchmark VMs use a simplified `process-config.py` (no k3s, miner credential, or
  Docker Hub logic) so the config volume can be created without miner credentials.
- `benchmark-storage-setup.sh` + `setup-storage-bind-mounts.service` (benchmark
  role): at boot, identifies the storage block device, creates a stable
  `/dev/chutes-storage` symlink and `/data` mount point. Auto-mounts the device
  if it already has a filesystem; logs `luks-setup` instructions otherwise.
  Service name matches the production service so existing systemd ordering
  constraints are satisfied without changes to `config-manager.service`.
- `docs/tee-gpu-vm.md`: operator reference for building and launching the TEE GPU VM.
- `docs/tee-gpu-vm-guide.md`: user-facing walkthrough covering SSH access, GPU
  verification, attestation, storage encryption, and network transparency.

### Changed
- **k3s server config**: Added `encryption-provider-config` pointing to the
  ephemeral key path described above.
- **k3s cluster-init**: `k3s-cluster-init.service` keeps `Requires=k3s.service`
  as the correct dependency. The secrets re-encryption script no longer stops and
  restarts k3s to perform the kine purge — DELETE and UPDATE operations now run
  online (SQLite WAL mode allows concurrent access), removing a systemd dependency
  cascade that was previously killing the service mid-run.
- **k3s secrets re-encryption marker**: The completion marker is now written only
  after both the kubectl re-encryption pass and the kine history purge succeed. A
  failed purge previously left plaintext dead rows and `old_value` data permanently;
  it now causes a full retry on the next boot.
- Moved libvirt/VM lifecycle handlers from `roles/run-vm/handlers/main.yml` to the
  top-level `ansible/guest/handlers/main.yml` so they are available to all guest
  roles rather than scoped only to `run-vm`.
- `gpu/tasks/device-setup.yml`: Docker NVIDIA Container Runtime is now configured
  here (alongside containerd) so benchmark images have Docker GPU support without k3s.
- `final_img_path` no longer appends a `-benchmark` suffix; use `build_env: "benchmark"`
  in inventory to produce a dedicated `image/benchmark/<version>.qcow2` output path.
- `chutes-miner-vm.yml` is now production-only — all `benchmark_build` conditions and
  the benchmark tools play have been removed. Benchmark builds use `tee-gpu-vm.yml`.
- **Playbook rename:** `site.yml` → `chutes-miner-vm.yml`. `tee-gpu-vm.yml` is a new
  dedicated TEE GPU VM build playbook (see Added).
- **Role refactor — single responsibility:** `harden-access` and `cleanup` were split
  into focused single-purpose roles. New roles: `harden-ssh` (sshd key-only auth),
  `lock-accounts` (password locking), `disable-console` (getty/serial masking + grub
  cmdline), `remove-ssh` (SSH and sudo removal for production builds), `setup-ssh-access`
  (partner key injection for TEE GPU VM), `cleanup-build-vm` (common build cleanup:
  cloud-init, infiniband, fstrim), `cleanup-orchestration` (k3s/attestation/admission
  teardown for production builds), `finalize-vm-image` (shutdown, undefine, move image).
- **`rtmr3-measure` role enhancements:**
  - Added `/etc/default/grub` to `rtmr3_measure_paths` and `rtmr3_canonical_paths` —
    any change to the GRUB kernel cmdline (e.g. re-enabling console) now changes RTMR3
    and fails the boot-time hash check.
  - `verify-access-config`, `tdx-rtmr-extend`, and `/etc/tdx-rtmr3-expected-hashes`
    are now set `chattr +i` (immutable) after install — accidental modification by root
    is blocked at the filesystem level.
  - `verify-access-config` gains a **Console Access Configuration** section: displays
    GRUB cmdline masking, checks live systemd state for all getty/serial services, and
    returns exit 1 if any console service is `active`.
- **`disable-console` role now sets GRUB default:** `GRUB_DEFAULT=0` and
  `GRUB_SAVEDEFAULT=false` are written to `/etc/default/grub`; `grub-set-default 0`
  pre-populates `/boot/grub/grubenv`. Together these ensure GRUB always selects the
  latest kernel entry deterministically without requiring a prime boot at the GRUB level
  (TDVF EFI variable priming still requires one actual boot cycle via `prime-vm`).
- **`prime-vm` role simplified:** removed ephemeral config/cache/storage volumes and
  dummy credentials. Now launches with `--image` + `--network-type user` only, polls
  the serial console log for `Linux version` (kernel first line), then force-kills.
  Works identically for both `chutes-miner-vm.yml` and `tee-gpu-vm.yml`.

### Fixed
- **LUKS attestation mTLS cert binding**: The ephemeral mTLS client certificate
  generated during `fetch_key_and_unlock` (init-premount) is now preserved across
  init stages so init-bottom (`setup_storage`) uses the same certificate for the
  `/luks/attest` call. The TDX quote REPORTDATA for `/luks/attest` now includes
  `nonce + cert_hash`, binding the quote cryptographically to the certificate
  presented in the mTLS handshake and matching the boot attestation pattern the
  API expects. Previously only the nonce was included, causing a 403 from the API.
- `setup_storage`: add `--batch-mode` and `timeout 60` to all `cryptsetup luksOpen`
  and `luksFormat` calls to prevent indefinite hangs in init-bottom; fix
  `purge_admin_kubeconfig` mount point from `/tmp` (not guaranteed in initramfs) to
  `/run`, and surface the mount error message instead of suppressing it.
- `process-config.py`: `apply_docker_hub_and_registries` now returns early with
  success when the `admission` group is absent and no Docker Hub credentials are
  present, instead of hard-failing. This prevented `config-manager.service` from
  starting in benchmark VMs (which have no admission controller).
- `attest verify`: GPU attestation now passes `options={"ppcie_mode": False}` to
  `get_evidence()`, matching how `chutes_nvevidence.NvClient.gather_evidence()`
  works — fixes evidence collection on H200s in Protected PCIe mode.
- `attest verify`: TDX verification replaced `trustauthority-cli` with
  `dcap_qvl.get_collateral_and_verify()` — no API key or config file required,
  same library used in production validator. `dcap-qvl` is now installed in the
  nvevidence venv.
- `verify-access-config`: `masked-runtime` (services masked via kernel cmdline
  `systemd.mask=`) is now accepted as a valid masked state alongside `masked`.
- Removed stale `trustauthority-cli` install task from `benchmark` role — nothing
  in the codebase calls it; remote attestation is handled by `attest.py` + `dcap_qvl`.
- Console access regression: `disable-console` play was missing from `tee-gpu-vm.yml`
  after role refactor, allowing getty to run. Now included unconditionally for both
  build types.

### Removed
- `roles/harden-access` — split into `harden-ssh`, `lock-accounts`, `remove-ssh`,
  `disable-console`.
- `roles/cleanup` — split into `cleanup-build-vm` (common build cleanup) and
  `cleanup-orchestration` (k3s/attestation/admission teardown for production builds).

## [1.2.0] - 2026-05-05

### Added
- Boot-time Helm upgrade script (`04-helm-chart-upgrade.sh`) refactored into a generic multi-chart dispatcher; per-chart configs in `/etc/chutes/chart-configs/` and optional override scripts in `/etc/chutes/chart-upgrade-overrides/` support custom upgrade logic (e.g. GPU Operator CRD migration)
- GPU Operator boot-time upgrade override script handles CRD migration with `--disable-openapi-validation` and `operator.upgradeCRD=true` for persistent clusters upgrading across major chart versions

### Changed
- Updated sek8s to 0.3.0: HuggingFace cache improvements including download cancellation, stale revision purging, and isolated download subprocess.
- k3s upgraded from `v1.33.7+k3s1` to `v1.35.4+k3s1`
- CUDA toolkit upgraded from `13-0` to `13-2`
- NVIDIA driver package upgraded from `595.58.03-1ubuntu1` to `595.71.05-1ubuntu1`
- GPU Operator Helm chart upgraded from `v24.9.2` to `v26.3.1`; build-time install now uses `operator.upgradeCRD=true`
- Helm CLI upgraded from `v3.11.3` to `v3.20.2`
- OPA upgraded from `0.68.0` to `1.15.2` (0.x to 1.x major bump; existing policy tests confirmed passing)
- cosign pinned to `v2.6.3` (previously fetched `latest` at build time, non-deterministic; fixes CVE-2026-39395)
- `nv-attestation-sdk` constraint bumped from `^2.6.2` to `^2.7.0` in `nvevidence/`

### Fixed
- OPA validating policy (`chutes.rego`) no longer enforces pod-spec rules on Pod UPDATE operations, preventing the Job controller from being permanently blocked when removing tracking finalizers from completed CronJob pods that predate the `automountServiceAccountToken` policy.

## [1.1.0] - 2026-05-04

### Added
- nvidia-imex package (GPU memory mapping over NVLink).
- libnvidia-nscq package (NVSwitch Configuration and Query library).
- DKMS build verification step in device-setup.
- 

### Changed
- NVIDIA 595 drivers — guest stack moves from 590 to 595 driver branch.
  All packages now use unversioned names from the CUDA repo exclusively
  (no Ubuntu restricted packages). DKMS compiles kernel modules at install
  time (no prebuilt linux-modules-nvidia-*-open). Requires nvidia-dkms-open.
- Single version pin: `nvidia_pkg_version` replaces `nvidia_pkg_release_ubuntu`
  and `nvidia_pkg_release_cuda` in group_vars.
- k3s bumped from `v1.33.7+k3s1` to `v1.35.4+k3s1`.
- CUDA version bumped from `13-0` to `13-2`.
- NVIDIA driver package version bumped from `595.58.03-1ubuntu1` to `595.71.05-1ubuntu1`.
- GPU operator helm chart bumped from `v24.9.2` to `v26.3.1`.
- `extract-acpi.sh`: firmware path now overridable via `$TDVF_FIRMWARE` env var (defaults to `firmware/TDVF.fd`). Allows testing `OVMF.inteltdx.ms.fd` without modifying the script.

### Fixed
-

### Removed
- nvidia-utils, nvidia-compute-utils, xserver-xorg-video-nvidia (folded into
  nvidia-driver-open / nvidia-open in 595).
- Prebuilt linux-modules-nvidia resolution and assertion (replaced by DKMS).
- nvidia_pkg_release_ubuntu, nvidia_pkg_release_cuda, nvidia_firmware_pkg
  variables (replaced by nvidia_pkg_version).
-

## [0.2.7] - 2026-03-31

### Added
- RTX Pro 6000 host-side support: profiles for supported Ubuntu releases, BAR-size
  checks, CC-mode verification via GPU tools, topology guidance.
- Guest profiles with vCPU support; fewer CPUs reserved on the host so more capacity
  goes to workloads.
- `setup-tdx-host --install-tools-only` for updating host dependencies and symlinks
  without a full host setup; enables `chutes-reset-gpus`.
- Monorepo package refactor: `src/` layout with `sek8s`, `sek8s-common`, and
  `attestation-proxy` as separate packages. VM image version moved to
  `ansible/guest/VERSION`. Python target aligned to 3.12.

### Changed
- NVIDIA 590 drivers — guest stack moves to the 590 driver / Fabric Manager line.
  Requires VBIOS 96.00.CF.00.xx or newer.
- Docker Hub rate-limit handling and TTL alignment fixes.
- Static manifests and both webhooks refresh more reliably; k3s vs Fabric Manager
  startup ordering tightened; storage mounts carry sync markers to reduce
  corruption risk on bad boots. Webhooks tuned for lower latency.

## [0.2.6] - 2026-03-25

### Fixed
- Updated nvidia-persistenced timeout configuration to prevent service startup
  failures.

## [0.2.5] - 2026-03-24

### Added
- Docker credentials support in the VM. Configure via config file or CLI; the VM
  handles sanitizing and setting up credentials for the admission controller and k3s.
- Pinned helm charts to the VM with automatic upgrade and signature verification on
  boot (no volume refresh required).

### Changed
- Memory arguments updated with numactl args and host-side NUMA config for
  performance optimization. Removed prealloc for shared memory pages (wasted host
  resources).
- VM resources always sync from the root volume so updates apply properly across
  versions.
- Certs and resources in k3s sync from static manifests (resolves SSL errors that
  previously required clearing the storage volume).
- Upgraded to latest 580 NVIDIA driver and Fabric Manager (stability fixes for TEE).

## [0.2.4] - 2026-03-18

### Fixed
- Updated VM launch RAM allocation to be based on GPU type. Previous flat allocation
  was a significant contributor to CUDA and memory issues.

## [0.2.3] - 2026-03-11

### Added
- Base image + overlay architecture with checksum verification in quick-launch.
  Prevents corruption-caused bad measurements and ensures VM + quick-launch version
  pairing matches expected measurements.
- Image management API: pre-populate images hosted by the validator; list, prune, and
  delete images via `chutes-miner tee image-[pull/list/delete/prune]`.
- Download option using aria2c for faster VM image downloads.
- Images moved out of the repo into `var/lib/chutes/`.

### Changed
- Network params tuned: reduced parallelism to address conntrack-related post-activation
  failures (~1-2% failure rate).
- Raw volumes now use XFS to allow for >16TB cache volumes. Existing ext4 volumes are
  backward compatible.

### Fixed
- Fixed a bug that prevented restarting the attestation-proxy in the attestation-system
  namespace (required VM restart in 0.2.1/0.2.2; resolved in 0.2.3 with normal kubectl).

## [0.2.2] - 2026-03-06

### Added
- Raw volume support. Existing qcow2 volumes continue to work; new volumes should use
  raw format.
- Updated cache cleaner image to check for GPU processes and VRAM threshold before
  cleaning.

### Changed
- System manager API updated to improve cache performance and avoid resource constraints
  during concurrent downloads.
- QEMU args updated to improve disk throughput (~50% improvement with raw volumes) and
  network performance.
- HF model download speed improved ~6-10x via system manager API updates.

### Fixed
- Fixed 500 errors from resource constraints during concurrent downloads in the system
  manager API.
