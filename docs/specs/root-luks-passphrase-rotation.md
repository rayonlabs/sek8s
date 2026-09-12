# Feature Spec: Root Volume LUKS Passphrase Rotation

**Date**: 2026-05-26  
**Status**: draft

---

## Context

The root volume is LUKS-encrypted at image build time with a shared passphrase. At runtime, the passphrase is delivered via TDX-attested boot attestation. Storage and cache volumes already support per-VM passphrase rotation (add next key slot, confirm, remove old slot). The root volume does not rotate because:

1. The QEMU overlay preserves the base image's LUKS header permanently, so `luksRemoveKey` in the overlay still leaves the original key slot in the base image. The LUKS master key is the same across all key slots, so the build-time passphrase can always derive it.
2. The API cannot distinguish a reboot (needs rotated passphrase) from a relaunch with a fresh image (needs build-time default), since VMs are keyed by `(miner_hotkey, vm_name)`.

This feature solves both problems: drop the overlay so the per-VM image is the only copy of the LUKS header, and add a LUKS2 token as a "first boot" marker so the API knows which passphrase to return.

- **Packages affected**: `ansible/guest/roles/luks`, `host-tools/scripts`
- **Key files**: `src/chutes-cvm/chutes_cvm/guest/launch.py` (the `_prepare_vm_image()` function), `host-tools/scripts/quick-launch.sh`, `ansible/guest/roles/luks/tasks/luks_encrypt.yml`, `ansible/guest/roles/luks/files/initramfs/fetch_key_and_unlock`, `ansible/guest/roles/luks/files/initramfs/fetch_key`
- **Dependencies**: Chutes API changes (separate repo -- see API prompt at end of this spec)

---

## Design Decisions

- **Drop the QEMU overlay.** Replace with a per-VM copy of the base image. Only one VM runs per host (all GPUs passed through), so there is no disk space penalty. Without the overlay, `luksRemoveKey` destroys the old key slot in-place on the only copy -- same security model as storage/cache.
- **SHA256 prefix in the per-VM filename** (`tdx-<hostname>-<sha256prefix>.qcow2`). Detects base image version changes without mounting or parsing the per-VM image. On upgrade, the filename won't match the new SHA, triggering a fresh copy.
- **LUKS2 token as first-boot marker.** Token ID 15, type `chutes-first-boot`, with the image version embedded as local debug metadata. Readable from the locked device via `cryptsetup token export`. Removed immediately after successful `luksOpen`.
- **Root rotation is self-contained in `fetch_key_and_unlock`.** Boot attestation returns `root_next` and `root_confirm_nonce`; the script does `luksAddKey`, confirms, and `luksRemoveKey` all in init-premount. Storage/cache rotation in `setup_storage` is unchanged.
- **Separate confirm calls per stage.** Root confirms via `POST /servers/<vm>/luks/confirm` from init-premount with its own nonce. Storage/cache confirms from init-bottom with their own nonce. The API endpoint is the same, just called twice with different nonce/volume sets.

---

## API Changes

- **No new endpoints.**
- **Schema changes to `POST /servers/boot/attestation`**:
  - Request body gains `first_boot` (bool). The API determines the image version from the TDX quote's attestation measurements; no `image_version` field is needed from the client.
  - Response body gains `root_next` (string | null) and `root_confirm_nonce` (string | null)
- **`POST /servers/<vm>/luks/confirm`** is unchanged structurally -- now called from two initramfs stages instead of one. Root confirm body: `{"volumes": {"root": {"rotated": true/false}}}`
- **`POST /servers/<vm>/luks/attest`** is unchanged -- continues to handle storage + cache only.
- **Migrations**: Per-VM root passphrase storage (keyed by `(miner_hotkey, vm_name)`). Per-version default root passphrase lookup (keyed by `image_version`).

---

## Goal

Success = on every boot, the correct root passphrase is returned by the API and the root volume unlocks, across all lifecycle scenarios:

1. **New server**: fresh copy from base image, token present, API returns build-time default, unlock succeeds.
2. **Normal reboot**: per-VM image persists, token absent, API returns rotated passphrase, unlock succeeds.
3. **Upgrade**: new base image has new SHA prefix, fresh copy created, token present with new version, API returns default for new version.
4. **Relaunch** (miner deletes per-VM image): fresh copy from base, token present, API returns build-time default, API resets stored root passphrase state.
5. **Root passphrase rotation**: `luksAddKey` succeeds, confirm succeeds, `luksRemoveKey` removes old slot. On next reboot, API returns the new passphrase.
6. **Crash during rotation**: VM recovers on next boot -- either the old passphrase still works (if confirm didn't happen) or the new one works (if confirm succeeded).

---

## Constraints

- Token detection and removal must work on a locked LUKS2 device in initramfs (no dm-crypt open required for header metadata operations).
- Root rotation (addKey, confirm, removeKey) must complete before saving state to `/run/chutes/` and before init-bottom.
- The `fetch_key` initramfs hook must include `cryptsetup token` subcommand support (it should, since `cryptsetup` is already packed, but verify).
- The base image at `/var/lib/chutes/base-images/` is never modified at runtime.
- `--ephemeral` mode may continue to use an overlay for debug purposes (special case).
- No changes to `setup_storage` -- it continues to own storage + cache exclusively.

---

## Output Format

### 1. `src/chutes-cvm/chutes_cvm/guest/launch.py` (the `_prepare_vm_image()` function)

Replace overlay creation with per-VM copy. The base image is a verified image-set directory.

```
Input:  BASE_IMAGE_SET_DIR, HOSTNAME, VM_IMAGE_DIR
Output: path to per-VM image (stdout)

Logic:
  1. Verify the set against its manifest (chutes_cvm.guest.image_set verify); read the qcow2 sha256 from the manifest
  2. VM_IMAGE="$VM_IMAGE_DIR/tdx-${HOSTNAME}-${SHA:0:16}.qcow2"
  3. If exists: reuse
  4. If not: cp "$BASE_IMAGE" "$VM_IMAGE"
  5. Clean up stale images: rm tdx-${HOSTNAME}-*.qcow2 that don't match current SHA
  6. Print VM_IMAGE path
```

### 2. `host-tools/scripts/quick-launch.sh`

- Rename `--overlay-dir` to `--vm-image-dir` (default: `/var/lib/chutes/vm-images/`)
- Update Step 4b to call `prepare-vm-image.sh` with `$VM_IMAGE_DIR` instead of `$OVERLAY_DIR`
- Update variable names: `OVERLAY_IMAGE` -> `VM_IMAGE`
- Pass `VM_IMAGE` (not overlay) to `chutes-cvm guest launch`

### 3. `ansible/guest/roles/luks/tasks/luks_encrypt.yml`

After the "Create LUKS container" task (line ~129), add:

```yaml
- name: Add first-boot LUKS2 token
  ansible.builtin.shell: >-
    cryptsetup token add {{ root_partition }}
    --token-id 15
    --json '{"type":"chutes-first-boot","keyslots":[],"version":"{{ vm_version }}"}'
  no_log: true
```

### 4. `ansible/guest/roles/luks/files/initramfs/fetch_key_and_unlock`

Add to the `main()` function:

**Before attestation POST** (after config volume reads, before `fetch_luks_key`):
```sh
FRESH_IMAGE="false"
IMAGE_VERSION=""
if token_json=$(cryptsetup token export "$DEVICE_PATH" --token-id 15 2>/dev/null); then
    FRESH_IMAGE="true"
    IMAGE_VERSION=$(echo "$token_json" | jq -r '.version // empty')
fi
```

**Update attestation POST body** to include `first_boot`:
```json
{"quote":"...","vm_name":"...","miner_hotkey":"...","first_boot":true}
```

**After successful `luksOpen`, before saving state**:
```sh
cryptsetup token remove "$DEVICE_PATH" --token-id 15 2>/dev/null || true
```

**Root rotation block** (after token removal, before saving state to `/run/chutes/`):
```sh
ROOT_NEXT="..."   # extracted from boot attestation response .root_next
ROOT_CONFIRM="..."  # extracted from .root_confirm_nonce

if [ -n "$ROOT_NEXT" ] && [ -n "$ROOT_CONFIRM" ]; then
    # Add new key slot
    luks_add_key "$DEVICE_PATH" "$LUKS_KEY" "$ROOT_NEXT"
    ROOT_KEY_ADDED=$?

    if [ "$ROOT_KEY_ADDED" -eq 0 ]; then
        # Confirm with API
        http_code=$(curl -s -w "%{http_code}" -X POST \
            -H "X-Confirm-Nonce: $ROOT_CONFIRM" \
            -H "X-Chutes-Hotkey: $HOTKEY" \
            -H "Content-Type: application/json" \
            --max-time "$TIMEOUT" --cacert "$API_CA_CERT" \
            -d '{"volumes":{"root":{"rotated":true}}}' \
            -o /dev/null \
            "${VALIDATOR_BASE_URL}/servers/${VM_NAME}/luks/confirm")

        if [ "$http_code" = "200" ]; then
            luks_remove_key "$DEVICE_PATH" "$LUKS_KEY"   # remove old
        else
            luks_remove_key "$DEVICE_PATH" "$ROOT_NEXT"  # rollback
        fi
    fi
fi
```

Requires `luks_add_key`, `luks_remove_key`, `write_key_file`, `shred_key_file` helpers -- either duplicate from `setup_storage` (they're small, ~40 lines total) or extract to a shared initramfs include.

### 5. `ansible/guest/roles/luks/files/initramfs/fetch_key` (hook)

Verify `cryptsetup token` subcommands work with the packed binary. No changes expected since full `cryptsetup` is already included.

---

## Failure Conditions

- Root passphrase rotation must not leave the volume with zero valid key slots under any crash scenario.
- A `first_boot=true` signal when the LUKS header actually has a rotated passphrase must result in VM poweroff (unlock failure), not silent data exposure.
- The base image at `/var/lib/chutes/base-images/` must never be modified by any runtime operation.
- Stale per-VM images from a previous version must be cleaned up on upgrade (no orphaned images accumulating).
- The `--ephemeral` flag must still work (overlay or tmpfs copy for debug).
- Root rotation confirm failure must cleanly rollback (`luksRemoveKey` the newly-added slot), leaving the volume in single-slot state with the current passphrase.

---

## Rollout Notes

- **Image rebuild required**: The LUKS2 token is burned in at build time. Existing images without the token will boot normally (token absent = `first_boot=false`), so this is backward compatible for existing VMs.
- **API must be deployed first**: The API needs to accept `first_boot` in the boot attestation request and return `root_next`/`root_confirm_nonce` in the response before the new VM image is deployed. The API should ignore unknown fields and return `null` for `root_next`/`root_confirm_nonce` until the feature is enabled server-side.
- **Host-tools update**: `prepare-vm-image.sh` and `quick-launch.sh` changes can be deployed independently -- they only affect the host-side image management, not the guest boot flow.
- **Miner communication**: Miners running the overlay-based quick-launch will continue to work. When they upgrade host-tools, existing overlays will be ignored (filename pattern changes) and a fresh per-VM copy will be created from the base image. This triggers a `first_boot=true` boot, which is correct.
- **Version**: `ansible/guest/VERSION` is currently `1.3.1`. This feature bumps it at release time per versioning policy.

