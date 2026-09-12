# Feature Spec: RTX Pro 6000 TEE VM Support

**Date**: 2026-03-29  
**Status**: draft

---

## Context

Add NVIDIA RTX Pro 6000 Blackwell (GB202) GPU support to the TEE VM launch
pipeline. The RTX Pro 6000 is a PCIe Gen 5 workstation/server GPU with 96 GB
GDDR7 and NVIDIA Confidential Computing support. Unlike H200/B200, it has **no
NVSwitch and no NVLink** -- inter-GPU communication is PCIe-only.

- **Packages affected**: `src/chutes-cvm/chutes_cvm/guest`
- **Key files**:
  - `src/chutes-cvm/chutes_cvm/guest/gpu/profiles.py` (new profile)
  - `src/chutes-cvm/chutes_cvm/guest/gpu/tools.py` (nvidia-gpu-tools compat)
  - `src/chutes-cvm/chutes_cvm/guest/detection.py` (detection driven by profile)
  - `src/chutes-cvm/chutes_cvm/guest/passthrough.py` (orchestration driven by profile)
  - `ansible/guest/roles/gpu/files/nvidia-fabricmanager-mask.sh` (no changes, already handles no-NVSwitch)
  - `ansible/guest/roles/gpu/files/nvidia-persistenced-config.sh` (no changes, already handles no-NVSwitch)
- **Dependencies**: `nvidia-gpu-tools` (bundled wheel from NVIDIA/gpu-admin-tools) must support GB202 CC mode

---

## Design Decisions

- **CC mode only, no PPCIe**: RTX Pro 6000 has no NVSwitch fabric, so PPCIe mode is not applicable. Use `--set-cc-mode=on` (same as B200).
- **No NVSwitch passthrough**: `should_passthrough_nvswitches()` returns `False` unconditionally regardless of GPU count.
- **No InfiniBand passthrough**: RTX Pro 6000 does not use external IB HCAs. Multi-GPU communication within a single node uses PCIe P2P, not network.
- **Profile-driven architecture**: All behavior differences are encoded in `GpuProfile` subclass; no changes needed to passthrough orchestration, VFIO binding, or QEMU topology code.
- **Guest image unchanged**: Existing fabricmanager-mask and persistenced-config services already detect the absence of NVSwitch via `lspci` and behave correctly.

---

## API Changes

- **New endpoints**: None
- **Schema changes**: None
- **Migrations**: None

---

## Goal

Success = TDX VM launches with RTX Pro 6000 GPU(s) passed through in CC mode, with:

1. `nvidia-gpu-tools --query-cc-mode` detects the GPU and reports CC status
2. `nvidia-gpu-tools --set-cc-mode=on` succeeds before VFIO bind
3. GPU(s) bind to `vfio-pci` and appear in QEMU PCI topology
4. Guest VM boots, `nvidia-smi` shows GPU(s), `nvidia-fabricmanager` is masked
5. `nvidia-persistenced` runs in standard `--persistence-mode` (not UVM)
6. Unit tests pass for new profile (resolution, CC args, NVSwitch/IB flags, RAM sizing)

---

## Constraints

- PCI device ID `2bb1` is the Workstation Edition, `2bb5` is the Server Edition (confirmed on hardware). Both are in `pci_device_ids`.
- BAR size (`bar_size_mb`) is **131072 MB (128 GiB)**, validated on Server Edition hardware: `lspci -vvv -d 10de:` reports **Physical Resizable BAR / BAR 2: current size: 128GB** on each GPU. (Optional cross-check: `nvidia-smi -q -d BAR1` in a VM with driver.)
- Do not modify passthrough orchestration (`passthrough.py`, `detection.py`, `vfio.py`) -- all behavior must be driven by the profile.
- Single guest image for all GPU topologies -- no topology-specific Ansible changes.
- `nvidia-gpu-tools` bundled wheel must support Blackwell GB202. If not, re-bundle from latest `gpu-admin-tools` main via `make bundle-gpu-tools` (`src/chutes-cvm/tools/gpu-tools/bundle-tools.sh`).

---

## Output Format

1. New `RTXPro6000Profile` class in `src/chutes-cvm/chutes_cvm/guest/gpu/profiles.py`
2. New entry in `GPU_PROFILES` dict keyed as `'RTX_PRO_6000'`
3. Unit tests in `tests/` covering profile resolution, CC mode args, NVSwitch=False, IB=False, mixed-model rejection, and RAM sizing (`N * 96G`)

---

## Failure Conditions

- Profile is not detected from `lspci` output containing `[10de:2bb1]` or `[10de:2bb5]`
- CC mode args include PPCIe flags or NVSwitch configuration
- `should_passthrough_nvswitches()` ever returns `True`
- Guest image requires topology-specific changes to support RTX Pro 6000
- Passthrough orchestration code (`passthrough.py`, `vfio.py`, `qemu.py`) is modified

---

## Rollout Notes

- **Host-level ACS**: Multi-GPU PCIe P2P requires ACS disabled. Hosts need kernel boot parameter `pcie_acs_override=downstream,multifunction` or BIOS-level ACS disable. Without this, NCCL AllReduce hangs (ref: NVIDIA/nccl#1999).
- **NCCL tuning**: PCIe-only nodes should set `NCCL_IB_DISABLE=1`. `NCCL_P2P_DISABLE` should remain `0` (enabled) once ACS is handled. These env vars are already allowed by OPA admission policy.
- **Bounce buffers**: In CC mode without NVSwitch, GPU-to-GPU data goes through encrypted bounce buffers via CPU TEE. This is inherent to CC-over-PCIe and acceptable for inference/fine-tuning but limits tensor-parallel training throughput.
- **Driver**: Current pinned 580.x open-source kernel module supports Blackwell. No Ansible var changes needed.
- **Backward compatible**: Existing H200/B200 nodes are unaffected. Profile resolution matches on PCI device ID; unknown IDs are rejected, not silently mismatched.

---

## Open Items (Hardware Validation Required)

These must be resolved on actual hardware before production deployment. Use
placeholder values from this spec for the initial implementation, then update
once validated.

| Item | Current Value | How to Validate |
|------|---------------|-----------------|
| Server Edition PCI device ID | `2bb5` (confirmed via `lspci` on server hardware) | **RESOLVED** |
| BAR size (MMIO) | 131072 MB (128 GiB) | **RESOLVED** — `lspci -vvv -d 10de:`: Physical Resizable BAR, BAR 2 current size 128GB on Server Edition (2bb5) |
| `nvidia-gpu-tools` / GB202 | Bundled wheel **v2025.11.21** | **RESOLVED** — `--query-cc-mode` and **`--set-cc-mode=on --reset-after-cc-mode-switch --gpu-bdf=…`** verified on Server Edition (`0x2bb5`); query shows **CC mode is on** after reset. Full launch applies the same args per GPU via `passthrough.py` |
| Host ACS configuration | GPU endpoints: ARI shows `ACS-` on each `[10de:2bb5]` function in sample `lspci -vvv` | If NCCL P2P still misbehaves, inspect **parent PCIe bridges / root ports** (ACS often lives upstream, not on the GPU); use `pcie_acs_override=...` or BIOS if needed |

