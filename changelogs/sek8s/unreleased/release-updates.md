### Changed

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
