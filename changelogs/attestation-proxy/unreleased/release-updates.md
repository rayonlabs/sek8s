### Changed

- The miner hotkey proof-of-possession headers (`X-Chutes-Hotkey`, `X-Chutes-Nonce`,
  `X-Chutes-Signature`) are no longer attached by the shared proxy path. They are now attached
  explicitly by the two authenticated endpoints whose responses the validator reads them from —
  the host-service and chute-service proxy routes — and only on successful responses. Endpoints
  that do not need the proof, including the unauthenticated health route, no longer return it.
  The body signature (`X-Signature`) is unchanged and still covers every proxied response.

### Removed

- The internal (unauthenticated) proxy port no longer serves `/service/{name}/{path}`. That route
  dialled another pod's port 8002 in the workload namespace, so any chute could use the proxy as a
  relay to reach every sibling chute — the cluster's network policies permit that hop by design
  (they admit the proxy to chute pods on 8002), which is exactly why the chute egress policy
  blocking direct chute-to-chute traffic did not contain it. Chutes only ever need the attestation
  service, which is reached over a unix socket and is unaffected. The authenticated copy on the
  external port, which serves the third-party attestation flow, is unchanged.

### Fixed

- Upstream error text is now returned only on the validator-authenticated port. The
  underlying HTTP client's error carries the address it was trying to reach, and the internal
  port answers any chute pod without authentication — so that port now returns a fixed
  message, while the validator keeps the detail it needs to diagnose a guest it cannot log
  into. Previously both ports returned it. The full detail was already written to the log and
  still is.
