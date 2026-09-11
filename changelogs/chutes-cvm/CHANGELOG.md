# chutes-cvm Changelog

The `chutes-cvm` CLI + toolkit (`src/chutes-cvm/`) — an independently installable host CLI
(`pip`/`install.sh`). Versioned with SemVer via `src/chutes-cvm/VERSION`. Run
`make promote-changelogs` to aggregate fragments into the current version section.
## [0.1.0] - 2026-09-11

### Added
- **`chutes-cvm measurements`** — TDX measurement generation is now a first-class command
  group (`generate` / `list`), forwarding to `chutes_cvm.measurement`. The guest build
  calls the CLI instead of per-register shell scripts + ansible roles:
  - **The API is the source of truth for known host classes.** `generate` reads the published host
    profiles (`GET /servers/tdx/host_profiles` — public, unauthenticated; `--api-base`, default
    `https://api.chutes.ai`) and produces ONE entry per class: it derives the topology from each
    stored discover-profile document (mirroring the live `host_topology_fingerprint`), runs the fork
    offline, and **carries the API's 64-hex `fingerprint` through onto the entry** (never recomputed).
    The reconciler joins published measurements to submitted host profiles on that fingerprint, so it
    is required — an entry without one is unmatchable ("pending" forever even though it launches). The
    in-repo baseline registry (`known_topologies`, `GpuProfile.baselined_measurements`) is removed:
    adding hardware is `chutes-cvm host submit-profile`, not a code change here. By default only
    *measured* classes are processed (what a third party can verify); **`--include-pending`** also
    processes classes awaiting generation (the generator's queue), which the release build passes to
    turn newly submitted profiles into published measurements.
  - **`generate`** — with no `--register`, computes EVERY register in one POST-LUKS call — mrtd +
    RTMR0 (all API host classes) + RTMR1/RTMR2 (the image's staged direct-boot artifacts) + RTMR3
    (mounting the root, unlocking it with `LUKS_PASSPHRASE`) — and writes the version's single
    `measurements.yaml`. This is what the miner-VM build runs; there is no separate pre-LUKS RTMR3
    step.
  - **`generate --register rtmr0`** — just the version-level MRTD + per-topology RTMR0 via the
    tdx-measure fork (offline, any x86-64 Linux — no TDX/GPU) as a JSON block. A standalone partial
    (a full `generate` computes RTMR0 inline).
  - **`generate --register rtmr3`** — just the version-level RTMR3 (SHA-384 chain over the image's
    `/etc/tdx-measure.conf` files), mounting the root read-only. `LUKS_PASSPHRASE` unlocks an
    encrypted root; always recomputes **fresh** (the real value, no cached/reused fallback). Used by
    the partner GPU-VM build (`tee-gpu-vm.yml`), which has no aggregation.
  - **`list`** — prints the API's known host classes (fingerprint + GPU summary).
- **`src/chutes-cvm/install.sh` — the single source of truth for install** (replaces
  `host-tools/scripts/provision/setup-chutes-cvm.sh`). One script owns both fetch and install, and
  picks its mode: run from a checkout (ansible / build / dev) → editable install from that checkout
  (a `git pull` updates the code, no reinstall); `curl -sSL …/src/chutes-cvm/install.sh | bash` →
  sparse shallow-fetch (`host-tools/` + `firmware/` + `src/chutes-cvm/`) into a **temporary** dir,
  non-editable install (CLI + bundled nvidia-gpu-tools into a persistent venv, firmware copied next
  to it), then delete the temp checkout. The sparse path-list and the install steps live here
  exactly once; the `host_tools` ansible role and the guest build both invoke it. A standalone
  install is fully launch-capable — no manual `git clone`, no lingering source, no PyPI, no R2.
  `chutes-cvm host setup` no longer installs/verifies the CLI or gpu-tools (install.sh installs
  them; the launch path verifies gpu-tools where it matters); its `--install-tools-only` flag and
  the `install_dependencies` step are removed.
- **`make bundle-gpu-tools`** — discoverable maintainer target that rebuilds the vendored
  nvidia-gpu-tools wheel into the package (recipe at `src/chutes-cvm/tools/gpu-tools/`).
- **`chutes-cvm image` / `chutes-cvm config`** — the base-image tool (`image download` /
  `image verify` / `image manifest`) and the config validator (`config init` / `config verify`) are
  noun groups, so every caller routes through the one console script.
- **`chutes-cvm guest` — the TDX VM runtime lifecycle, grouped under one noun** (mirroring `host`):
  `guest launch` / `stop` / `down`. The operator surface is all nouns; the low-level QEMU boot
  primitive (`chutes_cvm.guest.__main__`) is not a CLI command — `guest launch` reaches it via a
  Python import. GPU/PCI hardware ops (`reset-gpus`, `vfio-wedged`) live under `host`, since they act
  on host hardware with or without a running guest.
- **`chutes-cvm guest launch`** — end-to-end VM launch orchestrator (`chutes_cvm.guest.launch`): a
  Python decision layer that resolves config with precedence (CLI > YAML > defaults), validates, runs
  the host gates (TDX active, NUMA, duplicate-VM guard), then runs each privileged step — the
  bundled bash helpers for the tool-sequence ones (volumes, config volume, bridge), and in-process
  `sudo` file ops for the per-VM image copy — and boots via the QEMU boot
  primitive. This is the one command a miner uses to bring a VM up. Per the AGENT.md bash-vs-Python
  rule, Python owns the decisions and bash still owns the root system mutations (cryptsetup/mkfs/nbd,
  ip/iptables).
- **Launch gates on a measurement for THIS image, not just the host class.** Before any
  GPU/volume/boot work, `guest launch` runs the same control-plane check as `host verify`: it reads
  the base image's `(version, rc)` from its manifest, captures + signs the host profile, and asks
  `POST /servers/tdx/preflight` whether a *published measurement for that exact `(version, rc)`*
  covers this host class. A stored host profile is no longer treated as launchable — a class can be
  registered (or measured for a different version) yet have no measurement for the image you are
  about to boot. If it is not launchable it refuses early instead of booting a VM that would only
  fail attestation, pointing you at `host submit-profile`; fails closed if the API is unreachable;
  `--force` overrides (with a warning). Only **benchmark** VMs skip it (dummy creds, not attested);
  **debug (RC)** images are no longer special-cased — their `rc:true` measurement must be published
  just like a production image's, which the `(version, rc)` join checks directly.
- **`chutes-cvm image download` / `config init` / `guest stop` / `guest down`** — the launch
  orchestrator's modes that used to be flags are now first-class commands: `image download [--debug]`
  fetches + verifies a base image set, `config init` scaffolds a `config.yaml`, `guest stop` shuts
  down only the VM (leaving the bridge up), and `guest down` tears the whole environment down (VM +
  bridge + benchmark-netlog).
- **`chutes-cvm guest stop` and `guest down` shut the guest down gracefully by default.** Both POST
  a hotkey-signed request to the guest system-manager API (`http://<vm_ip>:8080/status/system/shutdown`,
  the same endpoint the chutes-miner control plane uses) so the VM powers off cleanly — a miner can
  shut down gracefully with only their config.yaml, no chutes-miner CLI needed. `stop` does just the
  guest shutdown (bridge + volumes left in place); `down` additionally tears down the host-side
  bridge + benchmark-netlog — the extra dependency cleanup is the only difference. `--force` skips
  the API and force-kills QEMU on either command; a graceful attempt that can't reach the API stops
  and points the operator at `--force`.
- `chutes-cvm host submit-profile --target-os <release>` — register the host class this machine
  becomes after an OS upgrade (target release, its QEMU, its `-cpu` args), mirroring
  `host verify --target-os`. Unsupported releases are rejected before anything is signed or sent.

### Changed
- **Consolidated the host entrypoint scripts into the `chutes-cvm` CLI.** The thin wrapper
  scripts `run-td`, `verify-host`, `setup-tdx-host`, `tune-host.sh`, `restore-host.sh` and the
  `host-tools/bin/chutes-*` PATH delegators are removed; their operations are now `chutes-cvm`
  subcommands: `guest launch`, `host verify`, `host setup`, `host tune`, `host restore`,
  `host reset-gpus`.
  Logic still lives in the `chutes_cvm.guest` / `chutes_cvm.host`
  modules; the CLI is a thin front door. `discover-profile.sh` is deliberately kept as a
  standalone script (bundled with the package). Callers invoke the `chutes-cvm` console script
  installed by the package's `install.sh`, rather than the removed `host-tools/bin/` symlinks.
- **The guest build's measurement phase is entirely CLI-owned — no measurement roles.** The
  `compute-rtmr0` / `compute-rtmr1-2` / `compute-rtmr3` / `aggregate-measurements` roles and their
  shell scripts are removed; the miner-VM build calls `chutes-cvm measurements generate` once,
  POST-luks, for every register. The CLI unlocks the encrypted root with `LUKS_PASSPHRASE` to read
  RTMR3's userspace files (always fresh — no cached-value reuse), so there is no longer a separate
  pre-LUKS RTMR3 stage. `libguestfs-tools` (guestmount) is now a build-host prereq
  (`build-setup.yml`); `tee-gpu-vm.yml` calls `measurements generate --register rtmr3` inline. The
  generator's firmware paths resolve via `chutes_cvm.paths`.
- **The package-level CLI dispatcher moved to the package root** — `chutes_cvm/guest/cli.py` →
  `chutes_cvm/cli.py` (console script `chutes-cvm` → `chutes_cvm.cli:main`), since it routes to the
  host, guest, and measurement subpackages rather than belonging to `guest/`.
- **`chutes-cvm` is now a real Python package under `src/chutes-cvm/`** (import
  `chutes_cvm`, published to PyPI), instead of a loose module tree on `PYTHONPATH` at
  `host-tools/scripts/chutes/`. The rename from `chutes` to `chutes_cvm` avoids colliding
  with the Chutes platform SDK once installed. The offline measurement engine
  (`guest-tools/measurement/*.py`) moved into `chutes_cvm.measurement`, dropping its
  `sys.path` shims.
- **Host provisioning installs the package** — `host_tools` stages and runs the package's
  `install.sh`, which fetches + `pip install -e`'s the package into a venv and puts the
  `chutes-cvm` console script on PATH (with its deps: pyyaml/pydantic-settings/substrate-interface). Host
  ansible calls `chutes-cvm <command>` instead of `python3 -m chutes.guest.*`, so dependency-bearing
  commands (`config`, and the API-backed `host verify`) run with their deps available. The sparse
  checkout now includes `src/chutes-cvm/`. Guest image build keeps `PYTHONPATH` (stdlib commands
  only). Set `CHUTES_CVM_PYPI=1` to install from PyPI instead of the checkout.
- **The package is self-contained — no repo-layout assumptions.** The built nvidia-gpu-tools
  wheel moved into the package (`chutes_cvm/scripts/gpu-tools/`, bundled in the wheel; its
  maintainer build recipe lives at `src/chutes-cvm/tools/gpu-tools/`, run via `make
  bundle-gpu-tools`, and builds the wheel into the package), and the default launch-config lookup
  is now `./config.yaml` (where
  `chutes-cvm config init` writes it) / `$CHUTES_CVM_CONFIG` rather than a checkout path. The only
  checkout-relative resolution left is the guest firmware (OVMF) — MRTD-measured, so intentionally
  not shipped in this host-side package. A repo-present (editable) install resolves it from the
  checkout; a standalone (non-editable) install copies it out of the fetched checkout to a
  persistent dir and sets `$CHUTES_CVM_FIRMWARE_DIR` in the shim, so no repo or R2 is needed at
  runtime.
- **nvidia-gpu-tools is installed at CLI-setup time, not lazily at launch.** `install.sh`
  `pip install`s the bundled wheel into the chutes-cvm venv and symlinks `nvidia-gpu-tools` on
  PATH; the runtime lazy self-installing venv machinery is removed (`chutes_cvm.guest.gpu.tools`
  now only verifies the CLI is present and runs, raising a clear "re-run install.sh" error).
- **Host lifecycle + attestation live under one `chutes-cvm host` group.** `host setup` / `verify`
  / `submit-profile` / `tune` / `restore` replace the former top-level `setup-host` / `verify-host`
  / `tune-host` / `restore-host`; the standalone `discover-profile` command is dropped (its capture
  is done inline by the verify/submit flow — `discover-profile.sh` stays as the bundled helper).
  `host verify` is API-backed: Gate A (host runs its OS release's QEMU) stays local; Gate B reads the
  base image's `(version, rc)` from its manifest, captures the host's platform metadata
  (discover-profile.sh), signs it with the miner hotkey (sr25519), and asks
  `POST /servers/tdx/preflight` whether a published measurement for that `(version, rc)` covers this
  host class — the control plane owns the fingerprint and returns a single `launchable` verdict,
  replacing the in-repo `known_topologies` set. `--target-os` checks against a target OS's QEMU, and
  `--base-image` picks which image set to check (pre-upgrade). `host submit-profile` registers an
  unmeasured host class (`POST /servers/tdx/host_profiles`, a distinct operation from the preflight
  check), run when the check reports the class is not yet launchable. Fails closed (BLOCKED) when it
  can't get a verdict. Adds `substrate-interface` to the chutes-cvm package for the signature.
- **`detect_profile` no longer gates on a local baselined set.** It resolves the GPU profile and the
  live fingerprint (which still drive the launch `-smp`/`-m`); acceptance is the control plane's call.
- **VM-management scripts now ship inside the `chutes-cvm` package.** The privileged bash helpers
  (`discover-profile.sh` and the `volumes/`, `network/`, `devices/` scripts)
  plus the `config/` schemas moved from `host-tools/scripts/` into `chutes_cvm/scripts/`, resolve
  package-relative, and are bundled in the wheel. `host-tools/scripts/` now holds only config
  examples and the deprecated `quick-launch.sh` compat shim. Ansible host launch/upgrade invokes
  `chutes-cvm guest launch`; the install no longer needs a
  `CHUTES_CVM_SCRIPTS_DIR` env (the scripts are package-relative).
- **The launch orchestrator is Python, not a bash script.** The former `quick-launch.sh` is ported
  to `chutes_cvm.guest.launch` (`chutes-cvm guest launch`): Python owns arg/config precedence,
  validation, the host gates and the duplicate-VM guard, and calls the bundled bash helpers for the
  privileged volume/network steps then boots via the QEMU boot primitive
  (`chutes_cvm.guest.__main__`, reached by import — not a CLI command). Its old `--download` /
  `--template` / `--clean` early-exit modes are the first-class `image download` / `config init` /
  `guest down` commands above. The `config.tmpl.yaml` template moved into the package (so
  `chutes-cvm config init` can emit it); the config `.example.yaml` files stay in
  `host-tools/scripts/config/`. A deprecated `host-tools/scripts/quick-launch.sh` shim remains
  (forwards to `chutes-cvm guest launch`) so existing
  miner automation that invokes the script by path keeps working across the upgrade; when run from
  a checkout without the CLI installed, it bootstraps it via the checkout's `install.sh` (editable)
  so `git pull` + the wrapper gets a host going with no separate install step.
- **Launch config is one pydantic-settings model (`LaunchConfig`).** It is the single source of
  fields, defaults, validation, and precedence — **CLI > env (`CHUTES_CVM_*`, nested with `__`) >
  config.yaml > defaults** (nested sections deep-merge across sources) — replacing the hand-rolled
  defaults/flag maps and the KEY=value shell bridge. The model uses per-area nested sections (`vm`,
  `network`, `volumes`, …) that **mirror the existing `config.yaml` structure, so miners' configs
  load natively with no migration**. The same model generates a starter file: `chutes-cvm config init`
  emits a schema-derived, commented config, and `chutes-cvm config verify <file>` validates against the
  model. This drops `jsonschema` and the `config-schema*.json` / `config.tmpl.yaml` files for
  `pydantic-settings`; `chutes-cvm guest down` reads the network values in Python and passes them to
  `teardown.sh` (no more `chutes-cvm config` eval round-trip).
- **`chutes-cvm host setup` is now the complete per-host configuration.** It folds in what were
  three ansible roles so that running the CLI fully provisions a host (launch-ready modulo the CLI
  install itself, PCCS secrets, and a reboot): the `ntp` role becomes `_setup_ntp()` (chrony with
  `makestep` to step a skewed BMC RTC before any VM inherits the host clock), the `chutes_dirs` role
  becomes `_ensure_chutes_dirs()` (`/var/lib/chutes/base-images` + `vm-images`), and the host
  operational deps from `host_prerequisites` (chrony, aria2, xfsprogs, python3-yaml) move into a new
  version-independent `HostProfile.base_packages` installed alongside the kernel + TDX stack.
- **The `setup.yml` host-setup playbook is now thin orchestration.** It runs only `host_tools`
  (bootstraps the CLI), `tdx_bootstrap` (`chutes-cvm host setup` + reboot + TDX-init verify), and
  `pccs_configure` (vault-held PCCS secrets) — the boundary is: the CLI owns per-host config,
  ansible owns bootstrap, secrets, and fleet reboot/verify. The `host_tools` role now self-ensures
  its own install prerequisites (python3-venv/pip **+ git** for the sparse fetch), so no separate
  pre-CLI `host_prerequisites` step is needed in setup.
- **`chutes-cvm host verify` no longer requires a downloaded base image.** Verification asks a
  version-free question — *is this host class known, and which published images cover it?* — via the
  new `POST /servers/tdx/host_profiles/status`, replacing the per-version
  `POST /servers/tdx/preflight` call. Previously a host with no image set reported
  `BLOCKED (image): manifest.json missing`, which made the gate unreachable on exactly the hosts
  that need it: a brand-new box is verified (and `--submit`-registered for measurement) *before* it
  downloads anything. The two gates are now cleanly split — `host verify` answers "can this host run
  anything, and what", `guest launch` keeps its unchanged per-version preflight against the
  `(version, rc)` it actually holds.
  - READY now lists the images the class can launch, so an operator sees what to download.
  - A class that is registered but awaiting measurement generation is reported as such and is no
    longer prompted to re-submit — re-submitting does not advance the queue.
  - If a base image *is* downloaded, its `(version, rc)` is checked against the covered set as a
    **note**: an uncovered image is flagged (exit 2) but never invalidates the host-class verdict.
    `--base-image` now selects the image for that note only.
- **`chutes-cvm image manifest` now writes `manifest.json` next to the qcow2 by default**
  (previously `<base>.manifest.json`). `manifest.json` is the only name the readers —
  `image verify`, `image download`, and `guest launch` — look for, so a freshly generated
  set is directly consumable and copyable as a whole directory. Pass `-o` for the old name.
- Offline RTMR3 prediction now runs the same `tdx-measure` script the guest runs, instead of
  reimplementing the walk in Python. The script is bundled with the package, so a `pip install
  chutes-cvm` can still predict a measurement with no checkout, and the guest image is built by
  copying that same file in. Previously this module decided independently which files to measure,
  in what order, and how to hash them, and had drifted from the guest in ways that would have made
  a predicted measurement disagree with the one a VM actually produces. Only the chain fold stays
  in Python, because at boot the hardware does the folding.

### Fixed
- Bridge networking now clamps TCP MSS to the egress interface's PMTU (`setup-bridge.sh`),
  so guest TLS handshakes survive a small-MTU `public_interface` such as a WireGuard tunnel
  (MTU 1280). Previously the guest advertised an MSS from its own 1500 NIC, and oversized
  handshake segments were black-holed on the smaller uplink — connections established but
  stalled during the TLS handshake.
- `--target-os` now rewrites every OS-derived field of the submitted host profile, not just
  the QEMU used for the readiness check. A host asking about (or registering for) a release it
  has not upgraded to yet no longer submits its live QEMU, `-cpu` args and OS release — e.g. a
  25.10 host targeting 26.04 previously registered "26.04 + QEMU 10.1.0", a pair 26.04 never
  ships. `host verify --target-os ... --submit` now registers the target class too.
- `host submit-profile` on an unsupported OS release (or a supported one running a QEMU it does
  not ship) is now rejected locally, as `host verify` already was. It previously registered the
  host class anyway, spending a measurement-generation slot on a class that could never attest.
- Measurement generation now skips a host whose GPUs are not all the same model instead of
  measuring it as whichever model was listed first. Such a host cannot launch anyway —
  launching has always rejected mixed hardware — so the entry it produced was unusable
  rather than wrong in a dangerous way; it is now reported as pending with the reason,
  alongside the other hosts that cannot yet be generated offline.
- Each GPU profile now describes exactly one graphics card. The RTX PRO 6000 profile
  covered both the Workstation and Server editions, so a Workstation host was measured
  using hardware details read from a Server card. Workstation Edition is not supported and
  no longer resolves — such a host is reported as pending rather than measured incorrectly.
  A profile carries reserved CPU counts, guest memory rules, firmware and confidential
  computing settings as well, so two cards that happen to agree on those today could
  quietly diverge later.
- `install.sh` reused an existing venv without checking which Python built it. After an OS
  release upgrade moved `python3` (3.13 to 3.14 on a TEE host), the new interpreter no longer
  looked at the venv's `site-packages`, so the install reported success and left a `chutes-cvm`
  on PATH that died with `ModuleNotFoundError: No module named 'chutes_cvm'`. The venv is now
  recreated when its recorded version differs from the running interpreter; a venv matching the
  current version is still reused, and an unparseable `pyvenv.cfg` is left alone.
- `install.sh` now verifies the package imports from the venv before writing the PATH shim,
  instead of writing it unconditionally. Any install that leaves `chutes_cvm` unimportable —
  a version-mismatched venv, an editable install whose source moved — now fails loudly rather
  than producing a broken command that looks installed.
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

### Removed
- **The `ntp` and `chutes_dirs` ansible roles** — folded into `chutes-cvm host setup` (above). The
  `host_prerequisites` role stays (still used by the launch / remediate / build-setup playbooks) but
  is no longer part of `setup.yml`.
- Removed the lab-validated host topology matrix (`chutes_cvm/host/support_matrix.py`) and the
  `chutes-cvm host setup --topology-matrix` flag that printed it. The matrix was a hardcoded
  set of (Ubuntu, GPU SKU, GPU count) triples maintained by hand and consulted by nothing —
  its lookup helper had no callers, so it documented support rather than enforcing it, and it
  drifted from reality (B300 was listed by the formatter but absent from the data). Host
  support is now determined by probing the actual host with the chutes-cvm CLI, and the set of
  supported profiles is served by the API, so the static table was a second source of truth
  with no way to stay correct. The validated-topology table in `host-tools/README.md` remains
  as operator documentation.

