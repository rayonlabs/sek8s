# Feature Spec: Docker Hub Credential Support for Cosign and Containerd

**Date**: 2026-03-24  
**Status**: draft

---

## Context

Cosign image signature verification and containerd image pulls both hit Docker Hub anonymously, sharing a single 100-pull/6-hour per-IP rate limit. At VM boot, the combination of containerd pulling workload images and cosign verifying signatures can exhaust this quota — especially after restarts during debugging or when the single-flight dedup fix (separate PR) is not yet in place. Once Docker Hub returns `TOOMANYREQUESTS`, cosign enters a 300-second global backoff and all admission is blocked.

Authenticated Docker Hub users get at least 200 pulls/6hrs (free tier) and significantly more on paid tiers. This feature adds optional Docker Hub credentials to the miner config pipeline so both containerd and cosign authenticate, avoiding the anonymous rate limit entirely.

The config volume (`/var/config`) is an **untrusted input boundary** — the miner provides the credentials on the host, and the guest must treat them as potentially adversarial. On the **guest**, credentials must never be interpolated into shell commands or subprocess arguments. On the **host**, passing username/token as optional positional arguments into `create-config.sh` is acceptable: the same values are stored as **cleartext files on the config volume**, so anyone who can mount that volume can read them anyway; the primary threat model is **guest-side injection** into generated files (see **Threat model**).

- **Packages affected**: `host-tools/scripts`, `ansible/guest/roles/config`, `ansible/guest/roles/common`, `ansible/guest/roles/admission-controller`, `ansible/guest/roles/system-manager`, `sek8s/cosign`
- **Key files**:
  - `src/chutes-cvm/chutes_cvm/scripts/config/config.tmpl.yaml` — user-facing config template
  - `host-tools/scripts/config/config-schema.json` — JSON Schema for config validation
  - `src/chutes-cvm/chutes_cvm/guest/config.py` — YAML parser, emits shell vars
  - `host-tools/scripts/quick-launch.sh` — orchestrates VM launch, calls `create-config.sh`
  - `src/chutes-cvm/chutes_cvm/scripts/volumes/create-config.sh` — creates and populates config volume
  - `ansible/guest/roles/config/files/process-config.py` — guest-side config validator and applier (already uses PyYAML)
  - `ansible/guest/roles/common/templates/registries.yaml.j2` — containerd registry config (k3s); Ansible content preserved at runtime
  - `sek8s/cosign/client.py` — cosign subprocess runner (`_COSIGN_ENV`); should follow systemd `DOCKER_CONFIG`, not duplicate path logic
  - `ansible/guest/roles/admission-controller/files/admission-controller.service` — systemd unit; ordering vs `config-manager`
  - Shared **systemd drop-in** (implementation choice: e.g. under `admission-controller.service.d/` and `system-manager.service.d/`, or a shared snippet included by both) setting `DOCKER_CONFIG`
  - `ansible/guest/roles/system-manager/files/system-manager.service` — must receive the same `DOCKER_CONFIG` as admission
- **Dependencies**: Depends on (but does not block) the single-flight cosign dedup fix. Both reduce Docker Hub rate limit pressure independently.

---

## Threat model

- **Secondary concern**: A local attacker on the guest “stealing” credentials. The miner intentionally supplies credentials; the VM is otherwise locked down. Secrecy against someone who already has root on the box is not the main design driver.
- **Primary concern**: **Untrusted strings** from the config volume must not **inject** or corrupt **`config.json`** or **`registries.yaml`** in ways that cause unsafe behavior — e.g. shell execution, unsafe YAML constructs, parser confusion, or broken JSON/YAML structure.
- **Enforcement (guest)**:
  - **No** shell interpolation and **no** raw username/token on **guest** subprocess argv.
  - **`config.json`**: build only with **`json.dumps`** from a small dict and the computed base64 `auth` value — no format-string templates filled from untrusted input.
  - **`registries.yaml`**: read with **`yaml.safe_load`**, write with **`yaml.safe_dump`** (or equivalent safe round-trip); change only **known keys** under fixed paths (e.g. `configs` entries for Docker Hub). Never append untrusted bytes as a raw tail; never use unsafe `yaml.load`.
  - **Credential strings**: enforce **non-empty**, **bounded length** (implementation: username ≤ **64**, token/password ≤ **128** chars after strip), reject **NUL and C0 control characters** (and optionally strip surrounding whitespace). Goal is **parser-safe and serialization-safe**, not matching Docker’s undocumented PAT character set. See [Docker Hub access tokens](https://docs.docker.com/docker-hub/access-tokens/) for miner guidance (prefer read-only PATs).
  - **`registries.yaml` caveat**: Hub `username` / `password` appear as YAML string fields (not base64 there). Safety comes from **no shell**, **safe emit**, and **control-free bounded strings**.
- **Fail open**: If validation fails, fall back to anonymous Hub pulls and log a warning — do not block boot.

---

## Design Decisions

### Service ordering (Q1)

- **`config-manager.service`** (runs `process-config.py`) must finish **before** units that need Docker Hub auth for cosign/containerd consume the generated files.
- **`config-manager.service`** must run **`After=setup-storage-bind-mounts.service`** (and **`Requires=setup-storage-bind-mounts.service`** on images that use the storage bind mount) so `process-config.py` merges Docker Hub auth into **`/etc/rancher/k3s/registries.yaml` after** that path is bind-mounted to persistent storage. If config-manager runs **before** the bind mount, a common failure mode is: `setup-storage-bind-mounts` copies an unmerged file to storage, then `process-config` updates only the **rootfs** copy, then the bind mount hides that with the **unmerged** storage file — containerd then pulls Hub anonymously (`429 TOOMANYREQUESTS`) even though a later `cat` of the bind-mounted file can look correct if credentials were fixed manually afterward.
- **Spec**: cosign-consuming systemd units (at minimum **`admission-controller.service`**, and any other unit that runs cosign against Hub) declare **`After=config-manager.service`** (and ordering that ensures `process-config` has run successfully for the one-shot config flow).
- **No hot-reload**: Credentials and merged `registries.yaml` are not expected to change for the lifetime of a running VM without a reboot or explicit re-run of the config flow. Relying on environment and files as they exist at service start is acceptable.

### Shared `DOCKER_CONFIG` for admission and system-manager (Q2, Q6, Q7)

- **One directory**: `/etc/admission-controller/docker-config` with `config.json` inside.
- **Both consumers**: **`admission-controller.service`** and **`system-manager.service`** get **`Environment=DOCKER_CONFIG=/etc/admission-controller/docker-config`** from a **shared systemd drop-in** (or equivalent), not admission-only.
- **Always set**: `DOCKER_CONFIG` is set **even when Hub creds are absent or invalid** — do not conditionally omit it in systemd.
- **Permissions (Q7)**: Directory **`0750`**, **`root:admission`**. File **`config.json`** **`0640`**, **`root:admission`**. **`User=admission`** reads via primary group **`admission`**. **`User=system-manager`** reads via **supplemental `admission`** (already configured in Ansible for cosign key sharing). No new Unix group required. Never world-readable.

### `config.json` when creds missing or invalid (Q6)

- **`process-config.py`** always maintains **`config.json`**: with valid Hub creds, write full Docker auth for `https://index.docker.io/v1/` (standard format). With **missing or invalid** creds, write **`{"auths": {}}`** (via `json.dumps`) so the file always exists and **stale Hub auth is never left on disk**.

### `registries.yaml` merge (Q4)

- **Do not** “append” raw YAML snippets. Use **`yaml.safe_load`** → update **only** the fixed Docker Hub **`configs.<host>`** keys (and nested `auth`) → **`yaml.safe_dump`**, preserving everything else (e.g. Ansible **`mirrors`** and other **`configs`** entries such as Chutes/local registry). See [K3s private registry configuration](https://docs.k3s.io/installation/private-registry).
- **Strip on failure**: If credential files are missing or invalid, **remove** those same Hub `configs` keys so the node uses **anonymous** pulls (no stale PAT in the file).
- **Host keys**: Set auth for **`docker.io`**, **`registry-1.docker.io`**, and **`index.docker.io`** with the same credentials (manifest GETs often go to `registry-1.docker.io`; containerd host matching varies by version).
- **k3s**: Registry file changes may require a k3s restart per upstream docs; first boot after image install should order `config-manager` before k3s consumes the file. Mid-life updates are out of scope for the static miner VM.

### Host pipeline (Q5)

- **Inputs**: Optional **`docker_hub`** in miner **`config.yaml`** **and** **`quick-launch.sh`** flags **`--docker-hub-username`** / **`--docker-hub-token`**. When both are set, **CLI overrides YAML**.
- **Transport**: Optional **8th/9th positional args** to **`create-config.sh`** remain acceptable. Host **`ps`** visibility is a minor concern versus **cleartext files on the config volume**.

### Cosign client (`sek8s/cosign/client.py`)

- **Systemd is source of truth** for `DOCKER_CONFIG`. Prefer inheriting **`os.environ`** as set by the unit. **Avoid** duplicating “if directory exists, set `DOCKER_CONFIG`” logic in Python if it conflicts with systemd (Q1/Q6).

### Other

- **Credentials are optional**: If `docker_hub` is absent, behavior matches today’s anonymous pulls except that **`config.json`** still exists with empty `auths` and **`DOCKER_CONFIG`** is still set.
- **Standard Docker auth format** for `config.json`: `{"auths": {"https://index.docker.io/v1/": {"auth": "<base64(user:token)>"}}}`.
- **Two consumers, one source**: `process-config.py` reads credential files once and writes **`config.json`** and merged **`registries.yaml`**.

---

## API Changes

- **New endpoints**: None
- **Schema changes**: New optional `docker_hub` section in `config-schema.json` and `config.tmpl.yaml`
- **Migrations**: None (purely additive; existing configs without `docker_hub` continue to work)

---

## Goal

Success = A miner who provides Docker Hub credentials has both containerd (k3s) and cosign authenticate to Docker Hub where applicable, avoiding anonymous rate limits when credentials are valid. Specifically:

1. A config with valid `docker_hub.username` and `docker_hub.token` results in authenticated Docker Hub usage (verify via Hub rate-limit headers / tier).
2. A config without `docker_hub` boots normally with anonymous Hub pulls (**backward compatible**).
3. Malformed credentials (per **safe string** rules) fall back to anonymous with a warning — **does not block boot**.
4. On the **guest**, credential **values** do not appear in `ps`, journal, or `/proc/*/cmdline` for cosign/systemd — only paths such as **`DOCKER_CONFIG`**.
5. **`config.json`**: **`0640` `root:admission`**, directory **`0750` `root:admission`**; **`system-manager`** can read the same file as **`admission`** (supplemental **`admission`**). Not world-readable.
6. **`registries.yaml`** after merge contains no stale Hub auth when creds are missing/invalid; non-Hub content from Ansible is preserved.

---

## Constraints

- **Guest**: Credentials must never be interpolated into shell commands, subprocess arguments, or executable strings. **Structured file I/O only** (`json.dumps`, `yaml.safe_load` / `yaml.safe_dump` on known subtrees).
- **Guest validation**: **Safe string** rules (non-empty, max length **64** / **128** for username / token, no NUL/C0 controls, optional strip) — **not** a strict alphanumeric-only PAT regex.
- The config volume is untrusted input. Treat all values read from `/var/config/` as adversarial at the guest boundary.
- **`create-config.sh`**: Write credential files with redirection to files only; do not pass values to external commands as arguments where avoidable (values still may appear in host argv per Q5 — document for operators).
- **`DOCKER_CONFIG`** holds a **directory path** only.
- **Guest Python**: Use stdlib **`json`** and existing **`yaml`** (PyYAML) already used by `process-config.py` — **no new dependencies** without separate discussion.
- Must not break existing miners who have no `docker_hub` section.

---

## Output Format

1. **Modified: `host-tools/scripts/config/config.tmpl.yaml`**
   - Add optional `docker_hub` section (document PAT link in comments/help).
2. **Modified: `host-tools/scripts/config/config-schema.json`**
   - Add `docker_hub` to schema properties (not in `required`).
3. **Modified: `src/chutes-cvm/chutes_cvm/guest/config.py`**
   - Parse `docker_hub.username` and `docker_hub.token` from config.
   - Emit `DOCKER_HUB_USERNAME` and `DOCKER_HUB_TOKEN` shell vars via `shlex.quote` (defense in depth for host scripts).
4. **Modified: `host-tools/scripts/quick-launch.sh`**
   - Accept `--docker-hub-username` / `--docker-hub-token`.
   - **Precedence**: CLI overrides `config.yaml` when both set.
   - Pass credentials as optional 8th/9th positional args to `create-config.sh`.
5. **Modified: `src/chutes-cvm/chutes_cvm/scripts/volumes/create-config.sh`**
   - Accept optional Docker Hub username/token args.
   - Write `docker-hub-username` and `docker-hub-token` on the config volume (mode **0600**).
6. **Modified: `ansible/guest/roles/config/files/process-config.py`**
   - Read `/var/config/docker-hub-username` and `/var/config/docker-hub-token` (treat missing as no creds).
   - Validate with **safe string** rules (see **Threat model** / **Constraints**).
   - **Always** write `/etc/admission-controller/docker-config/config.json`: either full Hub auth or `{"auths": {}}`; mode **0640**, **root:admission**. Never leave stale Hub entries when creds are invalid or removed.
   - **`registries.yaml`**: **`yaml.safe_load`** → merge/strip **only** fixed Hub `configs` keys → **`yaml.safe_dump`**; preserve mirrors and other registry configs from Ansible.
7. **Modified: `ansible/guest/roles/common/templates/registries.yaml.j2`** (optional)
   - Prefer a **comment** documenting that Docker Hub auth is applied at runtime by `process-config.py` (no secrets baked into the image).
8. **Modified: `sek8s/cosign/client.py`**
   - **`_COSIGN_ENV`**: Rely on **`DOCKER_CONFIG`** from the process environment (systemd). Remove or narrow redundant “set `DOCKER_CONFIG` if directory exists” logic if it duplicates systemd.
9. **Modified: systemd units / drop-ins**
   - **`admission-controller.service`**: **`After=config-manager.service`** (plus any existing ordering).
   - **Shared drop-in** for **`admission-controller.service`** and **`system-manager.service`**: **`Environment=DOCKER_CONFIG=/etc/admission-controller/docker-config`**.
10. **Modified: Ansible (image layout)**
    - Ensure `/etc/admission-controller/docker-config` exists at build time (**0750**, **root:admission**), e.g. in admission-controller or config role tasks; align with **`configure-cosign.yml`** or equivalent.

---

## Failure Conditions

- Guest passes credentials as subprocess arguments or shell expansions (except documented host pipeline per Q5).
- Injection or unsafe YAML/JSON generation from untrusted config-volume strings (e.g. unsafe `yaml.load`, uncontrolled append to `registries.yaml`, format-string-built `config.json`).
- Malformed credentials **halt boot** instead of falling back to anonymous.
- **Guest**: Credential **values** appear in journal, `ps`, or `/proc/*/cmdline` for cosign-related services.
- **`system-manager`** cannot read **`config.json`** while **`admission`** can (permission regression), or either file is world-readable.
- **`config.json`** or merged **`registries.yaml`** retains Hub auth when creds are missing or invalid (**stale auth**).
- Existing miners without `docker_hub` fail to boot (**backward compatibility** regression).
- **`DOCKER_CONFIG`** env var contains credential material instead of a directory path.

---

## Rollout Notes

- **Backward compatible**: `docker_hub` is optional in schema and code paths.
- **Miner action**: To use auth, add `docker_hub.username` and `docker_hub.token` to `config.yaml` and re-create the config volume / re-run `quick-launch.sh`.
- **Existing config volumes**: Without `docker-hub-*` files, guest writes empty `auths` and strips Hub keys from `registries.yaml` merge target behavior.
- **Tokens**: Prefer Docker Hub [access tokens](https://docs.docker.com/docker-hub/access-tokens/) with **Read-only** scope; avoid account passwords in config.
- **Rate limits**: Free tier 200 pulls/6h authenticated vs 100 anonymous; paid tiers higher. Complements single-flight dedup (separate change).
- **Config volume**: Normally re-created by `quick-launch.sh`; cleartext credential files on the volume remain the main host-side exposure.
