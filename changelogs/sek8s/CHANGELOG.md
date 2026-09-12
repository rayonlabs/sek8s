# Changelog

All notable changes to the `sek8s` package will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

Version source of truth: `src/sek8s/VERSION`

> **Note:** Prior to 0.2.5, the sek8s package and VM image shared a single version
> and codebase. Entries below 0.2.5 reflect service-level changes from that era.

## [0.4.0] - 2026-09-10

### Added
- `WebServer.serve()` (async) in `sek8s-common`, alongside `run()` (blocking).
  Both derive their uvicorn arguments from a single `_uvicorn_kwargs()` source of
  truth, so every server honours its full TLS/mTLS/bind config regardless of how
  it is hosted (single-server process via `run()`, or several servers sharing one
  event loop via `serve()`).
- **Chute log shipper agent (`sek8s.log_shipper`, Phase 1).** New headless asyncio package
  (`config`, `crictl`, `checkpoint`, `shipper`, `agent`, `exceptions`) with a `chute-log-shipper`
  console entry, closing the gap where a chute crashing before instance registration left its logs
  unreachable. No new dependencies (reuses `aiohttp` + the `run_command` shell pattern; no k8s API
  access). It discovers chute pods via the CRI socket (`k3s crictl pods -o json`, through a
  restricted wrapper), reads their logs off `/var/log/pods`, and streams them to the validator over
  the per-boot CVM mTLS leaf.
  - **Streaming read path (deterministic memory).** A single coroutine per pod tails only *new*
    bytes from a bounded `buffer_bytes` window (byte offset per log file keyed by inode, so it
    follows kubelet rotation; reset on truncation), rather than re-reading whole files. Memory is
    bounded to `buffer_bytes × pods` (≤ 1 chute pod per GPU); a slow validator pauses reading
    (backpressure); only complete logical lines are shipped (window-cut lines and CRI `P`-runs are
    held); the shipped offset is committed on success and persisted to a `{config_id → {inode →
    offset}}` checkpoint for restart resume. No wall-clock backstop — termination is the validator's
    job (`204`).
  - **Only the `chute` container is captured** (`CONTAINER_NAME`; admission-enforced name);
    init/sidecar containers are skipped so the stream stays single-container and monotonic in `ts`,
    which the validator's high-watermark dedupe relies on.
  - **Wire contract:** `POST https://cvm.chutes.ai/instances/launch_config/{config_id}/logs` with a
    body of `{"deployment_id": "<uuid>", "logs": [{ts, stream, log}]}`. Nothing security-relevant is
    self-asserted — identity is derived validator-side from the mTLS leaf + path + proxy;
    `deployment_id` (from the `chutes/deployment-id` pod label) is the sole top-level field. `204` =
    validator terminated (stop); other 2xx = keep sending; `403`/`404` = rejected (stop + log the
    reason); `413` = payload too large → split the batch and retry the halves (and shrink the batch
    ceiling); any other non-2xx / connection error = transient retry with backoff. No `seq` is sent.
- System status `/services` allowlist now covers `chute-log-shipper` and `opa`. The prod VM has no console
  or SSH access, so an unlisted unit cannot be status-checked or log-tailed by the miner CLI at all.

### Changed
- Split cosign signature verification into two keys: `chutes.pub` for the private localregistry (and wildcard fallback), `dockerhub.pub` for Docker Hub `parachutes/*` images
- `AdmissionConfig`: replaced `chutes_cosign_public_key_path` (`CHUTES_COSIGN_PUBLIC_KEY_PATH`) with `chutes_public_key_path` (`CHUTES_PUBLIC_KEY_PATH`) and new `dockerhub_public_key_path` (`DOCKERHUB_PUBLIC_KEY_PATH`)
- `ValidationContext.required_key_path: Optional[Path]` replaced by `required_key_paths: set[Path]`; `_require_ctx_key` now validates against set membership rather than a single path
- `AdmissionConfig.authz_allowed_log_prefixes` default updated: `"registry-"` replaced with `"chutes-registry-"` to match the new Helm chart Service/Deployment name (`chutes-registry`) introduced in `chutes-miner-gpu` v0.3.0. The old prefix would silently deny miner log access to the registry pod after the chart upgrade.
- `AdmissionConfig.chutes_cosign_public_key_path` (single key) replaced with two separate fields: `chutes_public_key_path` (env `CHUTES_PUBLIC_KEY_PATH`, default `/etc/admission-controller/cosign/chutes.pub`) for localregistry-signed images, and `dockerhub_public_key_path` (env `DOCKERHUB_PUBLIC_KEY_PATH`, default `/etc/admission-controller/cosign/dockerhub.pub`) for Docker Hub-signed images.
- `ValidationContext.required_key_path: Optional[Path]` replaced with `required_key_paths: Set[Path]` — the chutes namespace now accepts images signed by either the localregistry key or the Docker Hub key, eliminating false rejections when images are dual-signed or sourced from different registries.
- `ImageConfig.image_pull_allowed_registries` default changed from `["localhost:30500", "127.0.0.1:30500"]` to `["localregistry.chutes.ai:30500"]` to match the static registry hostname decoupled from the validator hotkey.
- `resolve_to_full_ref` registry-matching predicate updated from `.localregistry.chutes.ai` (dot-prefix, validator-scoped) to `localregistry.chutes.ai` (bare hostname) to reflect the static registry change.
- `AdmissionConfig.chutes_public_key_path` default updated from `/etc/admission-controller/cosign/chutes.pub` to `/run/chutes/signing-keys/cosign/chutes.pub`.
- `AdmissionConfig.dockerhub_public_key_path` default updated from `/etc/admission-controller/cosign/dockerhub.pub` to `/run/chutes/signing-keys/cosign/dockerhub.pub`.
- Registry defaults moved from `localregistry.chutes.ai:30500` to
  `registry.chutes.ai`: `ImageConfig.image_pull_allowed_registries`,
  `AdmissionConfig.allowed_registries`, and the `chutes_public_key_path`
  description in `config.py`.
- `resolve_to_full_ref` (`system_manager/images/util.py`) resolves short-form
  image refs against `registry.chutes.ai` and drops the now-unused
  `localhost` / `127.0.0.1` special-casing for full-ref detection.
- `AdmissionConfig` cosign key documentation updated to reflect that the
  dynamically-fetched cosign keys are now RSA-verified (not PGP-verified)
  against the attested root key before being written to tmpfs. Key paths and
  behavior are unchanged.
- Guest services now install a loguru sink with `diagnose=False`
  (`sek8s_common.log_config.configure_logging`, called from each service entrypoint). Loguru's default
  renders frame-local *values* into exception tracebacks, which on an error path could write request data
  into a journal the miner can read over the status API. Tracebacks are otherwise unchanged — `backtrace`
  stays on, so every frame, line, and source line is still logged.
- Rebuilt the chute log shipper around three parts with one job each: an agent that watches
  for pods, a shipper per pod that ships until the validator says stop, and a reader that
  turns log files into batches. Reading and shipping were previously interleaved in one
  class, which is how two defects hid in it.
- **Config**: `BUFFER_BYTES` is retired. `BATCH_MAX_BYTES` now bounds both the batch and the
  per-pod memory ceiling, because only one batch is held at a time. Existing images are
  unaffected — unknown settings are ignored — but the setting no longer does anything.

### Fixed
- The TDX quote provider now validates the supplied nonce (exactly 64 hex characters) and asserts
  the assembled REPORTDATA is exactly 128 hex characters, instead of truncating it. A malformed
  nonce is rejected with HTTP 400 rather than producing a quote.
- The log shipper now checks the pod labels it reads before using them. One of them becomes
  part of the address it sends logs to, and the guest signs that request with the identity
  proving it is a genuine confidential VM — so a label containing a path separator would have
  redirected an authenticated request somewhere it was never meant to go. Kubernetes already
  rejects such labels, which is why this was not reachable; the shipper now rejects them too
  rather than relying on a component two layers away. Pods with unusable labels are skipped
  and their siblings keep shipping.
- The log shipper no longer writes exception messages into its journal. That journal is
  readable by the miner through the status API, and the service handles chute log output —
  so an exception that quoted the value that caused it would hand tenant data to the
  operator it is kept from. Failures now report the error's type and location instead. The
  parser that turns raw bytes into log lines is also now covered by tests asserting it never
  raises, since that is what makes the rest of this safe.
- The system manager now installs the hardened log sink at startup. Its own journal is
  readable by the miner, and it was the one service still running on the default handler —
  which renders local variable values inside tracebacks. Nothing was exposing values in
  practice, but the next error path added to that service would have.

### Removed
- system-manager's `ImageManager` no longer pulls images. Removed the cosign-verified pull
  path (`start_pull` / pull-status tracking, `PullStatusEnum` / `PullSnapshot`), the
  `COSIGN_PUBLIC_KEY_PATH` (`cosign_public_key_path`) setting, and the `CosignClient`
  dependency. It now only lists, deletes, and prunes containerd images; image signature
  verification stays with the admission controller.

## [0.3.1] - 2026-06-20

### Added
- Retry logic in `download.py` for transient 403 errors caused by presigned CDN URL expiration on large model downloads (up to 5 retries with exponential backoff)
- Presigned-URL credential redaction in model-download error logs (`_redact_urls`): URL query strings (e.g. `?X-Amz-Signature=…`) are replaced with `?<redacted>` before exception text is written to stderr/the journal, so short-lived CDN credentials never get logged.

### Changed
- Upgraded `huggingface_hub` from 0.36.2 to ^1.18.0; removed deprecated `hf-transfer` dependency
- System manager downloads now use throttled XET (`HF_XET_FIXED_DOWNLOAD_CONCURRENCY=16`, `TOKIO_WORKER_THREADS=8`) instead of disabled XET with httpx fallback — benchmarked at ~500 MB/s vs ~22 MB/s in TDX
- Model-download retry classifier now keys on typed exceptions instead of substring matching. Replaced the old `"403"/"Forbidden" in str(exc)` check in `download.py` with `_is_transient_download_error`, which retries only genuine transient download-layer failures (CDN presigned-URL expiry → HTTP 403 on the file GET, and XET transport hiccups) and immediately raises hard auth/availability errors (gated repo, bad/expired token → 401, missing repo/revision → 404, XET auth). This avoids burning 5 retries (~150s) on a permanent failure and avoids treating any message that merely contains "403" as transient.
- Removed internal audit finding-ID references (`SEK8S-NNN`) from public-bound source comments/docstrings; the finding↔test mapping now lives only in the sensitive audit doc, enforced by a new leak-guard test.
- System-manager cache delete now routes its privileged remove through the path-restricted `/usr/local/bin/cache-rm` wrapper instead of `sudo rm`, and `HuggingFaceSnapshot.delete()` gained an in-code guard that refuses any target that isn't a direct child of the HF cache base (defense in depth alongside the wrapper and the router's UUID validation).

### Fixed
- System-manager cache delete (`DELETE /cache/{chute_id}`) returned HTTP 500 when a chute pod had downloaded model files directly into the shared cache: the partial blobs are owned by the pod (uid 1000) and not group-writable, so `shutil.rmtree` fails to unlink them. The privileged-remove fallback in `HuggingFaceSnapshot.delete()` only triggered on `EPERM` (errno 1), but the real failure is `EACCES` (errno 13); both are now handled.
- Re-downloading a chute over a pod-owned partial failed mid-way with `PermissionError [Errno 13]` because the download subprocess cannot write a tmp blob into a pod-owned, non-group-writable directory (and cannot resume it). `start_download` now detects a tree containing entries owned by another process (`_has_foreign_entries`) and, **only when `force=true`**, clears it first. Without `force` the cache download endpoint returns HTTP 409 instead of silently discarding the partial — an INCOMPLETE pod-owned tree is indistinguishable from one a chute pod is still actively downloading.

## [0.3.0] - 2026-05-15

### Added
- `POST /cache/{chute_id}/cancel` endpoint to cancel an in-progress HuggingFace model download, with an optional `cleanup` query parameter to delete partial files from disk after cancelling.
- `POST /cache/purge` endpoint to purge stale HuggingFace revisions from all tracked chutes without evicting any chutes. Returns `purged_bytes` freed. Useful for reclaiming orphaned blobs on a schedule independently from age/size-based cleanup.
- Stale HF revision purging during cache cleanup: after removing chutes that exceed age/size limits, surviving chutes have orphaned revisions pruned via the HuggingFace `delete_revisions` API. Bytes freed by purging are reported separately as `purged_bytes` in the cleanup response.
- `DownloadProcess` class (`cache/download.py`) isolates each model download in a subprocess (`-m sek8s.system_manager.cache.download`), providing clean state properties (`is_running`, `is_done`, `succeeded`, `was_cancelled`, `error`) and SIGTERM-based cancellation without leaking asyncio tasks into the manager.
- `chmod_tree` utility for recursively setting permissions on a cache directory tree (used by the download subprocess pipeline).

### Changed
- `HuggingFaceSnapshot` download state is now tracked via `DownloadProcess` instead of a raw `asyncio.Task`, decoupling download lifecycle management from the manager.
- `CacheCleanupResponse` now includes a `purged_bytes` field alongside `freed_bytes` and `removed_chutes`.
- `CACHE_COMPLETE_MARKER`, `CACHE_STALE_MARKER`, and `chmod_if_owned` moved from `manager.py` to `util.py` to support the subprocess entry point without circular imports.

### Fixed
- Mutating webhook no longer applies `automountServiceAccountToken: false` on Pod UPDATE operations, preventing the API server from rejecting immutable-field mutations (e.g. Job controller finalizer sync on completed CronJob pods).
- OPA validating policy (`chutes.rego`) no longer enforces pod-spec rules on Pod UPDATE operations; pod specs are immutable after creation, so spec checks on UPDATE blocked finalizer removal and pod cleanup for pods created before the SA token policy was deployed.

### Removed
- **Image pull endpoints**: Removed `POST /images/pull` and `GET /images/pull/status`
  from the system manager images API. These endpoints are no longer part of the
  supported image management interface.

## [0.2.6] - 2026-04-07

### Changed
- Refactored sek8s module to contain only necessary code for guest services.

## [0.2.5] - 2026-04-02

### Changed
- Initial release under `src/sek8s/` layout (monorepo package refactor).
- Shared code extracted to `sek8s-common`; `sek8s` depends on `sek8s-common`.

## [0.2.3] - 2026-03-11

### Added
- Image management API in system manager: pull, list, delete, prune images from
  the validator mirror.

### Fixed
- Attestation-proxy restart bug in the attestation-system namespace (now handled
  via kubectl without requiring VM restart).

## [0.2.2] - 2026-03-06

### Changed
- System manager API updated: improved cache download performance, concurrent
  download resource handling.
- Cache cleaner updated to check GPU processes and VRAM threshold before eviction.

### Fixed
- Fixed 500 errors from resource constraints during concurrent model downloads.
