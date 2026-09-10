# TDX VM Host Setup Guide

This guide covers setting up a baremetal host to launch TDX-enabled VMs with GPU passthrough.

> Need the full workflow (host prep → VM launch → deploying the Chutes miner)? Start with [`docs/end-to-end-miner.md`](../docs/end-to-end-miner.md) for an end-to-end view, then return here for host-specific details.

## Prerequisites

- **Hardware**: Intel TDX-capable CPU and NVIDIA GPUs. See [Validated host topologies](#validated-host-topologies).
- **OS**: Ubuntu **26.04** — the only supported host OS. `chutes-cvm host setup` has no profile for 25.10 or 25.04, and no other release ships a baselined QEMU; advance an existing host with `upgrade-host.yml -e target_version=26.04` before setup.
- **Access**: Root/sudo privileges on the host; SSH access from the Ansible control machine.

### Validated host topologies

**Validated** means end-to-end tested (TDX host + VM + GPU passthrough). A profile existing in `chutes-cvm host setup` does **not** imply validation.

| Ubuntu | GPU SKU      | GPU count | Status              | Notes |
|--------|--------------|-----------|---------------------|-------|
| 26.04  | H200         | 8         | Validated           | NVSwitch required. Intel DCAP attestation. |
| 26.04  | B200         | 8         | Validated           | Host-side Fabric Manager. CX7 NVSwitch bridge PFs stay on host. See [Blackwell HGX notes](#blackwell-hgx-notes). |
| 26.04  | RTX Pro 6000 | 8         | Validated           | No NVSwitch. Intel DCAP attestation. |

#### Blackwell HGX notes

B200 and B300 use a different NVSwitch architecture from H100/H200. `chutes-cvm host setup` detects and configures both, but only **B200** is in the [validated topologies](#validated-host-topologies) above — B300 host setup works the same way but has not yet been validated end-to-end. Key differences that affect host setup:

- **Host-side Fabric Manager**: NVSwitches are not PCIe devices visible to the guest. `nvidia-fabricmanager` and `nvlsm` run on the *host* and are installed automatically by `chutes-cvm host setup` when B200 or B300 GPUs are detected. The guest's Fabric Manager is masked.
- **CX7 NVSwitch bridge PFs stay on the host**: ConnectX-7 devices acting as the host interface to NVSwitches (identified by `SMDL=SW_MNG` in PCIe VPD) are excluded from VFIO passthrough. Regular CX7 NIC PFs are still passed through normally.
- **Encrypted NVLink (MPT CC mode)**: NVLink traffic between GPUs and the host Fabric Manager is encrypted, so host-side FM does not compromise the zero-trust security model.
- **`nvidia-open` driver**: Required on both host and guest for Blackwell (already the default in the guest image).

---

## Setup via Ansible (Recommended)

The `ansible/host/` playbooks are the primary way to provision and operate hosts. They handle all setup steps end-to-end, including PCCS configuration, and are idempotent.

### Step 1: Provision the host

```bash
cd ansible/host

# Full host setup (TDX kernel, attestation packages, PCCS, chutes dirs):
ansible-playbook -i ~/chutes/my-inventory.yml playbooks/setup.yml \
  -e pccs_api_key=<your-key> -e pccs_password=<your-password>
```

`pccs_api_key` and `pccs_password` must both be set (or both omitted — see Step 2). Obtain your Intel API key from the [Intel Trusted Services portal](https://api.portal.trustedservices.intel.com/).

A reboot is triggered automatically if a new kernel was installed.

### Step 2: Launch the VM

```bash
ansible-playbook -i ~/chutes/my-inventory.yml playbooks/launch.yml
```

This renders `config.yaml` on the host, downloads the base image if missing, verifies its checksum, and launches the VM via `chutes-cvm guest launch`.

### Subsequent updates

```bash
# Update guest image and relaunch:
ansible-playbook -i ~/chutes/my-inventory.yml playbooks/upgrade-guest.yml

# Upgrade host OS (e.g. 25.10 → 26.04):
ansible-playbook -i ~/chutes/my-inventory.yml playbooks/upgrade-host.yml
```

See `ansible/host/README.md` for the full playbook reference including `remediate-host.yml` (fix broken hosts), `shutdown.yml`, and `firewall.yml`.

---

## Manual Setup (Advanced / Direct Script Use)

Use these steps if you are not using Ansible or are working directly on the host.

### Step 1: Install TDX host prerequisites

```bash
git clone https://github.com/chutesai/sek8s.git
cd sek8s/host-tools/scripts
sudo chutes-cvm host setup
sudo reboot
```

After reboot, verify TDX is active:
```bash
dmesg | grep -i tdx
# Expected: virt/tdx: module initialized
```

### Step 2: Configure PCCS

PCCS is the local Intel attestation cache. Configure it with your Intel API key and a password:

```bash
pccs-configure
# Enter your API key and a password when prompted — this runs npm install and
# generates the TLS cert/key under /opt/intel/sgx-dcap-pccs/ssl_key/

systemctl restart pccs

sudo PCKIDRetrievalTool \
  -url https://localhost:8081 \
  -use_secure_cert false
# Expected: "the data has been sent to cache server successfully"
```

Obtain your Intel API key from [api.portal.trustedservices.intel.com](https://api.portal.trustedservices.intel.com/).

**Note:** If PCCS was installed non-interactively (e.g. via `chutes-cvm host setup --noninteractive`) and the service fails with `Cannot find package 'config'`, run:
```bash
cd /opt/intel/sgx-dcap-pccs && npm install
systemctl restart pccs
```

### Step 3: Download the VM image

```bash
cd host-tools/scripts
chutes-cvm image download
```

Images are saved to `/var/lib/chutes/base-images/`.

### Step 4: Create configuration file

```bash
chutes-cvm config init
# Edit config.yaml with your settings
```

Key fields:
- `miner.ss58` / `miner.seed` — substrate credentials
- `network.public_interface` — host NIC name (e.g. `ens9f0np0`)
- `docker_hub` — optional read-only PAT to avoid anonymous Hub rate limits

See [`scripts/config/CONFIG-GUIDE.md`](scripts/config/CONFIG-GUIDE.md) for the full schema reference.

### Step 5: Launch the VM

```bash
chutes-cvm guest launch config.yaml
```

The launcher validates TDX, prepares volumes, configures networking, binds GPUs to `vfio-pci`, and starts the VM.

---

## Management

### VM status and logs
```bash
cat /tmp/tdx-td-pid.pid && ps -p $(cat /tmp/tdx-td-pid.pid)
tail -f /tmp/tdx-guest-td.log
cat /tmp/qemu.log
```

### Stop and clean up
```bash
chutes-cvm guest down
```
Removes the VM process, bridge, TAP interfaces, and NAT rules. Volume files are preserved.

### GPU management
```bash
# Query GPU CC/PPCIe mode
sudo nvidia-gpu-tools --query-cc-mode

# Secondary Bus Reset (all GPUs — stop VM first)
chutes-cvm host reset-gpus

# Recover a broken GPU
sudo nvidia-gpu-tools --recover-broken-gpu --gpu-bdf=<bdf>
```

Refresh host dependencies on an existing machine (no full re-setup needed):
```bash
sudo chutes-cvm host setup
```

---

## Troubleshooting

**PCCS fails with `ERR_MODULE_NOT_FOUND` / `Cannot find package 'config'`**
```bash
cd /opt/intel/sgx-dcap-pccs && npm install && systemctl restart pccs
```
Caused by non-interactive install skipping the `npm install` post-install step.

**GPU stuck or unhealthy**

If `chutes-cvm guest launch` or `chutes-cvm host reset-gpus` hangs, check for wedged PCI tasks:
```bash
ps aux | awk '$8 ~ /D/ && /nvidia-gpu-tools|vfio-pci\/unbind/'
```
When that shows D-state processes, **reboot the host** before retrying — SBR cannot run while those are stuck.

B200/B300 use CC mode (not PPCIe); use CC-mode SBR flags:
```bash
chutes-cvm host reset-gpus    # auto-selects flags from detected GPU type
sudo nvidia-gpu-tools --reset-with-sbr --reset-after-cc-mode-switch --gpu-bdf=<bdf>
```
H200 8-GPU PPCIe configs:
```bash
sudo nvidia-gpu-tools --reset-with-sbr --reset-after-ppcie-mode-switch --gpu-bdf=<bdf>
```

**TDX not initialized after reboot**
```bash
dmesg | grep -i tdx
# If blank: verify GRUB entry via `grub-editenv list`; re-run chutes-cvm host setup if needed
```

**Network not accessible**
```bash
ip link show br0 && ip link show vmtap0
sudo sysctl net.ipv4.ip_forward       # should be 1
```

---

## File Locations

| Path | Description |
|------|-------------|
| `/var/lib/chutes/base-images/tdx-guest/` | Base VM image set (qcow2 + boot artifacts + manifest.json) |
| `/var/lib/chutes/vm-images/tdx-<hostname>-<sha>.qcow2` | Per-VM image copy |
| `<volumes.cache.path>` (e.g. `/data/prod/cache.raw`) | HF/model cache volume (XFS) |
| `<volumes.storage.path>` (e.g. `/data/prod/storage.raw`) | k3s/containerd/kubelet volume |
| `<volumes.config.path>` (e.g. `/data/prod/config.qcow2`) | Credentials config volume |
| `/tmp/tdx-guest-td.log` | VM serial console log |
| `/tmp/qemu.log` | QEMU debug log |
| `/tmp/tdx-td-pid.pid` | VM process PID |

Set absolute `volumes.*.path` values in `config.yaml` (the Ansible-rendered config does).
An empty `path:` auto-generates `cache-<hostname>.raw` etc. **relative to the launch working
directory**, so prefer absolute paths for anything long-lived.

---

## Additional Documentation

- [Ansible Host Playbooks](../ansible/host/README.md) — full playbook reference
- [Configuration Guide](scripts/config/CONFIG-GUIDE.md) — full config schema
- [Intel TDX Documentation](https://www.intel.com/content/www/us/en/developer/tools/trust-domain-extensions/overview.html)
