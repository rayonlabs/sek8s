# Proposal: fold VM root-CA registration into the runtime attestation quote

**Status**: decided / in progress
**Repos**: `chutes-api` + `sek8s` (coordinated change)
**Related**: [`registry-mtls-auth.md`](registry-mtls-auth.md)

## Decision (2026-07)

We adopted the fold-in, and went one step further than the original proposal below:

- **Single CA everywhere.** The VM root CA is generated up front in `fetch_key_and_unlock`
  (init-premount) and used as the VM's one mTLS client identity for *every* boot call
  (nonce, boot attestation, root confirm, and the runtime storage attestation). The throwaway
  `CN=tdx-vm-*` client cert is eliminated.
- **New `/provision` namespace.** The runtime, RTMR3-attested call is a purpose-named
  `POST /servers/{vm}/provision` (+ `POST /servers/{vm}/provision/confirm`), replacing
  `/luks/attest` and the storage `/luks/confirm`. Legacy `/luks/*` routes are kept for in-field
  VMs and retired once the fleet upgrades; both share the same handler helpers. The endpoint's
  contract is "prove runtime state (RTMR3) + VM identity → receive provisioning secrets," so it is
  adaptable to future provisioning needs.
- **CA recorded only at `/provision`.** The security invariant: the validator persists
  `vm_root_ca_cert` only from the RTMR3-extended `/provision` quote (never the RTMR3=0 boot quote).
  This equals the old `PUT /vm-root-ca` guarantee **plus** the `luks_quote_nonce` anti-replay the
  PUT lacked.
- **`PUT /servers/{vm}/vm-root-ca` removed** (endpoint, `register_vm_root_ca`,
  `verify_vm_root_ca_quote`, `VmRootCaRequest`). The `vm_root_ca_cert` column + migration stay.
- **365-day cert validity** (CA + both leaves), up from 1 day, so long-running VMs don't hit
  expiry mid-run. Per-boot rotation is preserved.

The investigation notes below are retained for context.

---

## Question

Do we need the separate `PUT /servers/{vm_name}/vm-root-ca` endpoint at all, or can the
per-VM root CA be attested + stored using a quote the VM already sends during boot/storage
setup in initramfs?

## Finding: today the endpoint is required, but only because of an ordering choice

The CA does **not exist** when the boot/luks attestation calls run. The sek8s boot sequence
(all in initramfs, pre-pivot):

1. `init-premount` (`fetch_key_and_unlock`): generate a **throwaway self-signed** client cert
   (`CN=tdx-vm-<ts>`) → `GET /nonce` → `POST /boot/attestation` → open root LUKS → `luks/confirm`
2. `init-bottom` `rtmr3-measure`: **extends RTMR3** with real-root file hashes
3. `init-bottom` `setup_storage`: `POST /luks/attest` → rotate storage → `luks/confirm`
   (which then **deletes the ephemeral client cert**)
4. `init-bottom` `setup_vm_tls`: **generate the VM root CA** (`sek8s-vm-root-ca`, RSA-4096) →
   `PUT /vm-root-ca` → sign proxy/leaf certs → delete `ca.key` → pivot

So at luks/attest time the registry CA hasn't been minted. The cert whose hash is bound in the
boot/luks quotes is a **different, throwaway** self-signed cert that is deleted before the CA
is created.

### REPORTDATA layouts (64 bytes total)

| Quote | `[:64]` | `[64:128]` |
|---|---|---|
| boot / luks attest (`verify_quote`) | nonce (anti-replay) | `SHA256(ephemeral client-cert pubkey)` |
| vm-root-ca (`verify_vm_root_ca_quote`) | `SHA256(CA pubkey)` | *unused — and no nonce* |

The boot/luks quote already binds `nonce ‖ SHA256(client-cert pubkey)` and proves key
possession of that cert via the mTLS handshake — the exact shape `PUT /vm-root-ca` needs. The
only reason it can't double as CA registration is that the cert presented there is the
throwaway, not the CA.

## Proposal

Have sek8s **generate the CA before the `luks/attest` call and present the CA as that call's
mTLS client cert.** Then, with no new crypto machinery:

- `REPORTDATA[64:128]` = `SHA256(CA pubkey)` (the CA is now the bound "cert_hash")
- the mTLS handshake proves CA private-key possession
- the `luks/attest` handler stores `vm_root_ca_cert = the presented client cert`

This **removes** `PUT /vm-root-ca` (endpoint, its quote, one round-trip) and is strictly
**stronger**: it picks up the `luks_quote_nonce` anti-replay the vm-root-ca quote currently
lacks entirely.

### Why it's feasible

- The CA lives in tmpfs (`/run/chutes/vm-root-ca`) and does **not** depend on storage being up,
  so it can be generated earlier in `init-bottom`.
- `luks/attest` already carries the fully-extended RTMR3 that the vm-root-ca quote uses today,
  so the measurement config it validates against is unchanged.
- The CA must **not** be measured into RTMR3 (it's per-VM unique) — unchanged; only the
  *code* is measured (RTMR2 initramfs).

### Tradeoffs (why it was split out originally)

- Cross-repo, coordinated change; reverses a deliberate "generate the CA last, using its own
  CA" decision.
- Couples CA registration into the storage-attestation handler (two concerns in one path).
- The ephemeral boot cert is still needed for `POST /boot/attestation` (precedes CA gen),
  so there's slightly more cert juggling — unless the CA is moved earlier still (before boot
  attestation), which changes which measurement config validates it.

## Concrete changes if we do it

**sek8s** (`ansible/guest/roles/...`):
- Generate the VM root CA before the `luks/attest` call (move CA gen ahead of `setup_storage`'s
  quote, or into `setup_storage` before `post_sync_keys`).
- Use `ca.crt`/`ca.key` as the mTLS client cert for `luks/attest` instead of the ephemeral cert
  (or in addition, keeping the ephemeral for boot attestation only).
- Drop the `PUT /vm-root-ca` call and its dedicated quote.

**chutes-api**:
- In the `luks/attest` handler, after quote verification, store `vm_root_ca_cert = the client
  cert` (the `extract_client_cert` cert; its pubkey hash is already the verified `cert_hash`).
- Remove `PUT /servers/{vm_name}/vm-root-ca` (`put_vm_root_ca`), `register_vm_root_ca`,
  `verify_vm_root_ca_quote`, and `VmRootCaRequest`.
- Keep `verify_leaf_cert_signed_by_ca`, `lookup_server_by_ip`, the registry version gate, and
  the attestation-proxy client change — all unchanged.

## Open questions to confirm in sek8s before committing

1. Is there a hard reason CA generation must follow `setup_storage` (does the CA sign anything
   that depends on storage/cache being mounted)? The cert-signing (`sign proxy/leaf certs`) can
   stay in `setup_vm_tls`; only CA **generation** needs to move earlier.
2. Does the `luks/attest` version gate (VM `>= 1.3.0`) and the registry mTLS gate (`>= 1.4.0`)
   interact badly if a `1.3.x` VM starts registering a CA via luks/attest before it's forced
   onto registry mTLS? (Likely fine — CA stored early, used once version crosses the gate.)
3. Any consumer that relies on the ephemeral self-signed cert's hash specifically (rather than
   "some TEE-controlled cert") for boot/luks attestation? If the CA replaces it for luks/attest,
   the bound cert changes identity.

---

## sek8s investigation prompt (paste into a Claude Code session in ~/Code/Chutes/sek8s)

> Explore this repo (~/Code/Chutes/sek8s). I'm evaluating whether the per-VM registry root CA
> can be generated *before* the `POST /servers/{vm}/luks/attest` call in initramfs and presented
> as that call's mTLS client cert — so the existing luks/attest quote binds `SHA256(CA pubkey)`
> and the separate `PUT /servers/{vm}/vm-root-ca` call can be removed.
>
> Answer with file:line evidence:
> 1. Exactly where is the VM root CA generated (`sek8s-vm-root-ca`, `ca.key`/`ca.crt`) and what
>    is the earliest point in the initramfs boot it *could* be generated? Does anything it does
>    require storage/cache to be mounted first, or only tmpfs?
> 2. Where is the ephemeral self-signed client cert (`CN=tdx-vm-*`) generated, and where is it
>    used as the mTLS client cert (nonce, boot/attestation, luks/attest, luks/confirm)? Where is
>    it deleted?
> 3. Where is the `luks/attest` REPORTDATA built (`nonce ‖ cert_hash`) and where does `cert_hash`
>    come from? Could `cert_hash` instead be `SHA256(CA pubkey)` if the CA were the client cert?
> 4. What exactly does `setup_vm_tls` do with the CA (sign proxy server cert, sign registry leaf
>    certs, `PUT /vm-root-ca`)? Which of those steps depend on ordering after `setup_storage`?
> 5. Sketch the minimal reorder: generate CA → use it as the luks/attest client cert → drop the
>    vm-root-ca call, keeping cert-signing where it is. What breaks?
