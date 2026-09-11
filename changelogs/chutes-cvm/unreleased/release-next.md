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
