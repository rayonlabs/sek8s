# TEE GPU VM

Operator reference for building and launching the TEE GPU VM. For the partner-facing
walkthrough, see [tee-gpu-vm-guide.md](tee-gpu-vm-guide.md). For the development debug
build, see [debug-mode.md](debug-mode.md).

## Overview

The TEE GPU VM builds a standalone guest image for partner access — NDA evaluation
sessions, training runs, or any GPU workload where the partner requires a hardware-
attested, tamper-evident environment. The image runs the full GPU stack without
Kubernetes orchestration and provides SSH access via partner-supplied keys measured
into RTMR3 at boot.

## What changes vs. a production image

| Behaviour | Production | TEE GPU VM |
|-----------|-----------|------------|
| LUKS encryption | yes | no |
| SSH access | no | partner keys only |
| K3s / Kubernetes | yes | no |
| Chutes orchestration services | yes | no |
| SSH hardening (`sshd_config`) | no | yes (key-only auth) |
| Account password lock | yes | yes |
| Console access disabled | yes | yes |
| RTMR3 access config measurement | yes | yes |
| `attest` tool | no | yes |
| `verify-access-config` tool | no | yes |
| Host network logging | no | yes (`benchmark-netlog`) |
| Image suffix | `.qcow2` | `-benchmark.qcow2` |

## Building the image

### 1. Set inventory variables

Edit `ansible/guest/inventory.yml`:

```yaml
all:
  vars:
    benchmark_build: true
    benchmark_ssh_keys:
      - "ssh-ed25519 AAAA... partner@example.com"
      # add more keys as needed
```

`benchmark_ssh_keys` is required — the build will fail if it is empty so that an
image with no partner access is never shipped.

### 2. Build

```bash
cd ansible/guest
ansible-playbook -i inventory.yml playbooks/tee-gpu-vm.yml
```

The resulting image set is written to its own directory:

```
<img_dir>/<build_env>/<vm_version>/<vm_version>.qcow2   (+ .vmlinuz/.initrd/.cmdline, manifest.json)
```

### 3. Reset inventory after building

Clear `benchmark_build` and `benchmark_ssh_keys` before any subsequent non-TEE builds.

## Launching the VM

Use `chutes-cvm guest launch` with the `--benchmark` flag:

```bash
cd host-tools/scripts
cp config/config.benchmark.example.yaml config.yaml
# Edit config.yaml — at minimum set network.public_interface and network.vm_ip
chutes-cvm guest launch --benchmark config.yaml
```

The `--benchmark` flag:
- Sets the default base image to the `tdx-guest-benchmark/` image set
- Skips cache and config volume setup
- Auto-installs and starts the `benchmark-netlog` service on the host
- Skips miner credential validation (only hostname is checked)

### Example config

See [`host-tools/scripts/config/config.benchmark.example.yaml`](../host-tools/scripts/config/config.benchmark.example.yaml)
for a ready-to-use template.

## Security model

### Access hardening

Unlike production builds, the TEE GPU VM ships SSH as the only access path. The
build process unconditionally applies:

- **`harden-ssh`** — key-only authentication (`PasswordAuthentication no`,
  `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password`)
- **`lock-accounts`** — root and all non-system accounts are password-locked
- **`disable-console`** — getty/serial console services masked; kernel cmdline
  includes `systemd.mask=getty@tty1.service systemd.mask=serial-getty@ttyS0.service`

### RTMR3 access configuration measurement

Every file controlling access to the VM is measured into RTMR3 in the initramfs at
every boot, before any userspace process runs:

- `/etc/ssh/` — SSH host keys and daemon configuration
- `/etc/pam.d/` — PAM authentication stack
- `/root/.ssh/authorized_keys` — partner's authorised keys
- `/etc/passwd`, `/etc/shadow` — user accounts and password state
- `/etc/sudoers`, `/etc/sudoers.d/` — privilege escalation rules
- `/etc/default/grub` — boot cmdline (includes console masking)
- `/usr/local/bin/verify-access-config` — the verification script itself

Additionally, the initramfs contains expected SHA-384 hashes for canonical files
(all of the above except `authorized_keys`). If any canonical file is tampered with
offline, the VM powers off at boot rather than starting with a compromised state.

The partner uses `verify-access-config` to replay the RTMR3 measurement chain and
confirm a PASS against the live TDX quote. See
[tee-gpu-vm-guide.md](tee-gpu-vm-guide.md#access-configuration-verification).

## In-VM attestation: `attest`

The `attest` tool is installed at `/usr/local/bin/attest` and runs inside the VM.
It requires membership of the `tdx-attest` group (the root user qualifies by default).

### Commands

#### `attest dump`

Generates a TDX quote and prints the VM's hardware measurements — MRTD, RTMRs,
MRSEAM, MRSIGNERSEAM, and TEE TCB SVN. No external services are contacted.

```
$ attest dump
Field            Value
──────────────────────────────────────────────────────────────────────
MRTD             a3f2...
RTMR[0]          00000...
RTMR[1]          4c8b...
RTMR[2]          f19d...
RTMR[3]          a7c3...   # access config measurement
...
```

Use `--json` for machine-readable output:

```bash
attest dump --json
```

#### `attest verify`

Performs full attestation:

1. **TDX measurement dump** — same as `attest dump`
2. **GPU attestation** — sends GPU evidence to NVIDIA's Remote Attestation Service
   (NRAS). Returns an ES384-signed JWT that can be independently verified against
   NVIDIA's public certificates.
3. **TDX remote verification** — verifies the TDX quote against Intel Tiber Trust
   Services. Requires `/etc/tdx-attest-config.json` with a valid Intel API key;
   skipped gracefully if the file is absent.

```bash
attest verify
# or with a custom Intel config:
attest verify --tdx-config /path/to/tdx-attest-config.json
# or JSON output:
attest verify --json
```

### Intel TDX config

`/etc/tdx-attest-config.json` is partner-provided and is not baked into the image.
If absent, `attest verify` still completes GPU attestation and prints a clear message
explaining that TDX remote verification was skipped.

## Storage encryption: `luks-setup`

`luks-setup` is installed at `/usr/local/bin/luks-setup` and must be run as root.
It provides two subcommands:

### `luks-setup setup`

One-time setup: wipe, LUKS2-encrypt, format (XFS by default), and mount for the
current session. No entries are written to `/etc/crypttab` or `/etc/fstab` — the
volume must be explicitly unlocked via `luks-setup open` after each reboot.

```bash
luks-setup setup /dev/vdb /data
# with a custom label:
luks-setup setup /dev/vdb /data --label mydata
# with ext4 instead of XFS:
luks-setup setup /dev/vdb /data --fs ext4
# skip confirmation prompt:
luks-setup setup /dev/vdb /data --yes
```

### `luks-setup open`

Open and mount an already-encrypted device (manual use or recovery):

```bash
luks-setup open /dev/vdb /data
```

## Host-side network logging

When launched with `--benchmark`, `chutes-cvm guest launch` installs and starts the
`benchmark-netlog` systemd service on the host. It uses `conntrack` to stream all
connection events for the VM's bridge subnet, writing them to daily log files:

```
/var/log/chutes/benchmark-netlog/netlog-YYYYMMDD.log
```

Logs rotate daily, compressed after one day, and retained for 90 days.

### Useful commands

```bash
# View live connections
journalctl -u benchmark-netlog -f

# Check today's log
tail -f /var/log/chutes/benchmark-netlog/netlog-$(date +%Y%m%d).log

# Service status
systemctl status benchmark-netlog
```

The bridge subnet logged can be overridden via `/etc/chutes/benchmark-netlog.env`:

```ini
BRIDGE_SUBNET=192.168.100.0/24
```

## Security notes

- TEE GPU VM images contain the partner's SSH public keys as the **only** authorised
  keys. The builder's cloud-init keys are removed during the cleanup phase. The build
  asserts that `authorized_keys` contains exactly the keys in `benchmark_ssh_keys`
  — count and content — and halts if the assertion fails.
- TEE GPU VM images have no LUKS encryption on the root volume. Treat the image file
  as sensitive; delete it after the session ends.
- `benchmark_build: true` should never be set when building production or miner images.
- The partner should run `verify-access-config` at the start of every session to
  confirm the access configuration matches what was built into the image.
