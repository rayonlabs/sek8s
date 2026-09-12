### Changed

- **RTMR3 is now a single hardware extend over a digest of the measured file list**, rather
  than one extend per file: `rtmr3 = SHA384(0^48 || SHA384(hash-list))`, where the hash list is
  `tdx-measure hash` output verbatim. 41k per-file TDCALLs cost ~167s of every boot and bind
  nothing a single extend over the ordered list does not. Hashing the list text also binds the
  measured paths, which the per-file content chain did not. **Published RTMR3 measurements must
  be regenerated.**

- `tdx-measure` batches its hashing through `xargs` instead of forking `sha384sum` per file,
  which was the entire cost of the hashing phase (~6ms per file, 255s for 41k files on a real
  guest; 30x faster on a 6k-file bench here, output byte-identical). `sha384sum -z` disables
  GNU's filename escaping — the hazard that forced the per-file stdin form, where a backslash
  in a name silently shifted the hash field — and xargs preserves input order, so digests pair
  positionally with the sorted path list and the echoed names are ignored entirely.

### Fixed

- `chutes-cvm host setup` could not complete on Ubuntu 26.04. The profile pinned
  `linux-image-6.17.0-35-generic`, a noble HWE package that resolute has never carried, so the
  install aborted before QEMU and the native TDX kernel were installed. That left hosts
  upgraded from an earlier release running the new QEMU against the kernel carried over from
  before the upgrade — which presents at VM launch as `kvm run failed Input/output error` on
  every vCPU, the TD created but never enterable. This broke `setup.yml` and every
  `upgrade-host.yml` hop, both of which provision through this path. Pinned to
  `linux-image-7.0.0-31-generic`, resolute's current ABI.

- `chutes-cvm host setup` now checks the pinned kernel is available before installing
  anything, and reports it in one line naming the command that finds the current version.
  Kernel pins expire by design — a pocket carries only the newest ABI — and this previously
  surfaced as an apt error inside a Python traceback, after the repository setup steps had
  already run.
