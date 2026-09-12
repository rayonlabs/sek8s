# Ansible: bare-metal TDX host operations

Operational playbooks run from your workstation against inventory over SSH. Guest **image build** lives separately under [`../guest/`](../guest/).

## Prerequisites (controller)

- Ansible 2.17+ recommended
- `chutes-miner` and `kubectl` on the controller for `upgrade-guest.yml` and `upgrade-host.yml` (when `auto_drain_vm=true`)

## Quick start

```bash
cd ansible/host
ansible-playbook -i ~/chutes/my-inventory.yml playbooks/setup.yml
```

## Variables by playbook

### `setup.yml` — host prep, TDX kernel, PCCS

| Variable | Scope | Required | Notes |
|---|---|---|---|
| `ansible_host` | host | **yes** | bare-metal IP or hostname |
| `ansible_user` | host | **yes** | SSH user (typically `ubuntu`) |
| `ansible_ssh_private_key_file` | host | if not default | path to SSH private key |
| `pccs_api_key` | group / Vault | **both or neither** | Intel PCCS API key; omit both to skip PCCS automation |
| `pccs_password` | group / Vault | **both or neither** | generates `UserTokenHash` / `AdminTokenHash` (SHA-512) |
| `sek8s_remote_root` | group | no | remote install root; default `/opt/sek8s` |

`setup.yml` does **not** launch the VM and does **not** need miner credentials or hotkey.

---

### `launch.yml` — render config, download image, start VM

Before launching, `launch.yml` verifies that the host clock is NTP-synchronized (chrony offset < 5 s). VMs inherit the host RTC at QEMU boot time — a skewed clock causes boot-time mTLS certificates generated inside the guest to carry wrong `notBefore` timestamps, which the attestation endpoint rejects. If this check fails, run `setup.yml` first to install and sync chrony.

Requires everything in `setup.yml` (SSH) plus:

| Variable | Scope | Required | Notes |
|---|---|---|---|
| `chutes_hotkey_path` | group / Vault | **yes** | controller path to Bittensor hotkey JSON; `ss58Address` and `secretSeed` are extracted automatically |
| `chutes_miner_ss58` | host / Vault | override only | skip if `chutes_hotkey_path` is set |
| `chutes_miner_seed` | host / Vault | override only | skip if `chutes_hotkey_path` is set |
| `chutes_guest_bridge_network` | host | no | e.g. `192.168.50.0/24`; auto-picked when unset |
| `chutes_public_interface` | host | no | NIC for bridge; default is default-route interface |
| `chutes_docker_hub_username` / `chutes_docker_hub_token` | group / Vault | no | optional Docker Hub block in config |

The inventory hostname is used as both `vm.hostname` in `config.yaml` and `chutes-miner --name`. These must match — do not use an SSH alias as the inventory key.

---

### `shutdown.yml` — graceful TEE guest shutdown

| Variable | Scope | Required | Notes |
|---|---|---|---|
| `chutes_hotkey_path` | group / Vault | **yes** | controller path to miner hotkey file |
| `chutes_miner_api` | group | no | miner API URL; default `http://127.0.0.1:32000` |

---

### `upgrade-guest.yml` — stage new image, drain, cutover, relaunch

Requires everything in `launch.yml` plus:

| Variable | Scope | Required | Notes |
|---|---|---|---|
| `chutes_hotkey_path` | group / Vault | **yes** | controller path to miner hotkey file |
| `chutes_miner_api` | group | no | miner API URL; default `http://127.0.0.1:32000` |
| `chutes_kubeconfig_path` | group | no | kubeconfig written by `sync-kubeconfig`; default `~/.kube/chutes.config` |
| `upgrade_force_delete_stuck_pods` | group / `-e` | no | force-delete `Terminating` pods; default `false` |
| `chutes_validator_api` | group | no | validator API; default `https://api.chutes.ai` |

Upgrade timeouts (all in `group_vars/all.yml`, override as needed):

| Variable | Default |
|---|---|
| `upgrade_drain_timeout` | `600s` |
| `upgrade_powerdown_timeout_seconds` | `300` |
| `upgrade_health_poll_seconds` | `900` |

---

### `upgrade-host.yml` — upgrade the host OS (do-release-upgrade)

Advances the host OS by one or more versions following the `os_upgrade_path` defined in `group_vars/all.yml`. After the upgrade, re-run `setup.yml` to apply the matching host profile.

```bash
# Single hop (e.g. 25.10 -> 26.04):
ansible-playbook -i ~/chutes/my-inventory.yml playbooks/upgrade-host.yml

# Multi-hop to a target version (e.g. 25.04 -> 25.10 -> 26.04 in one run; 25.10 has
# no setup profile, so always target 26.04 from 25.04):
ansible-playbook -i ~/chutes/my-inventory.yml playbooks/upgrade-host.yml \
  -e target_version=26.04

# Auto-drain running VM before upgrading (requires miner credentials):
ansible-playbook -i ~/chutes/my-inventory.yml playbooks/upgrade-host.yml \
  -e auto_drain_vm=true
```

| Variable | Scope | Default | Notes |
|---|---|---|---|
| `auto_drain_vm` | `-e` | `false` | When `true`, enters maintenance mode, drains pods, and shuts down the VM before upgrading. When `false`, fails if a VM is detected running. |
| `target_version` | `-e` | unset | Target Ubuntu version (e.g. `26.04`). When unset, performs a single hop. |
| `chutes_hotkey_path` | group / Vault | — | Required when `auto_drain_vm=true` |
| `chutes_kubeconfig_path` | group | — | Required when `auto_drain_vm=true` |
| `chutes_miner_api` | group | `http://127.0.0.1:32000` | Required when `auto_drain_vm=true` |

The upgrade path is defined in `group_vars/all.yml`:

```yaml
os_upgrade_path:
  "25.10": "26.04"
  "25.04": "25.10"  # 25.04 EOL; no setup.yml profile, but hop still works
```

25.10 is a waypoint only — it has no `setup.yml` profile either, so from 25.04 always
pass `-e target_version=26.04` rather than taking single hops. A run whose final hop is
25.10 is refused by the `chutes-cvm host verify` pre-flight (that OS ships no baselined QEMU), which
is what keeps a node from landing on an OS it cannot be provisioned on or launch from.

To add future upgrade hops (e.g. `26.04 -> 26.10`), add an entry to `os_upgrade_path`.

After each hop the playbook automatically runs `host_prerequisites` and `tdx_bootstrap` (the same roles `setup.yml` uses), leaving the host fully re-provisioned with the correct kernel, Intel DCAP attestation repo, and TDX verified. No manual `setup.yml` re-run is needed. PCCS config and volume directories survive the OS upgrade unchanged.

**Idempotency and failure recovery:**

The playbook is safe to re-run after most failures:

| Failure point | Re-run behaviour |
|---|---|
| Pre-hook or `dist-upgrade` | Hop retries from the start; all cleanup steps are idempotent |
| `do-release-upgrade` fails or is interrupted | Re-run retries `do-release-upgrade`; it is designed to resume partial upgrades |
| Reboot timeout or `tdx_bootstrap` fails after a successful OS upgrade | The host is already on the new OS version; re-running `upgrade-host.yml` will compute the **next** hop rather than re-provisioning the current one. Run `setup.yml` directly to complete provisioning without triggering another OS upgrade. |

---

## Secrets and inventory layout

Keep secrets out of git. Copy the example inventory outside the repo:

```bash
cp inventory/hosts.yml ~/chutes/my-inventory.yml
# edit ~/chutes/my-inventory.yml
cd /path/to/sek8s/ansible/host
ansible-playbook -i ~/chutes/my-inventory.yml playbooks/setup.yml
```

Use `ansible-vault` for `pccs_api_key`, `pccs_password`, and `chutes_docker_hub_token`. A complete inventory looks like:

```yaml
# ~/chutes/my-inventory.yml
all:
  children:
    td_hosts:
      hosts:
        my-tee-host:
          # Per-host: SSH connection and any host-specific network overrides
          ansible_host: 10.0.0.10
          ansible_user: ubuntu
          ansible_ssh_private_key_file: ~/.ssh/id_ed25519
          # chutes_guest_bridge_network: ""  # e.g. 192.168.50.0/24; auto-picked when unset
          # chutes_public_interface: ""      # NIC for bridge; default is default-route interface

      vars:
        # Group-wide: same for all hosts managed by this operator

        # Miner hotkey (launch / shutdown / upgrade)
        # ss58Address and secretSeed are extracted automatically.
        chutes_hotkey_path: ~/.bittensor/wallets/mywallet/hotkeys/myhotkey

        # PCCS (setup only) — set BOTH or omit BOTH
        # pccs_api_key: !vault ...
        # pccs_password: !vault ...

        # Optional group overrides
        # chutes_miner_api: ""             # miner API URL; default http://127.0.0.1:32000
        # chutes_docker_hub_username: ""
        # chutes_docker_hub_token: !vault ...
```

`ss58Address` and `secretSeed` are read from the hotkey JSON on the controller at play time — no need to copy them into inventory. To override either value, set `chutes_miner_ss58` / `chutes_miner_seed` explicitly and the hotkey parse is skipped.

The inventory hostname (`my-tee-host` above) is used as both `chutes-miner --name` and `vm.hostname` in `config.yaml`. These must match the TEE server name registered in chutes-miner — do not use an SSH alias as the inventory key.

The `config.yaml` shape is defined by the `LaunchConfig` model in [`config.py`](../../src/chutes-cvm/chutes_cvm/guest/config.py); `chutes-cvm config init` generates a starter file from it.

---

## OS support

| Ubuntu | Status | TDX kernel | Attestation |
|---|---|---|---|
| **26.04** | the only supported OS | pinned `linux-image-6.17.0-35-generic` | Intel DCAP repo (`resolute` suite) |
| 25.10 | **no longer supported — upgrade-only waypoint.** No `setup.yml` profile and no baselined QEMU, so a host left on 25.10 cannot be provisioned or launch a VM. Advance it with `upgrade-host.yml -e target_version=26.04`. | — | — |
| 25.04 | **EOL Jan 2026 — no profile.** Use `upgrade-host.yml -e target_version=26.04` (hops via 25.10). | — | — |

---

## How the playbooks and roles fit together

```
upgrade-host.yml                   upgrade-guest.yml
     │                                    │
     │  include_role: os_upgrade/hop      │  include_role: chutes_tee_vm/drain_and_shutdown
     │  (loop over _upgrade_hops)         │
     │       │                            │
     │       ├─ pre-upgrade hook          └─ include_role: chutes_tee_vm/shutdown_via_miner
     │       │  (roles/os_upgrade/tasks/pre_<ver>.yml — skipped if absent)
     │       └─ do-release-upgrade + reboot + assert
     │
     └─ include_role: chutes_tee_vm/assert_not_running  (when auto_drain_vm=false)
        include_role: chutes_tee_vm/drain_and_shutdown  (when auto_drain_vm=true)
```

### Shared roles

| Role | Task file | Used by |
|---|---|---|
| `ntp` | `main.yml` | `setup.yml` (first role — installs chrony, forces immediate clock step) |
| `chutes_tee_vm` | `assert_not_running.yml` | `launch.yml`, `upgrade-host.yml` |
| `chutes_tee_vm` | `drain_and_shutdown.yml` | `upgrade-guest.yml`, `upgrade-host.yml` |
| `chutes_tee_vm` | `shutdown_via_miner.yml` | `shutdown.yml`, `drain_and_shutdown.yml` |
| `os_upgrade` | `hop.yml` | `upgrade-host.yml` (looped per hop) |

### Adding a new OS upgrade hop

1. Add an entry to `os_upgrade_path` in `group_vars/all.yml`:
   ```yaml
   os_upgrade_path:
     "26.04": "26.10"   # new
   ```
2. Optionally add `roles/os_upgrade/tasks/pre_2604.yml` with any migration tasks to run before `do-release-upgrade` on that version (e.g. removing stale repos). Omit the file if no pre-upgrade work is needed.
3. Add a host profile in `src/chutes-cvm/chutes_cvm/host/profiles.py` for the new target version so `chutes-cvm host setup` (called automatically by the hop) can configure it correctly.

See [docs/specs/ansible-playbooks.md](../../docs/specs/ansible-playbooks.md) for the full contract.
