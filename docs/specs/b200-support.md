# Feature Spec: B200 Host Support

**Date**: 2026-05-28  
**Status**: done

---

## Context

- **Packages affected**: `src/chutes-cvm/chutes_cvm/host/`, `src/chutes-cvm/chutes_cvm/guest/`
- **Key files**:
  - `src/chutes-cvm/chutes_cvm/host/setup.py` — host setup orchestration
  - `src/chutes-cvm/chutes_cvm/host/support_matrix.py` — validated topologies
  - `src/chutes-cvm/chutes_cvm/guest/gpu/profiles.py` — B200 GPU profile
  - `src/chutes-cvm/chutes_cvm/guest/detection.py` — PCI device detection
  - `src/chutes-cvm/chutes_cvm/guest/passthrough.py` — passthrough orchestration
- **Dependencies**: `nvidia-fabricmanager`, `nvlsm`, `libibumad3`, `infiniband-diags` from CUDA apt repo

---

## Design Decisions

- **Fabric Manager runs on the host, not the guest.** B200 NVSwitches are not PCIe devices — they are managed by Fabric Manager through ConnectX-7 bridge PFs. In Blackwell MPT CC mode, NVLink traffic is hardware-encrypted, so host-side FM can manage routing without being able to snoop GPU data. This is the NVIDIA-recommended and architecturally secure configuration for B200 CC workloads.

- **FM setup lives in `chutes_cvm.host.setup`, not a new Ansible role.** The `chutes_cvm.host` Python package owns GPU-specific idempotent host configuration. Ansible handles generic host orchestration and calls `chutes-cvm host setup --noninteractive` which runs `setup_host()`. Adding a new step there keeps the domain boundary clean and requires no Ansible changes.

- **CX7 bridge PF detection uses VPD, not device ID.** Both bridge PFs (`SMDL=SW_MNG` in VPD) and NIC PFs share the same PCI device ID (`15b3:1021`). The VPD Vendor-specific field `SMDL=SW_MNG` is the only reliable way to distinguish them. This was confirmed on a reference B200 host: 4 bridge PFs at `0000:23:00.{0-3}` all carry the marker; 8 NIC PFs (one per GPU) do not.

- **Only GPUs are passed through; CX7 bridge PFs stay on host.** CX7 NIC PFs continue to produce SR-IOV VFs for guest InfiniBand networking as before. The `should_passthrough_infiniband = True` on `B200Profile` is correct; the new exclusion logic ensures bridge PFs are never included in the VF creation loop.

---

## API Changes

- **New endpoints**: none
- **Schema changes**: none
- **Migrations**: none

---

## Goal

Success =
- `setup_host()` on a B200 host installs FM/NVLSM, loads `ib_umad`, sets `PARTITION_RAIL_POLICY=1`, and enables `nvidia-fabricmanager.service`.
- `setup_host()` on an H200 or RTX Pro 6000 host skips Step 5c with "No B200 GPUs detected" log message.
- `setup_passthrough()` on a B200 host passes 8 GPU BDFs and 8 CX7 NIC VFs to the guest, while the 4 CX7 bridge PFs remain on the host.
- `setup_passthrough()` on an H200 host is unaffected (no CX7 bridge PFs detected, no exclusions applied).

---

## Constraints

- No new Python dependencies.
- `_setup_host_fabric_manager()` must be idempotent (safe to re-run).
- `detect_cx7_bridge_pfs()` must return an empty list and not raise on non-B200 hosts.
- `detect_infiniband_pfs()` must remain backward-compatible (`exclude_bdfs` defaults to `None`).

---

## Output Format

1. `src/chutes-cvm/chutes_cvm/host/setup.py` — `_detect_b200_gpus()`, `_setup_host_fabric_manager()`, Step 5c in `setup_host()`
2. `src/chutes-cvm/chutes_cvm/guest/detection.py` — `detect_cx7_bridge_pfs()`, updated `detect_infiniband_pfs(exclude_bdfs)`
3. `src/chutes-cvm/chutes_cvm/guest/passthrough.py` — CX7 bridge exclusion in `setup_passthrough()`
4. `src/chutes-cvm/chutes_cvm/guest/gpu/profiles.py` — corrected `B200Profile` values
5. `src/chutes-cvm/chutes_cvm/host/support_matrix.py` — `("25.10", "B200", 8)` added

---

## Failure Conditions

- CX7 bridge PFs are bound to vfio-pci and passed into the guest VM (FM loses access to NVSwitches, fabric initialization fails).
- `nvidia-fabricmanager` is not installed/started on a B200 host (NVLink fabric never initializes, multi-GPU workloads fail).
- FM setup runs on H200/RTX hosts and installs unnecessary packages or breaks the fabricmanager guest-masking flow.
- `detect_infiniband_pfs()` existing callers break due to signature change (prevented by `exclude_bdfs=None` default).

---

## Rollout Notes

- Guest image is unchanged — `nvidia-dkms-open` is already installed, `nvidia-fabricmanager-mask.sh` correctly masks FM in the guest on B200 (no NVSwitch PCIe devices detected via lspci), and `infiniband-config.sh` correctly enables IB services when CX7 NIC VFs are present.
- No `ansible/guest/VERSION` bump needed (no guest image changes).
- Host must have the CUDA apt repo configured before `setup_host()` runs Step 5c (the repo is added in Step 1 via `HostProfile.repos` for Ubuntu 25.10+ profiles, and is also installed by the GPU tools setup).
- `PARTITION_RAIL_POLICY=1` is the numeric value for `symmetric` in `fabricmanager.cfg`.
