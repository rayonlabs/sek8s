# Feature Spec: Ansible host operational playbooks

**Date**: 2026-04-10  
**Status**: implemented (see [`ansible/host/`](../../ansible/host/))

---

## Context

Operational Ansible for **bare-metal TDX hosts** that run the sek8s VM stack (`host-tools/scripts`: `chutes-cvm host setup`, `quick-launch.sh`, etc.). This is **separate** from the **guest VM image build** Ansible under [`ansible/guest/`](../../ansible/guest/) (formerly `ansible/k3s/`).

Primary references:

- [host-tools/README.md](../../host-tools/README.md) — host prep, PCCS, image paths, launch behavior
- [host-tools/scripts/config/CONFIG-GUIDE.md](../../host-tools/scripts/config/CONFIG-GUIDE.md) — `config.yaml` schema
- [docs/end-to-end-miner.md](../end-to-end-miner.md) — TEE guest has **no SSH**; control via **chutes-miner CLI** / APIs
- [ansible/host/README.md](../../ansible/host/README.md) — operator quick start

**Packages affected**: No external Ansible collections required; all modules are `ansible.builtin`. Playbooks under **`ansible/host/`** are **not** part of the guest image release line; only **`ansible/guest/*`** (and other VM-domain paths) bump [`ansible/guest/VERSION`](../../ansible/guest/VERSION) per [docs/versioning.md](../versioning.md).

**Key files**: `ansible/host/playbooks/{setup,launch,shutdown,upgrade}.yml`, `ansible/host/inventory/`, `ansible/host/group_vars/`, `ansible/host/roles/` (including **`chutes_vm_config`**, **`chutes_tee_vm`** for live-QEMU pre-check and shared shutdown tasks); [`host-tools/scripts/quick-launch.sh`](../../host-tools/scripts/quick-launch.sh) (includes duplicate-QEMU guard and `--force`); [`src/chutes-cvm/chutes_cvm/scripts/devices/reset-gpus.sh`](../../src/chutes-cvm/chutes_cvm/scripts/devices/reset-gpus.sh).

**Dependencies**

- **Operator machine:** Ansible, `chutes-miner`, `kubectl` (upgrade only), `rsync`
- **Bare metal:** Ubuntu 25.10 or 26.04 per host profile, `aria2`, Python + PyYAML, PCCS stack (after `chutes-cvm host setup`)

### External tooling contract (v1)

- **No new sek8s HTTP services** — orchestration is Ansible + shell + upstream CLIs only.
- **`chutes-miner` CLI** (operator machine), verified subcommands:
  - **Maintenance:** `chutes-miner tee start-maintenance --name <server> --yes --hotkey <path> --validator-api <url> [--raw-json]`
  - **Status:** `chutes-miner tee maintenance-status --hotkey <path> [--validator-api <url>]`
  - **Shutdown:** `chutes-miner tee shutdown --name <server> --confirm --hotkey <path> [--miner-api <url>]` (or `--ip` instead of `--name`)
  - **Kubeconfig:** `chutes-miner sync-kubeconfig --hotkey <path> --path <kubeconfig> [--miner-api <url>]`
  - **Health:** `chutes-miner tee node-health --name <server> --hotkey <path> [--miner-api <url>]`
  - **Image pull/list/etc.:** `chutes-miner tee image-*` — **not** used for base qcow2 staging; staging uses `aria2c` on the host (see Upgrade).
  - **Unlock:** `chutes-miner unlock --name <server> --hotkey <path> [--miner-api <url>]` — resumes gepetto scheduling after maintenance; must be called explicitly after node-health passes (validator does **not** auto-clear the lock).
- **`kubectl`** — uses kubeconfig from `sync-kubeconfig`; upgrade drain deletes pods in namespace **`chutes`** with label **`chutes/chute=true`** (`upgrade_chute_label` in `group_vars`).
- **Alternative rejected by default:** installing **`chutes-miner` on bare metal**.

---

## Design Decisions

1. **Repository layout**  
   - **`ansible/guest/`** — guest **image build** only.  
   - **`ansible/host/`** — **setup**, **launch**, **upgrade** playbooks.

2. **Controller and inventory**  
   - Ansible on the **operator laptop**, **SSH** to bare-metal hosts, **central inventory**.

3. **`chutes-miner` on localhost**  
   - **`delegate_to: localhost`** for CLI and `kubectl` so credentials stay on the controller.

4. **TEE guest is not an Ansible host**  
   - Never SSH to the VM.

5. **PCCS automation**
   - Interactive **`pccs-configure`** has no useful non-interactive flags on target Ubuntu; automation **templates** `/opt/intel/sgx-dcap-pccs/config/default.json`, generates TLS key/cert under `pccs_ssl_dir`, restarts **`pccs`**, runs **`PCKIDRetrievalTool`** with the Vault password. **Intel `ApiKey` is stored plaintext in that JSON on the host** (PCCS requirement); protect with Vault on the controller and filesystem permissions on metal. On failure, operator follows [host-tools/README.md](../../host-tools/README.md) Step 2 manually. **`setup.yml`** always includes **`pccs_configure`**: if **`pccs_api_key`** and **`pccs_password`** are **both** set, the role runs; if **neither** is set, the role prints why it skipped and exits the role; **only one** set fails the play with an inventory remediation message.

6. **Launch vs upgrade (image-set coherence)**  
   - The base image is a published **image set** — a per-variant directory holding the qcow2, its direct-boot `.vmlinuz`/`.initrd`/`.cmdline`, and a `manifest.json` (sha256 + size per artifact). Coherence is verified against the manifest by `chutes_cvm.guest.image_set` (full hash at download, presence/size at launch); there is no hand-maintained `EXPECTED_BASE_SHA256`.  
   - **Launch:** `quick-launch.sh --download` **only** when the default base-image **directory** is **missing**. If it **exists** and manifest verification fails → **fail** and direct to **`upgrade-guest.yml`** (no auto-download overwrite).  
   - **Upgrade:** Stage the full set with **`aria2c`** into **`tdx-guest.staged/`** and verify it with **`image_set verify --full`**; then after shutdown **rename** current `tdx-guest/` → `tdx-guest-<YYYY-MM-DD>/`, **rename** staged → `tdx-guest/`, **relaunch** with the default directory (no `--base-image` override).

7. **Host content on metal**  
   - **rsync** `host-tools/` from the controller checkout to **`sek8s_remote_host_tools`** (default `/opt/sek8s/host-tools`), not full-repo clone.

8. **`ansible/host/` versioning**  
   - **Unpinned** — no separate `VERSION` file; VM image line remains **`ansible/guest/VERSION`**.

9. **Stuck pods (`Terminating`)**  
   - **`upgrade_force_delete_stuck_pods`** (default `false`) — when `true`, a force-delete is attempted after a failed `kubectl delete … --wait`.

10. **Idempotency**  
    - All three playbooks intended **safe to re-run**; **`quick-launch.sh`** refuses a second live **`chutes-td`** QEMU unless **`--force`**.

11. **Per-host `config.yaml`**  
    - **`launch.yml`** and **`upgrade-guest.yml`** install **`config.yaml`** by templating on the host (**`chutes_vm_config`**): miner credentials from Ansible vars, **`vm.hostname`** from inventory (with override), **`network.public_interface`** from the default route (with override), and **`vm_ip` / `bridge_ip`** on a **`/24`** that does not overlap existing host IPv4 assignments (deterministic scan with manual CIDR escape hatch).

---

## API Changes

- **New endpoints**: None (v1).

---

## Goal

Operators **provision**, **launch**, and **upgrade** TDX hosts from one inventory; playbooks **fail closed** with actionable messages (**launch → upgrade** handoff on checksum failure).

### 1. Host setup (`setup.yml`)

- Rsync **`host-tools/`**, **`aria2`** + **`python3-yaml`**, **`chutes-cvm host setup`**, **reboot** if `/var/run/reboot-required`, **TDX dmesg** check, **`/var/lib/chutes/*` dirs**, **`pccs_configure`** (automated PCCS when **both** **`pccs_api_key`** and **`pccs_password`** are set; otherwise a notice and no-op; partial config fails).  
- **Does not** launch the VM.

### 2. Launch (`launch.yml`)

- **Pre-check:** if a **live chutes-td QEMU** process is already on the host (same detection as **`quick-launch.sh`**), the play **fails** with remediation (**`playbooks/shutdown.yml`** or **`chutes-miner tee shutdown`**), so operators are not surprised by **`quick-launch`** refusing a duplicate instance.  
- Requires **`chutes_miner_ss58`** and **`chutes_miner_seed`** (host_vars / Vault) so **`chutes_vm_config`** can render **`config.yaml`** on the host.  
- Rsync, prerequisites, download **only if missing**, **`include_role: chutes_vm_config`** (sets **`vm.hostname`** to **`inventory_hostname`** unless **`chutes_vm_hostname`**; picks **primary NIC** and a **guest `/24`** that does not overlap host IPv4, unless **`chutes_guest_bridge_network`** / **`chutes_public_interface`** override), then **`quick-launch.sh`**.  
- Duplicate QEMU is still blocked in **`quick-launch.sh`** as a second line of defense (use **`--clean`** or **`--force`** only if intentional).

### 2b. Shutdown (`shutdown.yml`)

- **`chutes-miner tee shutdown`** from **`delegate_to: localhost`**, then poll **`/tmp/tdx-guest-td.log`** for **`Power down`** on the metal host (shared **`chutes_tee_vm`** role tasks used by **`upgrade-guest.yml`**).

### 3. Guest upgrade (`upgrade-guest.yml`)

- Requires **`chutes_hotkey_path`**. **`chutes-miner --name`** defaults to the **inventory hostname**; set **`tee_server_name`** only when that differs from the registered TEE server name. Before relaunch, **`chutes_vm_config`** re-renders **`config.yaml`** with the same variable contract as **`launch.yml`**.  
- Rsync, stage + verify, **`start-maintenance`**, **`sync-kubeconfig`**, **`kubectl delete`** chute pods, **`tee shutdown`**, wait **`Power down`** in **`/tmp/tdx-guest-td.log`**, **rename** cutover, **`quick-launch.sh`**, **`tee node-health`** poll.

---

## Constraints

- [AGENT.md](../../AGENT.md): no undeclared dependencies; secrets via Vault / vars.
- Do not conflate **`ansible/host/`** with **`ansible/guest/`** image build.
- Timeouts: **`upgrade_drain_timeout`**, **`upgrade_powerdown_timeout_seconds`**, **`upgrade_health_poll_seconds`** in `group_vars`.

---

## Output Format

### Directory layout

```
ansible/host/
  README.md
  ansible.cfg
  requirements.yml
  playbooks/setup.yml | launch.yml | shutdown.yml | upgrade-guest.yml | upgrade-host.yml
  inventory/hosts.yml
  group_vars/all.yml
  roles/chutes_vm_config/…
  roles/…
```

### Operator handoff (launch → upgrade)

On checksum / verification failure, **`launch.yml`** fails with instructions to run:

`ansible-playbook -i … ansible/host/playbooks/upgrade-guest.yml`

or fix/remove the qcow2 manually.

### Upgrade — ordered phases (implemented)

1. Rsync **host-tools** (syncs the launcher + `chutes_cvm.guest.image_set` verifier).  
2. **Stage** the image set with **`aria2c`** into **`/var/lib/chutes/base-images/tdx-guest.staged/`**; verify with **`image_set verify --full`** against the manifest.  
3. **`chutes-miner tee start-maintenance`**.  
4. **`chutes-miner sync-kubeconfig`**.  
5. **`kubectl delete pods -n chutes -l chutes/chute=true --wait=true`** (optional force path).  
6. **`chutes-miner tee shutdown --confirm`**.  
7. Poll **`/tmp/tdx-guest-td.log`** for **`Power down`** (same substring as [`ansible/guest/roles/prime-vm/tasks/main.yml`](../../ansible/guest/roles/prime-vm/tasks/main.yml)).  
8. **`mv`** current **`tdx-guest/`** → dated backup directory; **`mv`** staged **`tdx-guest.staged/`** → **`tdx-guest/`**.  
9. **`quick-launch.sh`** with the default base directory.  
10. **`chutes-miner tee node-health`** poll.  
11. **`chutes-miner unlock`** — resumes gepetto scheduling (validator does not auto-clear the maintenance lock).

---

## Failure Conditions

- **Anti-idempotency:** Second run **corrupts** images, starts **two** QEMU instances, or wedges maintenance without recovery docs.  
- **PCCS:** Template/service/PCK tool failure → fail with pointer to README.  
- **Launch:** Missing image after download attempt; checksum failure on **existing** file with auto-download “fix”.  
- **Upgrade:** Bad staged checksum; drain timeout; no **Power down** in log; relaunch failure; health never OK.

---

## Rollout Notes

- **`ansible/k3s/` → `ansible/guest/`** rename shipped with **`ansible/host/`**; **`ansible/guest/VERSION`** is the VM image tag source.  
- **`ansible/host/`** is **not** separately versioned.

---

## Collections

Install before first run:

```bash
cd ansible/host && ansible-galaxy collection install -r requirements.yml
```
