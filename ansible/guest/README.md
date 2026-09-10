# Secure Kubernetes TDX VM Builder

This Ansible automation builds production-ready Intel TDX (Trust Domain Extensions) virtual machine images for trustless, distributed computing networks. The resulting VMs provide hardware-enforced confidential computing with comprehensive attestation capabilities.

## Overview

The build process creates a hardened Ubuntu VM image with:

- **Intel TDX confidential computing** - Hardware-isolated execution environment
- **Full-disk LUKS encryption** - Root filesystem encrypted, unlocked via TDX attestation API
- **Kubernetes (k3s)** - Lightweight container orchestration
- **NVIDIA GPU support** - H200 GPU passthrough with attestation (PPCIe with NVSwitch; **NVSwitch required** for validated 8-GPU TDX)
- **Admission control** - OPA-based policy enforcement preventing privilege escalation
- **Attestation services** - TDX quote generation and GPU evidence verification
- **Dynamic configuration** - Node identity and network config loaded from verified volumes at boot

## Architecture

### Attestation-Based Boot

1. VM boots with encrypted root filesystem
2. TDX quote generated with nonce from attestation API
3. Quote verified by remote service, LUKS key returned
4. Configuration volume validated and applied
5. Services start with hardware-attested identity

### Security Layers

- **Hardware isolation** - Intel TDX enforces memory encryption and isolation
- **Admission controller** - Validates all Kubernetes workloads before scheduling
- **Seccomp profiles** - System call filtering to prevent container escapes
- **Network policies** - Restrict pod-to-pod and pod-to-service communication
- **Certificate-based authentication** - mTLS for attestation service access

## Quick Start

### Prerequisites

```bash
# Ubuntu 22.04/24.04 host with KVM support
sudo apt install ansible libvirt-daemon-system qemu-utils
ansible-galaxy collection install community.general kubernetes.core ansible.posix
```

### Environment Setup

```bash
# Required: LUKS encryption passphrase
export LUKS_PASSPHRASE="your-secure-passphrase"

# Optional: Attestation endpoints
export ATTESTATION_ENDPOINT="https://api.example.com/attestation"
export TDX_NONCE_ENDPOINT="https://api.example.com/nonce"
```

### Build Image

```bash
cd ansible/guest
ansible-playbook playbooks/chutes-miner-vm.yml
```

The build process:
1. Launches temporary VM from base Ubuntu cloud image
2. Installs and configures k3s, GPU drivers, attestation services
3. Applies security hardening and admission policies
4. Encrypts root filesystem with LUKS
5. Configures initramfs for TDX-based boot unlock
6. Outputs the final encrypted image SET under `guest-tools/image/<build_env>/<vm_version>/` — the `<vm_version>.qcow2`, its direct-boot `.vmlinuz`/`.initrd`/`.cmdline` sidecars, and `manifest.json` (see `playbooks/group_vars/host.yml` and inventory `build_env`; `vm_version` comes from `ansible/guest/VERSION`; a debug build appends `-debug` to both the directory and the image name). The directory is a ready-to-use image set: copy it into `/var/lib/chutes/base-images/<variant>/` to boot it with `chutes-cvm guest launch`.

At the **start** of `chutes-miner-vm.yml` (before the build VM is launched), the playbook prints the build configuration and **pauses for confirmation** (press Enter to continue, Ctrl+C to abort).

## Deployment

The built image requires TDX-capable host infrastructure. See `host-tools/README.md` for complete deployment instructions including:

- TDX host setup and kernel configuration
- GPU binding and PPCIe mode configuration
- Network setup (bridge or macvtap modes - **NAT is supported**)
- Config and cache volume creation (automated by host tools)
- VM launch with GPU passthrough

### Network Modes

The VM supports both dedicated public IP and NAT configurations:

- **Bridge mode** - Private bridge network with NAT and port forwarding
- **Macvtap mode** - Direct attachment to physical interface

Host tools automatically configure iptables rules for k3s API (port 6443) and NodePort services (30000-32767).

### Configuration Volumes

Production VMs require three attached volumes (created by `chutes-cvm guest launch`):

#### Config Volume (`tdx-config`)
- **Created by**: `src/chutes-cvm/chutes_cvm/scripts/volumes/create-config.sh`
- **Filesystem**: ext4 with label `tdx-config`
- **Mount point**: `/var/config`
- **Contents**:
  - `hostname` - Node hostname
  - `miner-ss58` - Bittensor SS58 address
  - `miner-seed` - Bittensor secret seed
  - `network-config.yaml` - Netplan configuration
  - `docker-hub-username` - (optional) Docker Hub username for authenticated pulls
  - `docker-hub-token` - (optional) Docker Hub PAT for authenticated pulls and cosign

#### Cache Volume (`tdx-cache`)
- **Created by**: `src/chutes-cvm/chutes_cvm/scripts/volumes/create-cache.sh`
- **Filesystem**: XFS with label `tdx-cache`
- **Mount point**: `/var/snap`
- **Purpose**: Persistent storage for HF/model caches (e.g., `/var/snap/cache` for model weights)
- **Size**: Configurable (default 5000G)

#### Storage Volume (`storage`)
- **Created by**: `src/chutes-cvm/chutes_cvm/scripts/volumes/create-cache.sh` (with label `storage`)
- **Filesystem**: XFS with label `storage`
- **Mount point**: `/cache/storage` (contents bind-mounted into standard paths)
- **Purpose**: Persistent k3s state, containerd data, kubelet pods, admission controller certs, and chutes agent state
- **Bind mounts**: `/var/lib/rancher/k3s`, `/var/lib/kubelet`, `/etc/admission-controller/certs`, `/var/lib/chutes/agent`
- **Size**: Configurable (default 500G)

All three volumes are validated at boot. Missing or invalid volumes trigger immediate shutdown.

## Key Components

### Attestation Service (`/opt/sek8s`)
- Generates TDX quotes with report data
- Combines TDX attestation with NVIDIA GPU evidence
- Exposes internal (authenticated) and external (validator) endpoints
- Implements nonce-based replay protection

### Admission Controller
- Validates all pod specifications against OPA policies
- Enforces registry allowlists with cosign signature verification
- Prevents privileged containers and dangerous capabilities
- Blocks namespace manipulation and webhook modifications

### K3s Initialization
- `k3s-pre-start` - Generates node configuration with TLS SANs
- `k3s-post-start` - Runs post-boot setup scripts:
  - Node cleanup and labeling
  - Certificate generation for remote access
  - Miner credential injection

## Customization

Key configuration in `playbooks/group_vars/all.yml`:

- `k3s_version` - Kubernetes version
- `cuda_version` / `nvidia_version` - GPU driver versions  
- `validator` - Allowed validator SS58 address

See role-specific defaults for component configuration.

## Repository Structure

```
├── ansible/guest/          # VM image build automation (this README)
├── host-tools/           # TDX host setup and VM deployment
│   ├── scripts/          # GPU binding, network setup, VM launch
│   └── docs/             # Cache volume and deployment guides
└── guest-tools/          # VM testing and validation tools
```

## Components Built by This Automation

This Ansible playbook builds the VM image only. The following are handled by host-tools:

- ❌ TDX-enabled host system setup → See `src/chutes-cvm/chutes_cvm/host/`
- ❌ GPU passthrough configuration → Handled automatically by `chutes-cvm guest launch`
- ❌ Network infrastructure → See `src/chutes-cvm/chutes_cvm/scripts/network/setup-bridge.sh`
- ❌ Config/cache/storage volume creation → See `src/chutes-cvm/chutes_cvm/scripts/volumes/create-*.sh`
- ❌ VM launch and orchestration → Handled by `chutes-cvm guest launch`
- ✅ Guest OS and k3s installation
- ✅ GPU drivers and attestation services
- ✅ Security hardening and admission control
- ✅ LUKS encryption and boot configuration

## License

See repository root for licensing information.