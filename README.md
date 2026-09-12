# sek8s

Confidential GPU infrastructure for Chutes miners and zero-trust workloads. This monorepo bundles everything you need to build, attest, launch, and operate Intel TDX VMs with NVIDIA GPUs—including the host orchestration scripts, the guest image builder, and ready-to-run documentation.

---

## What's in this repo?


| Directory               | Purpose                                                                                                                 |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `**guest-tools/**`      | Build the encrypted TDX VM image with k3s, attestation services, and GPU drivers                                        |
| `**host-tools/**`       | Set up the host machine and launch the TDX VM (GPU binding, networking, volume management)                              |
| `**docs/**`             | Integration guide with [chutes-miner](https://github.com/chutesai/chutes-miner) and system-status service documentation |
| `ansible/guest/`          | Ansible roles for guest image build automation                                                                          |
| `sek8s/`, `nvevidence/` | Python services running inside the guest (attestation, evidence verification, system status)                            |
| `guest-tools/`          | Guest measurement & verification tooling (`measurement/`), image build output (`image/`), R2 publish (`publish-image.sh`)  |


---

## Quick start roadmap

1. **Set up the host** — Use `[host-tools/](host-tools/)` to prepare your TDX-capable machine with the required kernel, PCCS, and networking.
2. **Download the VM image** — Run `chutes-cvm image download` to fetch + verify the prebuilt guest image set (requires `aria2`).
3. **Configure and launch** — Run `chutes-cvm config init` to generate a `config.yaml`, fill in your miner credentials and network settings, then `chutes-cvm guest launch config.yaml` to create volumes, configure GPUs, and boot the VM in one command.
4. **Understand the integration** — Read `[docs/end-to-end-miner.md](docs/end-to-end-miner.md)` to see how this repo integrates with the [chutes-miner](https://github.com/chutesai/chutes-miner) control plane.
5. **Build the guest image** (optional) — Use `[guest-tools/](guest-tools/)` and `[ansible/guest/](ansible/guest/)` to customize or rebuild the encrypted VM image.
6. **Monitor VM status** — See `[docs/system-status.md](docs/system-status.md)` for using the system-status API to inspect service health and GPU telemetry inside the VM.

> **Important:** The guest root disk is LUKS-encrypted. Only the Chutes attestation/key service (or your own compatible service) can decrypt it after verifying Intel TDX measurements, so simply possessing the qcow2 image is not enough to run the VM.

The `config.yaml` defines your deployment: VM identity, miner credentials, network settings, optional Docker Hub authentication, and separate cache and storage volumes. See `[host-tools/scripts/config/CONFIG-GUIDE.md](host-tools/scripts/config/CONFIG-GUIDE.md)` for the full schema reference.

### How this repo pairs with `chutes-miner`

- **Guest image:** Built with `guest-tools/` and `ansible/guest/`, contains the full Chutes stack pre-installed.
- **Host operations:** Use `host-tools/` to launch and manage the TDX VM on bare metal.
- **Control plane:** The [chutes-miner](https://github.com/chutesai/chutes-miner) repo manages your fleet of miners (both TEE and non-TEE) via `chutes-miner-cli`.
- **Integration:** See `[docs/end-to-end-miner.md](docs/end-to-end-miner.md)` for how the pieces fit together.

> **Note:** TEE VMs have no SSH access. Use the `chutes-miner-cli` for management and the system-status API (see `[docs/system-status.md](docs/system-status.md)`) for read-only monitoring.

---

## Key Documentation

- `**[host-tools/README.md](host-tools/README.md)`** — Setting up the TDX host and launching VMs
- `**[docs/specs/tdx-measurement-verification.md](docs/specs/tdx-measurement-verification.md)**` — How the guest image's TDX measurements are structured, reproduced, and independently verified (on-host capture in `guest-tools/measurement/`; offline replay/generation in `chutes_cvm.measurement`)
- `**[docs/end-to-end-miner.md](docs/end-to-end-miner.md)**` — Complete integration workflow with chutes-miner
- `**[docs/system-status.md](docs/system-status.md)**` — System status API for monitoring service health and GPU telemetry

---

## Questions / contributions

- File an issue or PR in this repo for host tooling, image builds, or docs
- Use the [chutes-miner](https://github.com/chutesai/chutes-miner) repo for chart-specific issues
