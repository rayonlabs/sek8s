# Changelog

All notable changes to the `attestation-proxy` package will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

Version source of truth: `src/attestation-proxy/VERSION`

## [0.3.2] - 2026-09-10

### Added
- The external proxy attaches a miner-hotkey proof-of-possession
  (`X-Chutes-Hotkey`/`X-Chutes-Nonce`/`X-Chutes-Signature`) to each response when a `MINER_SEED`
  is configured, so the validator can authorize release-candidate measurements at runtime. The
  seed is optional: proxies without it (older charts) pass through unsigned, so existing VMs keep
  working.

### Changed
- Forward `server` response header to clients (removed from hop-by-hop suppression list)
- Deployment manifest updated: `secret-reader` RBAC Role now includes `validator-auth` in `resourceNames`, and the `wait-for-credentials` init container waits for the `validator-auth` Secret before the attestation-proxy pod starts. The `validator-auth` Secret is no longer baked into the proxy manifest at build time — it is created at runtime by the cluster-init script on every boot.
- The attestation proxy now runs its two ports (external 8443, internal 8444) via
  the shared `WebServer.serve()` instead of a bespoke `run_server_async()` that
  hand-rolled a `uvicorn.Config` and silently dropped `mtls_required` /
  `client_ca_path` / `require_tls`. Each port now honours its full TLS/mTLS/bind
  config with no per-call-site server wiring that could drift.
- The external port (8443) presents the initramfs-minted, CA-signed server cert;
  the validator pins it to the VM's registered CA and authenticates with signed
  request headers. Client-cert mTLS (`MTLS_REQUIRED`) is intentionally NOT
  enabled on the proxy — it is not how validators authenticate.
- Install a loguru sink with `diagnose=False` at startup
  (`sek8s_common.log_config.configure_logging`), so exception tracebacks no longer render frame-local
  values into the proxy's logs. The tracebacks themselves are unchanged.
- The miner hotkey proof-of-possession headers (`X-Chutes-Hotkey`, `X-Chutes-Nonce`,
  `X-Chutes-Signature`) are no longer attached by the shared proxy path. They are now attached
  explicitly by the two authenticated endpoints whose responses the validator reads them from —
  the host-service and chute-service proxy routes — and only on successful responses. Endpoints
  that do not need the proof, including the unauthenticated health route, no longer return it.
  The body signature (`X-Signature`) is unchanged and still covers every proxied response.

### Fixed
- Upstream error text is now returned only on the validator-authenticated port. The
  underlying HTTP client's error carries the address it was trying to reach, and the internal
  port answers any chute pod without authentication — so that port now returns a fixed
  message, while the validator keeps the detail it needs to diagnose a guest it cannot log
  into. Previously both ports returned it. The full detail was already written to the log and
  still is.

### Removed
- `run_server_async()` — replaced by the shared `WebServer.serve()`.
- The internal (unauthenticated) proxy port no longer serves `/service/{name}/{path}`. That route
  dialled another pod's port 8002 in the workload namespace, so any chute could use the proxy as a
  relay to reach every sibling chute — the cluster's network policies permit that hop by design
  (they admit the proxy to chute pods on 8002), which is exactly why the chute egress policy
  blocking direct chute-to-chute traffic did not contain it. Chutes only ever need the attestation
  service, which is reached over a unix socket and is unaffected. The authenticated copy on the
  external port, which serves the third-party attestation flow, is unchanged.

## [0.3.1] - 2026-05-26

### Fixed
- Strip upstream `Server` response header in `proxy_request` so Uvicorn's own header is the only one sent to clients. Forwarding the backend's `Server` header alongside Uvicorn's own produced a duplicate that aiohttp 3.13.4+ rejects with `Duplicate 'Server' header found`.

## [0.3.0] - 2026-05-19

### Fixed
- Added `curl` to the production Docker image so Kubernetes startup, liveness, and readiness exec probes can execute successfully.

> **Note:** The changes listed under [0.2.0] were not published due to a build issue caused by a version misalignment from the prior monorepo restructure. The 0.2.0 changes (X-Signature response header) are first published in this release.

## [0.2.0] - 2026-05-04

### Added
- X-Signature response header on all externally proxied responses. The header contains a base64-encoded RSA-PKCS1v15-SHA256 signature of the response body, signed with the host TLS private key, enabling clients to verify the responder holds the private key corresponding to the TDX-attested certificate.

## [0.1.1] - 2026-04-07

### Changed
- Refactored sek8s to pull out attestaton proxy source code into standalone package to align with version management for the associated image.

## [0.1.0] - 2026-04-02

### Added
- Initial release: extracted from `sek8s` as a standalone package during monorepo
  refactor (Phase 2). Runs as a k3s container image, depends only on `sek8s-common`.

> **Prior history:** Before extraction, proxy code lived in `sek8s`. Notable
> pre-extraction fix: v0.2.3 (2026-03-11) resolved a bug preventing proxy restart
> in the attestation-system namespace without a full VM restart.
