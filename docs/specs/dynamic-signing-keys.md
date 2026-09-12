# Feature Spec: Dynamic Signing Key Retrieval via Root-of-Trust RSA Chain

**Date**: 2026-05-28  
**Status**: implemented

> **Update (signature primitive changed to raw RSA):** The root-of-trust chain
> described below was originally built on OpenPGP detached signatures verified
> with `gpgv`. It has since been switched to **raw RSA (PKCS#1 v1.5, SHA-256)**
> verified with `openssl dgst -sha256 -verify`, because the root signing key is
> now held by an external RSA signer that cannot emit OpenPGP signatures. The
> trust chain, the measured locations (RTMR1 + RTMR3), the JSON bundle shape,
> the tmpfs output paths, and the fail-closed behavior are all **unchanged** —
> only the signature primitive over the raw key bytes changed. The root key is
> now the RSA public key `/etc/chutes/root-signing-key.pem`. `helm-pubkey.gpg`
> remains a byte-identical OpenPGP key file consumed by Helm for chart
> provenance. Where the text below says PGP/`gpgv`, read RSA/`openssl`.

---

## Context

Cosign public keys (`chutes.pub`, `dockerhub.pub`) and the Helm PGP keyring (`helm-pubkey.gpg`) are currently baked into the VM image at Ansible build time and measured into RTMR3. Rotating any of these keys requires a full image rebuild, a new RTMR3 value, and a version bump — the same lifecycle as a code change. The validator auth SS58 was recently made dynamic (fetched at boot, written to `/run/chutes/`, not measured) which proved the pattern works.

This feature switches cosign and Helm keys to the same dynamic retrieval pattern, but adds a **root-of-trust PGP chain**: a dedicated root signing PGP public key is baked into the image and measured in RTMR3. Cosign and Helm keys are fetched from an API endpoint at boot, and their PGP signatures (made with the root signing private key) are verified against the attested root key before use. Key rotation requires only re-signing and publishing — no image rebuild, no RTMR3 change, no version bump.

- **Packages affected**: `ansible/guest/roles/admission-controller`, `ansible/guest/roles/chutes-gpu`, `ansible/guest/roles/rtmr3-measure`, `sek8s.config`, `sek8s.validators`
- **Key files**:
  - `ansible/guest/roles/admission-controller/tasks/configure-cosign.yml` — static cosign key copy (to be removed)
  - `ansible/guest/roles/admission-controller/templates/admission-controller.env.j2` — cosign key paths
  - `ansible/guest/roles/admission-controller/templates/cosign-registries.json.j2` — per-registry key paths
  - `ansible/guest/roles/chutes-gpu/tasks/setup_chutes.yml` — static Helm key copy (to be replaced)
  - `ansible/guest/roles/chutes-gpu/defaults/main.yml` — `helm_chart_public_key_path` variable
  - `ansible/guest/roles/rtmr3-measure/files/tdx-measure-miner.conf` — RTMR3 path list
  - `ansible/guest/roles/rtmr3-measure/files/initramfs/rtmr3-measure-hook` — initramfs hook
  - `ansible/guest/roles/k3s/files/cluster-init/04-helm-chart-upgrade.sh` — Helm keyring path
  - `ansible/guest/inventory.yml` — build-time key path variables
  - `src/sek8s/sek8s/config.py` — `AdmissionConfig` default key paths
  - `ansible/guest/roles/luks/files/initramfs/write-validator-auth` — existing dynamic pattern reference
- **Dependencies**: `openssl` (RSA verifier, `openssl dgst -sha256 -verify`), `curl`, `jq`, `base64` — all already staged into the initramfs by the LUKS `fetch_key` hook; no new binary is added

---

## Design Decisions

- **Dedicated root signing PGP key (not reusing the Helm key)**: The root key serves a distinct purpose — authenticating all dynamically-fetched leaf keys. A dedicated key has its own rotation cadence (very rare, requires image rebuild) and can be stored in an HSM. The Helm key is a leaf key that may rotate independently.
- **Raw RSA (PKCS#1 v1.5, SHA-256)**: The root key is held by an external RSA signer that cannot produce OpenPGP signatures, so the bundle is signed as raw RSA over the base64-decoded key bytes and verified with `openssl dgst -sha256 -verify`. `openssl` (and `libcrypto`) is already staged in the initramfs by the LUKS `fetch_key` hook, so no new crypto tooling is required. (Originally this was OpenPGP verified with `gpgv`.)
- **Root key path: `/etc/chutes/root-signing-key.pem`**: The `/etc/chutes` directory is already measured into RTMR3 via `tdx-measure-miner.conf`. Adding a file here requires no measurement config changes for the root key itself.
- **Dynamic keys stored in `/run/chutes/signing-keys/`**: Consistent with the validator auth pattern (`/run/chutes/validator-auth.env`). Tmpfs, fully ephemeral, cleared on reboot. Not measured in RTMR3 — trust is proven via the PGP signature chain, not direct measurement.
- **Cosign keys removed from RTMR3 measurement**: `/etc/admission-controller/cosign` is removed from `tdx-measure-miner.conf`. The directory may still exist (for structure) but contains no keys at runtime. Trust in the keys is delegated to the PGP chain: RTMR3 attests root pubkey → root pubkey verifies PGP sig → PGP sig authenticates cosign key.
- **Helm key stored outside `/etc/chutes/`**: The dynamic Helm key must NOT be written to `/etc/chutes/` because that directory is recursively measured in RTMR3. Writing a dynamic file there would make RTMR3 non-deterministic. It goes to `/run/chutes/signing-keys/helm-pubkey.gpg` instead.
- **Build-time Helm install still uses a static key**: At image build time, the Helm chart is installed with a static key (the current `helm_chart_public_key_path`). This key is only needed during the build — at boot, `04-helm-chart-upgrade.sh` uses the dynamically-fetched key from `/run/chutes/signing-keys/`. The build-time key does not need to be baked into the final image.
- **Fetch in initramfs init-bottom (not systemd service or cluster-init)**: Init-bottom scripts run after the root filesystem is mounted and after `fetch_key_and_unlock` (init-premount) has established network connectivity. This ensures keys are available before any userspace service starts. The initramfs itself is covered by RTMR1, so the fetch and verification logic cannot be tampered with without changing RTMR1.
- **Fatal failure on verification failure**: If any PGP signature fails verification, the VM powers off — same pattern as `rtmr3-measure`. This is fail-closed by design.
- **API serves a JSON key bundle**: A single `GET` request returns all keys and their detached PGP signatures as base64-encoded strings. This minimizes boot-time network calls and allows atomic key set updates.
- **Multiple keys for rotation overlap**: The API can serve 2–3 cosign keys simultaneously to support VMs that haven't rebooted during a rotation window. The initramfs script fetches all keys in the bundle and installs them.

---

## API Changes

- **New endpoint**: `GET /servers/signing-keys` (public, no auth required — keys are public)
- **Response schema**:

```json
{
  "version": 1,
  "keys": {
    "cosign/chutes.pub": "<base64-encoded key>",
    "cosign/dockerhub.pub": "<base64-encoded key>",
    "helm-pubkey.gpg": "<base64-encoded key>"
  },
  "signatures": {
    "cosign/chutes.pub": "<base64-encoded detached PGP signature>",
    "cosign/dockerhub.pub": "<base64-encoded detached PGP signature>",
    "helm-pubkey.gpg": "<base64-encoded detached PGP signature>"
  }
}
```

- **Schema changes**: None to existing services. The new endpoint lives on the validator API (`api.chutes.ai`).
- **URL derivation**: The initramfs `fetch-signing-keys` script reads the URL from `/etc/chutes/signing-keys.conf` (set at build time to `$VALIDATOR_BASE_URL/servers/signing-keys` by the `signing-keys` Ansible role).
- **Migrations**: None.

---

## Goal

Success = Cosign and Helm keys are fetched dynamically at boot, verified against an attested root PGP key, and used for admission control and chart provenance — without baking the leaf keys into the image. Specifically:

1. A VM boots, fetches the key bundle from the API, verifies all PGP signatures against the root key at `/etc/chutes/root-signing-key.pem`, and writes verified keys to `/run/chutes/signing-keys/`.
2. The admission controller starts and reads cosign keys from `/run/chutes/signing-keys/cosign/` — image admission works identically to the static-key behavior.
3. `04-helm-chart-upgrade.sh` reads the Helm keyring from `/run/chutes/signing-keys/helm-pubkey.gpg` — chart provenance verification works identically.
4. A key rotation (new cosign key signed with root PGP key, published to API) is picked up by VMs on next reboot with zero image changes.
5. A tampered key bundle (invalid PGP signature) causes the VM to power off during initramfs — fail closed.
6. RTMR3 does not change when cosign or Helm keys are rotated — only the root signing key (which rarely rotates) is measured.
7. A third-party verifier can reproduce the trust chain: RTMR3 quote → root PGP pubkey hash → PGP signature on cosign key → cosign key identity.
8. All existing tests continue to pass with updated default key paths.

---

## Constraints

- The root signing PGP private key must be stored offline or in an HSM. It is never present on any VM or in any hot-path service. It is used only when rotating cosign/Helm keys (an infrequent, manual operation).
- The root signing PGP public key must be present at `root_signing_key_path` on the build machine at Ansible build time. Missing key is a hard build failure.
- The initramfs `fetch-signing-keys` script must use only tools available in initramfs: `sh`, `curl`, `openssl`, `jq`, `base64`, `mkdir`, `chmod`.
- `curl`, `jq`, `base64`, and `openssl` are already pulled into the initramfs by the LUKS `fetch_key` hook. The `fetch-signing-keys-hook` reuses them and stages only the root RSA public key and `signing-keys.conf`.
- The fetch script must complete within 30 seconds (matching `TDX_TIMEOUT` from `fetch_key_and_unlock`). Network is already established by `fetch_key_and_unlock` (init-premount).
- Dynamic keys go to `/run/chutes/signing-keys/` only — never to the root filesystem, never to a measured path.
- The `/etc/chutes/` directory must not contain any dynamic files. Static build-time files (root signing key, chart-versions, chart-configs) remain there and are measured in RTMR3.
- The `admission-controller.env` file (measured in RTMR3) must reference the new `/run/chutes/signing-keys/cosign/` paths. This is a one-time RTMR3 change at the time this feature ships.
- `cosign-registries.json` (measured in RTMR3) must also reference the new paths — same one-time change.
- Do not modify `ImageConfig.cosign_public_key_path` or `ImageManager` — the system-manager images router uses a separate cosign key path for image pull verification.
- The API endpoint must be reachable from inside the TDX VM at boot time (HTTPS, public internet, same as `VALIDATOR_BASE_URL`).
- AppArmor profiles for `sek8s.system-manager` and any admission-controller confinement must allow reads from `/run/chutes/signing-keys/`.

---

## Output Format

### Ansible: New files

1. **`ansible/guest/roles/signing-keys/tasks/main.yml`** (new role) — installs root signing PGP public key to `/etc/chutes/root-signing-key.pem`, creates `/etc/chutes/signing-keys.conf` with the API URL, installs the initramfs hook and init-bottom script.

2. **`ansible/guest/roles/signing-keys/files/initramfs/fetch-signing-keys`** — init-bottom script that fetches the key bundle from the API, verifies PGP signatures with `gpgv`, writes verified keys to `/run/chutes/signing-keys/`, and powers off on any failure.

3. **`ansible/guest/roles/signing-keys/files/initramfs/fetch-signing-keys-hook`** — initramfs hook that copies `gpgv`, `base64`, the root signing key, and `signing-keys.conf` into the initramfs.

4. **`ansible/guest/roles/signing-keys/defaults/main.yml`** — default variables: `root_signing_key_path`, `signing_keys_api_url`.

### Ansible: Modified files

5. **`ansible/guest/roles/admission-controller/tasks/configure-cosign.yml`** — remove the two `copy` tasks that bake `chutes.pub` and `dockerhub.pub` into `/etc/admission-controller/cosign/`. Keep directory creation.

6. **`ansible/guest/roles/chutes-gpu/tasks/setup_chutes.yml`** — remove the "Install Helm PGP keyring" task (static key copy to `/etc/chutes/helm-pubkey.gpg`). Build-time `helm upgrade --install` still uses a temporary copy of the key for the build only (not persisted in image).

7. **`ansible/guest/roles/admission-controller/templates/admission-controller.env.j2`** — change `CHUTES_PUBLIC_KEY_PATH` and `DOCKERHUB_PUBLIC_KEY_PATH` to `/run/chutes/signing-keys/cosign/chutes.pub` and `/run/chutes/signing-keys/cosign/dockerhub.pub`.

8. **`ansible/guest/roles/admission-controller/templates/cosign-registries.json.j2`** — change all `public_key` paths from `/etc/admission-controller/cosign/` to `/run/chutes/signing-keys/cosign/`.

9. **`ansible/guest/roles/rtmr3-measure/files/tdx-measure-miner.conf`** — remove line `/etc/admission-controller/cosign`. Add comment explaining trust is delegated to PGP chain via attested root key in `/etc/chutes/root-signing-key.pem`.

10. **`ansible/guest/roles/k3s/files/cluster-init/04-helm-chart-upgrade.sh`** — change `KEYRING_FILE` from `/etc/chutes/helm-pubkey.gpg` to `/run/chutes/signing-keys/helm-pubkey.gpg`.

11. **`ansible/guest/inventory.yml`** — add `root_signing_key_path: "~/.chutes/root-signing-key.pem"` and `signing_keys_api_url` variables. Keep existing cosign key path vars (still used at build time for initial chart signing setup, but no longer baked into guest image).

12. **`ansible/guest/playbooks/chutes-miner-vm.yml`** — add `signing-keys` role to the play, after `admission-controller` and before `rtmr3-measure`.

### Python: Modified files

13. **`src/sek8s/sek8s/config.py`** — update default values for `AdmissionConfig.chutes_public_key_path` and `dockerhub_public_key_path` to `/run/chutes/signing-keys/cosign/chutes.pub` and `/run/chutes/signing-keys/cosign/dockerhub.pub`.

### AppArmor / security

14. **AppArmor profiles** — add `/run/chutes/signing-keys/**` read permission to `sek8s.system-manager` and any admission-controller profile.

### Tests

15. **`tests/unit/test_config.py`** — update any assertions on default cosign key paths.
16. **`tests/unit/test_validators.py`** / **`tests/unit/test_cosign_rules.py`** — update fixtures that reference old key paths.

---

## Failure Conditions

- The `fetch-signing-keys` script succeeds when a PGP signature is invalid (must power off).
- The `fetch-signing-keys` script succeeds when the root signing key is missing from the initramfs (must power off).
- The `fetch-signing-keys` script succeeds when the API is unreachable after retries (must power off — fail closed).
- Dynamic keys are written to a path that is measured in RTMR3 (would make RTMR3 non-deterministic on key rotation).
- Dynamic keys are written to the root filesystem instead of tmpfs (would persist across reboots, could be tampered with offline).
- The Helm dynamic key is written to `/etc/chutes/` (measured directory — breaks RTMR3 determinism).
- The admission controller fails to start because key files don't exist at the new paths (ordering: `fetch-signing-keys` must complete before pivot_root, keys must be at `/run/chutes/signing-keys/` when services start).
- `cosign-registries.json` or `admission-controller.env` still references the old `/etc/admission-controller/cosign/` paths.
- `04-helm-chart-upgrade.sh` still references `/etc/chutes/helm-pubkey.gpg` instead of the dynamic path.
- The root signing PGP private key is present on the VM or in any online service (must be offline/HSM only).
- `openssl` is not present in the initramfs (it must be staged by the LUKS `fetch_key` hook), causing `fetch-signing-keys` to fail on every boot.
- `ImageConfig.cosign_public_key_path` or `ImageManager` is modified (separate concern, must not change).
- Any keyless-verified or disabled registry entries in `cosign-registries.json` are changed.

---

## Rollout Notes

- **Root signing key provisioning** (one-time, before first build): the root key
  is an RSA key held by the external signer; its private half stays there. Export
  only the public half (PEM) to `~/.chutes/root-signing-key.pem` on build machines.
- **Sign existing cosign/Helm keys** before first build. Signatures are RSA
  PKCS#1 v1.5 over SHA-256 of the raw key bytes; the base64-encoded signature
  bytes are what the API serves. With a local private key the equivalent is:
  ```bash
  openssl dgst -sha256 -sign root-priv.pem -out chutes.pub.sig      chutes.pub
  openssl dgst -sha256 -sign root-priv.pem -out dockerhub.pub.sig   dockerhub.pub
  openssl dgst -sha256 -sign root-priv.pem -out helm-pubkey.gpg.sig helm-pubkey.gpg
  ```
- **API endpoint** must be live and serving the signed key bundle before any VM with this feature boots.
- **Hard cut-over on guest image build**: Old images continue to work with baked-in keys. New images require the API endpoint. There is no backward-compatible fallback in the new image — if the API is down at boot, the VM powers off.
- **RTMR3 changes**: This feature changes RTMR3 (new paths in `admission-controller.env`, removed `/etc/admission-controller/cosign` from measurement, root signing key added to `/etc/chutes/`). This is a one-time change. All subsequent key rotations leave RTMR3 unchanged.
- **Key rotation workflow** (post-rollout):
  1. Generate new cosign key pair.
  2. Sign the new public key with the root RSA key (PKCS#1 v1.5, SHA-256).
  3. Update the API endpoint to serve the new key + signature (keep old key for transition window).
  4. VMs pick up the new key on next reboot. No image rebuild, no version bump.
- **Changelog fragment**: `changelogs/vm/unreleased/<branch-name>.md` under `### Changed`.
- **Version bump**: This feature changes RTMR3 and the initramfs, so it requires a VM version bump when shipping. Subsequent key rotations do not.
