# Feature Spec: AppArmor Hardening + RTMR3 Measurement Expansion

**Date**: 2026-05-25  
**Status**: draft

---

## Context

Defense-in-depth hardening for TDX guest VMs. This spec covers two complementary measures:

1. **AppArmor MAC enforcement** -- restrict access to sensitive paths (model cache, service credentials, runtime sockets) using mandatory access control profiles, then lock down MAC capabilities so profiles cannot be modified at runtime.
2. **RTMR3 measurement expansion** -- extend boot-time integrity measurement to cover all system binaries, service configurations, code injection paths, and systemd units so offline tampering is cryptographically detectable.

- **Packages affected**: Ansible guest roles (`apparmor-hardening` new, `rtmr3-measure`, `admission-controller`, `cache-volume`), no Python code changes
- **Key files**: `ansible/guest/roles/apparmor-hardening/` (new), `ansible/guest/roles/rtmr3-measure/files/tdx-measure-miner.conf`, `ansible/guest/roles/rtmr3-measure/files/initramfs/rtmr3-measure`, `ansible/guest/roles/admission-controller/files/policies/pods.rego`, `ansible/guest/playbooks/chutes-miner-vm.yml`
- **Dependencies**: AppArmor (already installed by `common/container-networking.yml`), `libcap2-bin` (likely already present)

---

## Design Decisions

- **New Ansible role `apparmor-hardening`** rather than extending `cache-volume`. AppArmor is a cross-cutting security concern that protects multiple paths and integrates with RTMR3, OPA, and systemd. A dedicated role with a broad name allows future expansion.
- **Two AppArmor abstractions** (`sek8s-cache-deny` and `sek8s-secrets-deny`) to separate cache protection from credential/socket protection. Services that need cache access don't necessarily need credential access and vice versa.
- **Deny-by-default for shells/interpreters** rather than trying to confine every binary. Confining common shells (bash, sh, dash, python3) and data-handling tools (cat, cp, tar, curl, wget, socat, nc) provides broad coverage. Unconfined binaries are mitigated by RTMR3 binary measurement (Part 2) and noexec on writable tmpfs areas.
- **Boot-time profile verification** via a oneshot systemd service (`verify-apparmor-profiles.service`). Runs after `apparmor.service`, before workload services. Verifies all sek8s profiles are loaded and enforcing. `OnFailure=poweroff.target` ensures the VM never runs workloads without verified MAC enforcement. Runtime profile tampering is prevented by RTMR3 measurement of all profile files and OPA blocking `MAC_ADMIN`/`MAC_OVERRIDE` capabilities for containers.
- **Enforce mode in both builds, audit logging in debug only**. All profiles deploy in `flags=(enforce)` in both debug and production builds so security posture is identical. The only difference: debug builds use `audit deny` rules (blocks AND logs every denial to `journalctl -k`), production builds use plain `deny` rules (blocks silently). This ensures the debug VM matches production behavior exactly while giving full observability via kernel audit log (`journalctl -k | grep apparmor`).
- **Three-tier RTMR3 expansion** with progress logging. The VM image is sealed at build time with no runtime package updates, so measuring all system binaries is safe and comprehensive. Progress logging prevents miners from thinking the VM is stuck during the extended measurement phase.
- **`registries.yaml` excluded from RTMR3**. `process-config.py` modifies it at runtime and the change persists across reboots. The security properties it controls (registry allowlists, hostname resolution, signature verification) are independently measured through other static config files.

---

## API Changes

- **New endpoints**: None
- **Schema changes**: None
- **Migrations**: None

---

## Goal

Success =

1. Sensitive paths (model cache, service credentials, runtime sockets) are restricted by AppArmor MAC profiles -- only explicitly-whitelisted services can access them
2. MAC capabilities are dropped from the kernel bounding set after profile load -- profiles cannot be modified at runtime
3. OPA blocks `MAC_ADMIN` and `MAC_OVERRIDE` capabilities for all containers
4. All system binaries, service configs, code injection paths, systemd units, and AppArmor profiles are measured into RTMR3 at boot -- offline tampering is cryptographically detectable
5. Boot-time progress logging keeps miners informed during the expanded measurement phase

---

## Constraints

- AppArmor is already installed by the `common` role. The new role must not re-install it.
- All AppArmor profiles must be static files deployed at image build time -- no runtime profile generation.
- `verify-apparmor-profiles.service` must run `After=apparmor.service` and `Before=k3s.service,system-manager.service,setup-cache.service`. Failure must poweroff the VM.
- Only build-time static files may be added to `tdx-measure-miner.conf`. Files modified after boot by config-manager, k3s-config-init, or generate-admission-cert are excluded.
- Profiles for `system-manager` must allow the download subprocess (`-m sek8s.system_manager.cache.download`) to inherit cache write access.
- The `rtmr3-measure` initramfs script must log progress for the expanded measurement set (estimated 1500-2000+ files, 30-90 seconds).

---

## Output Format

### Part 1: AppArmor Hardening

#### New role: `ansible/guest/roles/apparmor-hardening/`

```
ansible/guest/roles/apparmor-hardening/
  templates/
    abstractions/
      sek8s-cache-deny.j2              # deny access to HF model cache volume (audit deny vs deny via debug_build)
      sek8s-secrets-deny.j2            # deny access to service credentials, runtime sockets (same toggle)
  files/
    profiles/
      sek8s.system-manager              # allow cache rw, credential read, containerd socket, network
      sek8s.setup-cache                 # allow cache rw, coreutils, no network
      sek8s.deny-sensitive-default      # shell/interpreter confinement with both deny abstractions
    verify-apparmor-profiles.sh          # verify all sek8s profiles are loaded and enforcing
    verify-apparmor-profiles.service     # oneshot, After=apparmor.service Before=k3s.service
  tasks/
    main.yml                            # install abstractions, profiles, lockdown service, enable everything
```

#### Abstraction: `sek8s-cache-deny`

Installed to `/etc/apparmor.d/abstractions/sek8s-cache-deny`. Denies all access to `/var/snap/cache/`. Deployed as a Jinja2 template: debug builds render `audit deny` rules (blocks + logs to kernel audit), production builds render plain `deny` rules (blocks silently).

#### Abstraction: `sek8s-secrets-deny`

Installed to `/etc/apparmor.d/abstractions/sek8s-secrets-deny`. Denies access to service credential files, ephemeral auth tokens on tmpfs, and runtime Unix domain sockets. The full list of protected paths is maintained in the abstraction file itself. Same `audit deny` vs `deny` templating as `sek8s-cache-deny`.

#### Legitimate access matrix

| Service | Cache rw | Cache r | Miner creds | Containerd sock | Network |
|---------|----------|---------|-------------|-----------------|---------|
| system-manager | yes | yes | yes | yes (images) | yes |
| setup-cache.sh | yes | yes | no | no | no |
| k3s (chute pods) | yes (hostPath) | yes (hostPath) | no | yes | yes |
| admission-controller | no | no | no | no | yes |
| attestation-service | no | no | no | no | yes |
| config-manager | no | no | write | no | no |

#### Profile: `sek8s.system-manager`

Named profile applied via systemd `AppArmorProfile=` drop-in (`30-apparmor.conf`). Grants cache read/write, miner credential read, containerd socket access, network access, and subprocess spawning for HF model downloads. Download subprocess inherits parent profile via `ix`.

#### Profile: `sek8s.setup-cache`

Named profile applied via systemd `AppArmorProfile=` drop-in (`30-apparmor.conf`). Grants cache read/write and coreutils execution (mkdir, chown, chmod, mountpoint, logger). No network, no credential access.

#### Profile: `sek8s.deny-sensitive-default`

Confines common shells and data-handling tools with both deny abstractions plus broad allow rules for everything else. Confined executables: bash, sh, dash, cat, cp, tar, rsync, scp, curl, wget, perl, dd, socat, nc, ncat. Python3 is intentionally excluded from auto-attachment — confining it would require explicit `AppArmorProfile=` overrides for every Python-based systemd service. Python launched from a confined shell inherits the profile via `ix`. Deployed in `flags=(enforce)` in both debug and production builds.

#### `verify-apparmor-profiles.service`

Oneshot systemd service (`After=apparmor.service`, `Before=k3s.service`). Verifies all sek8s AppArmor profiles are loaded in enforce mode by reading `/sys/kernel/security/apparmor/profiles`. If any profile is missing or not enforcing, the service fails and `OnFailure=poweroff.target` shuts down the VM. Profile tampering at runtime is further mitigated by RTMR3 measurement of all profile files and OPA blocking `MAC_ADMIN`/`MAC_OVERRIDE` for containers.

#### Modify: `pods.rego`

Add `MAC_ADMIN` and `MAC_OVERRIDE` to `dangerous_capabilities` in `ansible/guest/roles/admission-controller/files/policies/pods.rego`.

#### Modify: `chutes-miner-vm.yml`

Insert `apparmor-hardening` role after `cache-volume` and before `security`. Must come before `rtmr3-measure` so profile files exist when RTMR3 hashes them.

### Part 2: RTMR3 Measurement Expansion

#### Modify: `tdx-measure-miner.conf`

Remove `/etc/rancher/k3s/registries.yaml` (runtime-modified, persists across reboots; security properties independently measured through other files).

Add the following paths:

**Tier 1 -- Custom binaries** (replaces individual file entries):

```
/usr/local/bin
/usr/local/sbin
```

**Tier 2 -- Code injection config paths:**

```
/etc/ld.so.preload
/etc/ld.so.conf
/etc/ld.so.conf.d
/etc/modprobe.d
/etc/modules-load.d
/etc/sysctl.conf
/etc/sysctl.d
/etc/profile
/etc/profile.d
/etc/bash.bashrc
/etc/environment
/etc/crontab
/etc/cron.d
```

**Tier 3 -- System binaries and custom shared libraries:**

```
/usr/bin
/usr/sbin
/usr/local/lib
```

**Service configs not yet covered:**

```
/etc/admission-controller/cosign/cosign.pub
/etc/admission-controller/authorization-webhook-config.yaml
/etc/admission-controller/certs/openssl.cnf
/etc/opa/opa.yaml
/etc/attestation-service/attestation-service.env
/etc/attestation-service/scripts
/etc/chutes
/etc/systemd/system
```

**AppArmor profiles (from new role):**

```
/etc/apparmor.d/sek8s.system-manager
/etc/apparmor.d/sek8s.setup-cache
/etc/apparmor.d/sek8s.deny-sensitive-default
/etc/apparmor.d/abstractions/sek8s-cache-deny
/etc/apparmor.d/abstractions/sek8s-secrets-deny
/usr/local/bin/verify-apparmor-profiles.sh
```

#### Modify: `rtmr3-measure` initramfs script

Add progress logging for the expanded measurement set:

1. Per-directory progress via `/dev/kmsg` and console
2. Periodic progress within large directories (every 100 files)
3. Elapsed time per directory and total summary

Implementation: track directory boundaries in the extend loop, use `date +%s` for timing (busybox-compatible).

### Files intentionally excluded from RTMR3

Files modified at runtime by config-manager, k3s-config-init, or per-boot certificate generation are excluded from measurement. This includes miner credential env files, ephemeral auth env files, registry config (Docker Hub auth merge), k3s runtime config, and per-boot TLS certificates.

### Known remaining gaps (future work)

- `/usr/lib` and `/opt/sek8s/venv` contain too many files for sequential boot-time hashing. Candidates for `dm-verity` or manifest-based measurement.
- Unconfined compiled binaries bypass AppArmor shell confinement. Mitigated by RTMR3 binary measurement and noexec on writable tmpfs. Future: `apparmor.default_profile` kernel boot param (AppArmor 4.x).

---

## Failure Conditions

- Any AppArmor profile breaks a legitimate service (system-manager can't download models, setup-cache can't create dirs, k3s can't serve hostPath volumes). Mitigated by `audit deny` logging in debug builds -- denials are visible in `journalctl -k` while matching production enforcement exactly.
- `verify-apparmor-profiles.service` fails and the VM powers off on every boot. Must be tested in debug builds first.
- RTMR3 measurement of `/etc/systemd/system` includes a runtime-generated unit file, causing rtmr3-verify to fail on reboot. All units must be verified as build-time static.
- The expanded RTMR3 measurement exceeds acceptable boot time (>120 seconds). Progress logging must be validated and timing benchmarked.
- A file added to `tdx-measure-miner.conf` is legitimately modified at runtime, causing RTMR3 mismatch on reboot.

---

## Rollout Notes

- **Enforce from day one**: All profiles deploy in `flags=(enforce)` in both debug and production builds. Debug builds use `audit deny` rules for full observability via `journalctl -k | grep apparmor` (each denied access is logged with profile, operation, path, and mask). Production builds use plain `deny` rules (silent enforcement). Test on debug builds first; if it works there, production behavior is identical.
- **RTMR3 value changes**: The expanded measurement list changes the expected RTMR3 value. The external validator must be updated after the image is rebuilt.
- **Boot time increase**: Estimated 30-90 seconds additional boot time for the three-tier measurement expansion. Progress logging ensures visibility.
- **Image rebuild required**: Both Part 1 and Part 2 require a full guest image rebuild.
- **No backward compatibility issues**: New role is additive. Only change to existing roles is two new entries in OPA `dangerous_capabilities`.
- **`registries.yaml` removal**: Must be removed from `tdx-measure-miner.conf` before the next image build (added by commit 7c97ef1 but runtime-modified).
