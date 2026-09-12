# Feature Spec: Dual Cosign Keys (Private Registry vs Docker Hub)

**Date**: 2026-05-25  
**Status**: draft

---

## Context

All cosign signature verification currently uses a single key (`/etc/admission-controller/cosign/cosign.pub`) for both the private validator image registry (`localregistry.chutes.ai:30500`, the NodePort proxy for jimages) and public Docker Hub images (`docker.io/parachutes/*`). Splitting into two keys lets the private registry key remain tightly controlled on validators while the Docker Hub key manages the public image signing lifecycle independently.

- **Packages affected**: `sek8s.validators`, `sek8s.config`
- **Key files**:
  - `src/sek8s/sek8s/config.py`
  - `src/sek8s/sek8s/validators/cosign.py`
  - `ansible/guest/roles/admission-controller/templates/cosign-registries.json.j2`
  - `ansible/guest/roles/admission-controller/templates/admission-controller.env.j2`
  - `ansible/guest/roles/admission-controller/tasks/configure-cosign.yml`
  - `ansible/guest/inventory.yml`
- **Dependencies**: cosign CLI, Ansible guest image build pipeline

---

## Design Decisions

- **Private key = localregistry only** — `cosign.pub` is used exclusively for images pulled from `localregistry.chutes.ai:30500`. It does not sign any Docker Hub image. The `*` wildcard fallback also uses this key so unknown registries require the most restrictive path.
- **Dockerhub key = public Docker Hub** — a separate `cosign-dockerhub.pub` is used for `docker.io/parachutes/*` and the dockerhub-hosted infrastructure images that require signature verification (e.g. the `parachutes/sek8s` attestation proxy image).
- **No structural change to `CosignRegistryConfig`** — the existing `public_key: Path` field per registry entry already supports arbitrary key paths. The change is purely to the values in `cosign-registries.json.j2`, not the data model.
- **`AdmissionConfig` gains one new field** — `chutes_cosign_dockerhub_public_key_path` mirrors the existing `chutes_cosign_public_key_path`. Both are needed because `_require_ctx_key` in the chutes namespace must validate that each image's configured key is one of the two known trusted keys, not any arbitrary key.
- **`_require_ctx_key` generalised to a key set** — the `ValidationContext.required_key_path: Optional[Path]` field becomes `required_key_paths: set[Path]`. The rule passes when the configured key for an image is a member of that set. This preserves the defence-in-depth check (no image can sneak through with a weaker or unknown key) while supporting two legitimate keys for the chutes namespace.
- **`ImageManager` unchanged** — `ImageManager._pull_image()` already restricts pulls to `localregistry.chutes.ai:30500` and uses `cosign_key_path` (the private key) exclusively. No change is needed.

---

## API Changes

- **New endpoints**: none
- **Schema changes**:
  - `cosign-registries.json` — `docker.io/parachutes` org and `*` wildcard entries change `public_key` from `/etc/admission-controller/cosign/cosign.pub` to `/etc/admission-controller/cosign/cosign-dockerhub.pub`
  - `AdmissionConfig` gains `chutes_cosign_dockerhub_public_key_path` (`CHUTES_COSIGN_DOCKERHUB_PUBLIC_KEY_PATH` env var, default `/etc/admission-controller/cosign/cosign-dockerhub.pub`)
  - `ValidationContext.required_key_path: Optional[Path]` replaced by `required_key_paths: set[Path]`
- **Migrations**: none. Existing single-key deployments will fail at build time if the new `cosign_dockerhub_public_key_path` inventory variable is absent — this is intentional (the key must exist before the image is built).

---

## Goal

Success = a `parachutes/*` image signed with only the dockerhub key is admitted, and a `localregistry.chutes.ai:30500/*` image signed with only the private key is admitted, while:

1. A `localregistry.chutes.ai:30500/*` image signed with the dockerhub key is rejected.
2. A `docker.io/parachutes/*` image signed with the private key is rejected.
3. An unsigned image from either registry is rejected.
4. All existing passing tests continue to pass.
5. The chutes namespace `_require_ctx_key` rule rejects any image whose configured key is not in `{cosign.pub, cosign-dockerhub.pub}`.

---

## Constraints

- Both key files must be present on the control machine at Ansible build time (`~/.cosign/cosign.pub` and `~/.cosign/cosign-dockerhub.pub`). The Ansible copy task is not `ignore_errors`; a missing key is a hard build failure.
- Key files on the guest are deployed to `/etc/admission-controller/cosign/` with `mode: 0640`, `group: admission` — same as the existing key.
- `required_key_paths` must never be empty when `_require_ctx_key` is applied; the existing `RuntimeError` guard is updated to check `not ctx.required_key_paths`.
- Do not change `ImageConfig.cosign_public_key_path` or `ImageManager`; they are private-key-only and correct as-is.
- Do not change any keyless-verified entries (`bitnami/*`, `gcr.io/distroless`) or disabled entries (`registry.k8s.io`, `nvcr.io`, `quay.io`).

---

## Output Format

1. **`ansible/guest/inventory.yml`** — add `cosign_dockerhub_public_key_path: "~/.cosign/cosign-dockerhub.pub"` variable alongside `cosign_public_key_path`

2. **`ansible/guest/roles/admission-controller/tasks/configure-cosign.yml`** — add a second `ansible.builtin.copy` task to deploy `cosign-dockerhub.pub` to `/etc/admission-controller/cosign/cosign-dockerhub.pub` (same owner/group/mode as existing key task)

3. **`ansible/guest/roles/admission-controller/templates/cosign-registries.json.j2`** — change `public_key` for `docker.io/parachutes` org and `*` wildcard to `/etc/admission-controller/cosign/cosign-dockerhub.pub`; `localregistry.chutes.ai:30500` keeps `cosign.pub`

4. **`ansible/guest/roles/admission-controller/templates/admission-controller.env.j2`** — add `CHUTES_COSIGN_DOCKERHUB_PUBLIC_KEY_PATH=/etc/admission-controller/cosign/cosign-dockerhub.pub`

5. **`src/sek8s/sek8s/config.py`** — add `chutes_cosign_dockerhub_public_key_path: Optional[Path]` field to `AdmissionConfig`

6. **`src/sek8s/sek8s/validators/cosign.py`** — replace `required_key_path: Optional[Path]` with `required_key_paths: set[Path]` on `ValidationContext`; update `_get_rules_for_context` to populate the set from both config fields; update `_require_ctx_key` to check membership

7. **`tests/`** — update any existing tests that set `required_key_path` directly; add unit tests covering: (a) localregistry image admitted with private key, (b) parachutes image admitted with dockerhub key, (c) each image rejected when signed with the wrong key, (d) `_require_ctx_key` raises when `required_key_paths` is empty

---

## Failure Conditions

- A `localregistry.chutes.ai:30500/*` image signed with the dockerhub key is admitted.
- A `docker.io/parachutes/*` image signed with the private key is admitted.
- `_require_ctx_key` passes when `required_key_paths` is empty (missing guard).
- `cosign-registries.json` `localregistry` entry is changed to use the dockerhub key.
- `ImageConfig.cosign_public_key_path` or `ImageManager` is modified.
- Any keyless-verified or disabled registry entry is changed.
- Admission controller fails to start because `cosign-dockerhub.pub` does not exist on the guest (must be caught at Ansible build time, not at runtime).

---

## Rollout Notes

- Generate a new cosign key pair for Docker Hub signing: `cosign generate-key-pair` and place the public key at `~/.cosign/cosign-dockerhub.pub` on the control machine before running the Ansible build.
- Re-sign all current `docker.io/parachutes/*` images with the new dockerhub key before deploying the updated guest image to production.
- The private key (`cosign.pub`) continues to sign all jimages pushed to the validator's local registry. No change to the signing side of the localregistry workflow.
- This is a **hard cut-over** on guest image build — old guests using the single key continue to work until rebuilt. Ensure all active miners rebuild within the same deployment window.
- Changelog fragment goes in `changelogs/vm/unreleased/<branch-name>.md` under `### Changed`.
