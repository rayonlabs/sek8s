# Feature Spec: VM Attestation CA + Registry mTLS

**Date**: 2026-05-31 (re-derived onto release/next: 2026-07)
**Status**: implemented — CA registration mechanism **superseded** by
[`root-ca.md`](root-ca.md)

> **Superseded (2026-07):** the per-VM CA is no longer registered via a dedicated
> `PUT /servers/{vm_name}/vm-root-ca` call. The CA is now generated up front in
> `fetch_key_and_unlock` (init-premount), presented as the mTLS client cert on every boot call,
> and recorded by the validator from the RTMR3-attested `POST /servers/{vm}/provision` call
> (which replaced `/luks/attest`). `setup_vm_tls` no longer generates or registers the CA — it only
> signs the leaf certs from the existing CA. See [`root-ca.md`](root-ca.md). References below to
> `PUT /vm-root-ca`, `setup_vm_ca()`, and "generate the CA in `setup_vm_tls`" are historical.

---

## Context

The legacy registry pull path (nginx DaemonSet, NodePort 30500, hostname
`localregistry.chutes.ai:30500`) uses the miner's SS58 hotkey credentials to pull
images. Any registered miner can use their own proxy to pull private chute images
— the auth is miner-scoped, not VM-scoped. This spec replaces that with per-VM
mTLS so only attested VMs can pull images. A fresh CA keypair is generated in
initramfs on every boot, registered with the validator via a TDX-attested API
call, and used to sign both the attestation proxy's server cert and a short-lived
registry client cert. The key lives in TDX-encrypted DRAM only (tmpfs) and is
rotated at every reboot, minimising blast radius if a key is ever compromised.

- **Packages affected**: `src/attestation-proxy/`, `src/sek8s/`
- **Key files**:
  - `ansible/guest/roles/vm-tls/files/initramfs/setup_vm_tls` (new — owns the
    entire mTLS cert lifecycle)
  - `ansible/guest/roles/vm-tls/tasks/main.yml` (new — installs the script +
    cosign certs.d symlinks)
  - `ansible/guest/playbooks/chutes-miner-vm.yml` (registers the `vm-tls` role,
    ordered before the `luks` role)
  - `ansible/guest/roles/luks/files/initramfs/setup_storage` (unchanged by this
    feature; it deletes the ephemeral luks mTLS cert in `confirm_rotation()`)
  - `ansible/guest/roles/attestation-service/templates/proxy-manifests.yaml.j2`
  - `ansible/guest/roles/attestation-service/files/attestation-service-init.service`
  - `ansible/guest/roles/attestation-service/tasks/install-attestation-init-service.yml`
  - `ansible/guest/roles/k3s/templates/registries.yaml.j2`
  - `ansible/guest/roles/k3s/tasks/k3s-prereqs.yml`
  - `ansible/guest/roles/admission-controller/templates/cosign-registries.json.j2`
  - `ansible/guest/roles/admission-controller/tasks/configure-cosign.yml`
  - `ansible/guest/roles/admission-controller/defaults/main.yml`
  - `ansible/guest/roles/admission-controller/templates/opa-config-data.json.j2`
  - `ansible/guest/roles/system-manager/templates/system-manager.env.j2`
  - `ansible/guest/roles/rtmr3-measure/files/tdx-measure-miner.conf`
  - `ansible/guest/roles/apparmor-hardening/files/profiles/sek8s.attestation-proxy`
    (new), `apparmor-hardening/tasks/main.yml`, `verify-apparmor-profiles.sh`
  - `src/sek8s/sek8s/system_manager/images/util.py`, `src/sek8s/sek8s/config.py`
- **Removed**:
  - `ansible/guest/roles/attestation-service/files/service-init/setup-tls-certs.sh`
    — the userspace proxy-cert generator, superseded by initramfs generation.
- **External dependencies** (out of scope for this repo, required before rollout):
  - `chutes-api`: new `PUT /servers/{vm_name}/vm-root-ca` endpoint
  - `chutes-api`: dual-auth registry backend (mTLS path + legacy miner-header path)
  - `chutes-miner` chart: remove registry DaemonSet/Service/ConfigMap after
    migration completes (NOT part of this change)

---

## Design Decisions

- **Per-boot VM root CA (tmpfs only)**: A 4096-bit RSA CA keypair is generated
  fresh on every boot inside `setup_vm_tls` (initramfs `init-bottom`) and written
  to `/run/chutes/vm-root-ca/ca.{key,crt}` (tmpfs). The key never touches disk —
  it lives in TDX-encrypted DRAM only and is destroyed at the next reboot. There
  is no LUKS-persisted CA. Rotating every boot reduces the blast radius of a
  compromised key to a single boot window.

- **RTMR2 for the CA lifecycle, isolated from LUKS**: In this (release/next)
  architecture the entire CA lifecycle lives in a dedicated initramfs script —
  `setup_vm_tls` (`vm-tls` role, `PREREQ="setup_storage"`) — NOT inside
  `setup_storage`. `setup_storage` retains a single responsibility (LUKS + k3s
  encryption config) and is unaware of mTLS. Both scripts are baked into the
  RTMR2-measured initramfs, so any modification to the cert lifecycle code is
  detectable via attestation.

- **Ordering: after `setup_storage`, using its OWN CA as the client cert**:
  `setup_storage` runs at `PREREQ="rtmr3-measure"` and, in its
  `confirm_rotation()` step, deletes the ephemeral luks mTLS client cert
  (`/tmp/client_cert.pem`, `/tmp/client_key.pem`, `/run/chutes/cert-hash`) once
  the final `luks/confirm` call completes. `setup_vm_tls` runs after that
  (`PREREQ="setup_storage"`), so it CANNOT reuse the luks cert. It therefore
  mints its own `ca.crt`/`ca.key` and uses them as the mTLS client credential for
  the `PUT /servers/{name}/vm-root-ca` call — the validator receives a
  connection where the TLS client cert IS the cert being registered, proving key
  possession and identity in a single handshake.

- **Registration on every boot, no marker file**: The `PUT` fires on every boot;
  the validator performs an idempotent upsert. Each registration is TDX-attested
  with a fresh quote (`REPORTDATA = SHA256(ca_pubkey_der)`), so the validator
  always holds a recently-attested pubkey. No stale/orphaned key state.

- **All leaf certs generated in initramfs, `ca.key` deleted before userspace**:
  Both the registry client cert (`/run/chutes/registry-tls/client.{key,crt}`) and
  the attestation proxy server cert (`/run/chutes/proxy-tls/server.{key,crt}`) are
  signed immediately after CA creation, then `ca.key`/`ca.srl` are deleted (still
  in initramfs) before `pivot_root`. The CA key never exists in userspace — RTMR2
  is the attestation proof. All leaf certs live on tmpfs and evaporate at
  shutdown.

- **Attestation proxy server cert signed by the CA**: Generated with static SANs
  (`DNS:attestation-service`, `DNS:localhost`, `IP:127.0.0.1`, `IP:::1`), owned
  `root:tdx-attest 640` (GID resolved from `${rootmnt}/etc/group`). The proxy
  mounts it from `/run/chutes/proxy-tls/` (hostPath, tmpfs).

- **`CLIENT_CA_PATH` stays the system CA bundle**: The validator's TLS cert is
  issued by a standard CA; no custom validator CA distribution is needed.

- **Proxy caller auth is server-cert pinning + signed requests, not client-cert
  mTLS**: The validator connects to the external port (8443), verifies the
  proxy's server cert against the VM's registered attestation CA (server-cert
  pinning), and authenticates itself with signed request headers
  (`_sign_request`) — it does NOT present a TLS client cert. Client-cert mTLS
  (`MTLS_REQUIRED`) is therefore intentionally NOT enabled on the proxy; turning
  it on would reject the validator, which sends no client cert.

- **Single source of truth for server config**: The attestation proxy hosts its
  two ports concurrently via the shared `sek8s_common.server.WebServer.serve()`
  (async), not a bespoke runner. Both `serve()` and `run()` build their uvicorn
  arguments from one `_uvicorn_kwargs()`, so any TLS/mTLS/bind setting a config
  carries is always honoured — no server option can be silently dropped. If proxy
  client-cert mTLS is ever wanted, setting `mtls_required` on that server's
  config is sufficient and takes effect.

- **`registry.chutes.ai` dual-auth for migration**: Old VMs (no CA in DB)
  continue through the legacy miner-proxy path. New/upgraded VMs present a client
  cert; the registry backend checks whether a CA pubkey is stored for that VM.
  Migration is organic — no forced re-provisioning, and the chutes-miner chart
  registry DaemonSet stays in place until the fleet has migrated.

---

## API Changes

- **New endpoint (external — chutes-api)**: `PUT /servers/{vm_name}/vm-root-ca`
  - Auth: `X-Chutes-Hotkey: <miner_hotkey>` header + mTLS client cert (the CA cert itself)
  - Body: `{ "cert_pem": "<PEM string>", "quote": "<base64 TDX quote>" }`
  - Quote REPORTDATA: `SHA256(ca_pubkey_der)` — binds the CA cert to the TDX measurement
  - Behavior: verify TDX quote (same RTMR3 checks as `POST /servers`), upsert
    `vm_root_ca_cert` on the server record for `(miner_hotkey, vm_name)`
  - Idempotent: same CA cert on repeat calls is a no-op; a changed CA cert (new
    storage volume) updates the record

- **Registry backend change (external — chutes-api)**:
  - nginx: `ssl_verify_client optional_no_ca` on `registry.chutes.ai`; pass cert
    via `proxy_set_header X-Client-Cert $ssl_client_cert`; strip incoming
    `X-Client-Cert` from external requests
  - Backend logic: if `vm_root_ca_cert` stored for the requesting VM →
    verify client cert signed by that CA; else → verify legacy miner auth headers

- **Schema changes**: None in this repo. chutes-api adds `vm_root_ca_cert`
  to server records.

---

## Goal

Success =

1. A VM boots, generates its VM root CA, and registers `ca.crt` with the
   validator via `PUT /servers/{name}/vm-root-ca` before k3s starts.
2. On every boot, a leaf `clientAuth` cert is generated and placed at
   `/run/chutes/registry-tls/client.{crt,key}` (tmpfs).
3. containerd pulls images from `registry.chutes.ai` using the leaf cert for
   mTLS; the registry backend verifies the leaf cert against the stored CA.
4. cosign verifies image signatures against `registry.chutes.ai` using the same
   leaf cert (via `docker certs.d` symlinks).
5. The attestation proxy's server cert is signed by the CA; the validator pins
   it to the registered CA and authenticates with signed requests (no client
   cert).
6. Old VMs without a registered CA continue to pull via the legacy path.
7. Upgraded VMs self-register their CA on first boot of the new image.
8. `ca.key` is deleted in initramfs before `pivot_root` and never exists in
   userspace.

---

## Constraints

- All CA lifecycle code (generation + registration + leaf cert signing + key
  deletion) must remain in the RTMR2-measured initramfs (`setup_vm_tls`,
  `vm-tls` role, `PREREQ=setup_storage`). No userspace service generates or
  re-registers the CA.
- The CA cert MUST NOT be measured into RTMR3 — it is per-VM unique.
- `ca.key` is deleted in initramfs; it must never reach userspace.
- Leaf certs MUST live on tmpfs (`/run/chutes/{registry-tls,proxy-tls}/`) and be
  regenerated each boot.
- Do not change `CLIENT_CA_PATH` on the attestation proxy — it stays the system
  CA bundle.
- AppArmor profiles for new components must be added to `apparmor-hardening/`
  and their installed paths added to `tdx-measure-miner.conf`.
- The attestation proxy must host its ports via the shared
  `sek8s_common.server.WebServer` (`serve()` / `run()`) so server config is
  never reimplemented per call site. Do not hand-roll a `uvicorn.Config` that
  can drop TLS/mTLS settings.

---

## Failure Conditions

- `setup_vm_ca()` fails to generate or register the CA → boot halts (poweroff).
- `generate_registry_client_cert()` / `generate_proxy_server_cert()` fail → boot
  halts; no cert means no image pulls / no proxy.
- Proxy certs absent at `/run/chutes/proxy-tls/` → uvicorn fails to load
  `server.key` and the proxy pod crashes; indicates initramfs failure.
- AppArmor profile added but not measured into RTMR3 via `tdx-measure-miner.conf`
  → policy change is undetected; spec requires the profile path in the conf.
- `registries.yaml` still contains the `localregistry.chutes.ai` mirror →
  legacy path is used, mTLS bypassed.
- `cosign-registries.json.j2` retains `allow_insecure: true` → insecure pulls
  accepted, signature verification degraded.

---

## Rollout Notes

- **External prerequisites before deploying this VM image**:
  1. chutes-api: `PUT /servers/{vm_name}/vm-root-ca` endpoint live
  2. chutes-api: registry backend dual-auth logic deployed
  3. `registry.chutes.ai` nginx: `ssl_verify_client optional_no_ca` +
     `X-Client-Cert` header pass-through
- **Migration**: old VMs continue on the legacy path; upgraded VMs self-register
  and use mTLS. The chutes-miner chart registry DaemonSet is removed only once
  the whole fleet is confirmed migrated (separate change).
- **Measurement re-baselining**: this change is measurement-affecting (RTMR2
  initramfs script; RTMR3 AppArmor profile + config edits). Re-baselining is
  handled at release time.
- **No `ansible/guest/VERSION` bump during development** — done at release time.
- **Changelog fragment**: `changelogs/ops/unreleased/registry-mtls-auth.md`.
