# System Status Service

## Purpose
The System Status service is a read-only FastAPI endpoint that runs inside the guest VM to expose operational state from a tightly scoped set of systemd units and NVIDIA GPU telemetry commands. It is intended to de-risk the "black-box" nature of the VM by providing authenticated components (admission controller, attestation proxy, etc.) with structured status information without granting shell access or generic command execution capabilities.

## Functional Scope

| Capability | Description |
| --- | --- |
| Service inventory | Enumerate the fixed allowlist of managed systemd units: the long-running sek8s services (admission controller, **OPA**, **system manager**, attestation service, **chute log shipper**), k3s server, `storage-bind-mounts`, and the GPU/fabric units (`nvidia-persistenced`, `nvidia-fabricmanager`, `infiniband-config`). |
| Service status | Return summarized health derived from `systemctl show` for an allowlisted unit. |
| Service logs | Tail the latest N log lines (`journalctl -u <unit>`) with optional time window filtering. |
| GPU telemetry | Surface `nvidia-smi` output in either default (summary) or `-q` (detailed) modes with optional GPU index selection. |
| Overview summary | Aggregate all service statuses with the latest `nvidia-smi` result to produce an "ok"/"degraded" snapshot. |

Future enhancements (e.g., additional units) must be added explicitly to the allowlist to avoid broadening the attack surface.

Because the prod VM has no console or SSH access, this allowlist is the **only** operator/tooling view of a
unit — and it is also the **only** way journal content leaves the guest. That makes the allowlist a
confidentiality boundary, not just a convenience:

- **The miner is the party this guest is confidential *from*.** Miner-authenticated calls to
  `/services/{id}/logs` are how the miner CLI reads guest journals, so anything a unit logs is effectively
  published to the host operator. A unit qualifies only when its journal is free of **tenant/validator**
  material: chute log content, admission review objects (pod specs, env, mounts), key material.
- **Secondarily, the validator reads the same endpoints**, so units handling the *miner's own* credentials
  stay off the list too — not to protect them from the miner, but to keep `MINER_SEED` away from the validator.
  This is why `config-manager.service` (config-volume credential handling) is excluded.

`opa.service` is on the list, but only because `decision_logs` is off by default (see
`ansible/guest/roles/admission-controller/defaults/main.yml`). A build with `-e opa_decision_logs=true`
puts full AdmissionReview inputs in that journal and therefore behind this endpoint — acceptable for a debug
build (which already has console access), never for prod.

**Traceback hygiene.** Loguru's default `diagnose=True` renders frame-local *values* into exception
tracebacks, which on an error path would put request data into an allowlisted journal. The admission path
(`services/admission_controller.py`, `validators/`, `clients/cosign.py`) uses stdlib `logging`, which never
renders locals, so it was never exposed — but the guest mixes both libraries, so every service entrypoint now
calls `sek8s_common.log_config.configure_logging()` to install a `diagnose=False` sink. That makes the property
hold for the whole process regardless of which library a future call site reaches for. Only values are
suppressed — `backtrace` stays on, so tracebacks still carry every frame, line, and source line, including the
call chain above the catching point. The chute log shipper is
independently clear: it never calls `logger.exception`, and every one of its log statements carries only
`config_id`, pod name, counts, and status codes — never log content.

## API Surface

All responses are JSON and delivered over HTTPS or a Unix Domain Socket based on standard `ServerConfig` parameters.

By default the Ansible role configures the service to listen on `0.0.0.0:8080` inside the guest over HTTP. TLS variables are left blank so operators can supply their own certificates later if needed. The host bridge script forwards TCP/8080 so the status API is reachable from the host network without extra tunneling.

- `GET /health`
  - Returns `{"status": "ok"}` when the service is responsive.
- `GET /services`
  - Lists the static allowlist: service id, systemd unit name, description.
- `GET /services/{service_id}/status`
  - Summarizes `LoadState`, `ActiveState`, `SubState`, `MainPID`, and recent exit code harvested from `systemctl show`.
- `GET /services/{service_id}/logs?lines=200&since_minutes=60`
  - Streams log lines from `journalctl -u <unit>`.
  - `lines` defaults to 200 and is clamped to [1, 1000].
  - `since_minutes` (optional) truncates the log window to the last N minutes (1–1440). When omitted, only the latest `lines` are returned.
- `GET /gpu/nvidia-smi?detail=false&gpu=all`
  - Executes `nvidia-smi`.
  - `detail=true` swaps the command to `nvidia-smi -q`.
  - `gpu` can be `all` (default) or an integer GPU index; only a single index is accepted to keep the interface deterministic.
  - Output is returned as `{ "stdout": "...", "stdout_lines": ["line1", ...], "stderr": "...", "exit_code": <int> }`, making it easier for clients to render the text banner without reprocessing newline escapes.
- `GET /overview`
  - Collects the status for every allowlisted service plus a default `nvidia-smi` invocation.
  - Returns `{ "status": "ok" | "degraded", "services": [...], "gpu": {...}, "timestamp": "ISO-8601" }`.
  - The `status` is `ok` only when every service is loaded/active and `nvidia-smi` exits successfully; otherwise it degrades.

All other paths return 404.

## Security Model

1. **Read-only execution**
   - Only `systemctl show`, `journalctl -u`, and `nvidia-smi` commands are ever issued. Parameterization is handled server-side through validated inputs (service ids, bounded integers, boolean flags).
   - `subprocess` calls are made with `shell=False`, preventing shell interpolation or arbitrary redirection.
   - Each command has a strict timeout (default 10 seconds) and the stdout/stderr is size-limited before returning to the caller.

2. **Allowlist enforcement**
   - Service ids are resolved against a hard-coded dictionary mapping to systemd unit names (`admission-controller.service`, `system-manager.service`, `attestation-service.service`, `k3s.service`, etc.). Requests for unknown ids fail with HTTP 404.
   - GPU command options are derived from boolean and integer query parameters; textual arguments are never concatenated into the command line.

3. **Principle of least privilege**
   - The systemd unit runs as a dedicated `chutes` user (non-privileged account) with membership in the `systemd-journal` and `video` groups. It does not require root and is fully confined via a drop-in (`ProtectSystem=strict`, `NoNewPrivileges=true`, etc.).
  - Application directories live under `/opt/sek8s` with read-only permissions for service users. Device sandboxing is tightened via `DevicePolicy=closed` while explicitly allowing the NVIDIA control/uvm nodes plus `/dev/nvidia[0-9]*` and `/dev/nvidia-caps/nvidia-cap*` so `nvidia-smi` can talk to every GPU without exposing unrelated devices.

4. **Transport security**
  - The service reuses the existing `ServerConfig` foundation: TLS can be enabled by providing certificate/key paths, but by default the bridged deployment runs plain HTTP on the isolated host-only network. UDS deployments inherit filesystem ACLs. Future authentication layers (shared secret, mTLS) can be added when consumers require it.

5. **Operational safeguards**
   - Log and command outputs are truncated (configurable, default 16 KiB) to minimize potential sensitive data exposure.
   - Errors returned to clients omit raw stderr to avoid leaking host paths or kernel details; instead a structured error payload describes the failure mode (timeout, exit code, etc.).

## Open Questions / Next Steps

- Determine the final authentication story (e.g., reuse validator signature headers similar to the attestation proxy or rely on mTLS). The initial implementation focuses on the read-only execution layer; transport-level protections can be layered in once the consuming component is chosen.
- Remaining un-exposed units, should a triage gap show up in practice (all secret-free journals):
  `registry-tls-config`, `verify-apparmor-profiles`, `gpu-verify`, `rtmr3-verify`,
  `setup-cache` / `verify-cache-volume` / `verify-storage`, `attestation-service-init`. These are boot one-shots
  whose failure already surfaces through the long-running service that depends on them, so they are deliberately
  left off rather than widening the endpoint. `config-manager` is excluded by the credential rule above.
- Consider Prometheus metrics (command success/failure counts) if observability gaps appear.
