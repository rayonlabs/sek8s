### Changed

- Admission control now restricts which container may mount the shared cache root. The
  `/var/snap/cache` hostPath volume exists so the cache-cleaner init container can scan the tree
  for eviction; because volumes are pod-scoped, any other container in the same pod could mount it
  too. Only the cache-cleaner init container may now do so, matched on the same container name and
  signed image that already gate its root privilege. Containers keep unrestricted access to their
  own per-chute cache directory.

- Container capability admission is now an allowlist instead of a denylist. Only `IPC_LOCK` and
  `NET_BIND_SERVICE` may be added; every other capability is rejected, including the literal
  `ALL` and any spelling variant (`CAP_` prefix, lowercase). Previously the rule matched against
  a fixed list of eight names, so capabilities outside that list were admitted.

- Chute pods may no longer set container lifecycle hooks (`postStart`/`preStop`). Hooks take
  arbitrary arguments and run as a separate process, so they bypassed the rule that restricts
  containers to their signed image's entrypoint. Applies to all containers, init containers and
  ephemeral containers across Pod, Deployment, StatefulSet, DaemonSet, ReplicaSet, Job and
  CronJob. No existing chute spec uses lifecycle hooks; exec probes are unaffected.

- Chute pod probes that use `exec` must now be a plain localhost `curl`. The port and path stay
  free so the health endpoint can change without a policy change, but the command is pinned to
  `/bin/sh -c` with an anchored form, an explicit curl flag set, and a `127.0.0.1` target, so a
  probe cannot carry additional shell commands or reach off the pod. `httpGet` and `tcpSocket`
  probes are unaffected.

- Admission control can now bind a chute pod's per-chute cache directory to the chute identified in
  its launch token. The token's signature is verified in policy, and the mount must match the
  `chute_id` it carries, so the directory a pod may open is no longer determined by a value the
  operator writes. The check activates once the launch-config public key is provisioned into the
  admission controller's config data; it is inactive until then.

- The environment-variable allowlist now applies to init containers and ephemeral containers, not
  only to a pod's main containers. Previously those were unchecked, so an init container could
  carry any variable — including ones explicitly forbidden elsewhere. The cache-cleaner init
  container's own variables were added to the allowlist as part of this.

- Chute workloads may no longer declare `projected` volumes. A projected volume can carry a
  `serviceAccountToken` source, which is honoured regardless of `automountServiceAccountToken:
  false`, so chute code could obtain a Kubernetes API token. Other pods in the namespace are
  unaffected — the cluster's own cleanup job legitimately uses one.

- Chute workloads may no longer select a service account. Previously nothing restricted
  `serviceAccountName`, so a chute could run as an account with broader namespace permissions.
  Chutes now run as the unprivileged default account, matching what they were already deployed
  with. Other pods in the namespace are unaffected.

- The boot-time AppArmor enforcement check is now a hard dependency of `k3s.service` and
  `system-manager.service`. It previously ran only because it was enabled for
  `multi-user.target`, and systemd enablement lives in symlinks, which the RTMR3 measurement
  does not cover — so deleting one symlink disabled the check without changing the measurement.
  The new dependencies live in drop-ins under `/etc/systemd/system`, which is measured, matching
  how `rtmr3-verify.service` is already anchored. Neither service can start unless every sek8s
  profile is loaded in enforce mode.

- Chute workloads must now declare a container named `chute` and a non-empty
  `chutes/config-id` label. The in-guest log shipper finds a chute pod by that label and
  reads only that container, so a pod spec missing either was captured by nothing —
  and because the pod spec is authored outside the guest, that was a way to opt out of
  log capture entirely. Nothing else consulted either value, so neither omission was
  visible before. Real chute specs already set both.

- `/etc/chute-log-shipper` is now covered by the RTMR3 measurement. It holds the validator
  URL logs are sent to and the settings that decide which pods are captured, and it was the
  only sek8s service configuration left out — so an edit to it was the one that would not
  have shown up in the measurement.

- `/usr/lib` is now covered by the RTMR3 measurement on both images. The measured set
  previously included the system binaries but not the shared libraries those binaries load
  — half the executable surface, and the one the confined services are granted read and
  execute access to. It also brings the kernel modules on disk into the measurement, which
  the boot-time measurement did not reach. Costs roughly ten seconds of boot.

- Chute workloads may no longer set container `args`. Overriding a container's command was
  already blocked, but Kubernetes offers two ways to shape what a container runs: with the
  command omitted, `args` replaces the image's own default and is handed to its entrypoint.
  Restricting one and not the other enforced half the guarantee. Real chute specs set no args
  at all: the init container runs its image entrypoint configured by environment variables,
  and the chute container uses the sanctioned command form, which already carries the
  arguments it needs.
  No image shipped today acts on such arguments, so nothing was exploitable; the rule closes
  the asymmetry so a future image that does act on them cannot slip through unnoticed.
  Applies to main, init and ephemeral containers; other pods in the namespace are unaffected.

### Fixed

- `/run/chutes` is now unreadable except to the services that declare what they need from
  it. Its permissions decide whether four non-root services can reach secrets like the mTLS
  client key, but no script actually set them: the directory was created as a side effect of
  `mkdir -p` on a subdirectory, which leaves intermediate components at the umask default,
  and six later attempts to set the mode were silently no-ops because it already existed. It
  is now created 0700 everywhere, and the three services that genuinely read from it each get
  a private view containing only their own subtree. The fourth turned out to need nothing at
  all — its config file is read by the init system before it drops privileges — so its two
  read grants have been removed.

- The log shipper can no longer reach the container runtime socket directly. It discovers
  pods through a wrapper that allows two read-only commands, but the wrapper ran with the
  service's own permissions, so anything that compromised the service could skip it and drive
  containerd itself — pulling and running any image, with neither admission control nor
  signature verification in the way. Pod discovery now transitions into a separate, tighter
  profile that owns the socket, and the service's own profile has neither the socket nor a
  shell. The wrapper itself was rewritten from bash to POSIX sh in the same change: bash runs
  whatever `$BASH_ENV` points at before the script starts, which would have let a caller run
  its own code inside the tighter profile without passing the allowlist at all. This matters
  because the log lines the service parses are written by the chutes it watches, which makes
  it the one component with a genuinely hostile input.

- Cache setup no longer fails to boot when the model cache contains a directory it cannot read.
  The recursive ownership pass ran before the mode repair and the service is configured to power
  the VM off on failure, so a workload leaving an unreadable directory in the cache prevented the
  VM from starting again. Directory modes are now repaired before ownership is changed, so the
  walk can descend regardless of what a workload left behind.

- The RTMR3 measurement now has a single implementation. Four separate pieces of code decided
  which files RTMR3 covers, in what order, and how each is hashed — the initramfs measurer, the
  build-time manifest generator, `rtmr3-verify`, and the host-side measurement predictor — kept in
  step by a comment asking maintainers to update them together. They had drifted apart in four
  ways: only some stripped inline comments and trailing whitespace from `tdx-measure.conf`; a conf
  entry that was itself a symlink was measured by two of them and skipped by the other two (and for
  a symlinked directory the boot measurer silently covered *nothing* while the verifier expected
  the whole tree); the boot-time sort was not pinned to `LC_ALL=C`, so a locale could reorder the
  extend chain; and the build manifest hashed with `sha384sum FILE`, which prefixes its output with
  a backslash for systemd's `\x2d`-escaped unit names, so such a file could never match the hash
  computed at boot and would have powered the VM off on every boot. All four now run one
  `tdx-measure` script, which also refuses a symlinked conf entry outright rather than resolving it
  differently in different places. Measured values are unchanged for the current path list.

- Fixed a trap in the measured-file list: a directory and a file beneath it could both be
  listed, and those files were then measured twice. Nothing was left uncovered, but the
  measurement depended on that redundancy — removing an entry that looked superfluous would
  have changed the measurement, and a mismatch powers the machine off, so a tidy-up would
  have stopped every VM booting and looked like tampering. Each file is now measured once
  however the list names it.

- Cluster-init secrets are now staged root-only. Three secrets are copied into a temporary
  directory for the one boot step that needs each, then removed — but they were copied
  world-readable into a world-traversable directory, widening files that are otherwise
  owner-only for as long as that step ran. Every step already runs as root, so nothing
  needed the wider access.

- The two units that fix up permissions under `/run/chutes` now run their tools directly
  instead of through `/bin/sh`. A shell there auto-attaches the AppArmor profile written for
  stray and escaped shells, which denies reading that directory, so any recursive walk failed
  unless the unit happened to run before AppArmor loaded. `registry-tls-config` was not
  failing — all its arguments are named paths — but the trap is removed so a future recursive
  change cannot hit it, where the group it sets is load-bearing.

### Removed

- Dropped references to a `k3s-agent` service that the guest never runs. Seven units ordered
  themselves before it and an Ansible handler restarted it; the guest has always installed k3s in
  server mode, so systemd silently ignored the ordering and nothing ever notified the handler.
  `setup-cache.service` was the only unit whose sole ordering constraint was the dead one, and now
  orders against the real service. Also removed an unused `workers` group_vars file, which
  contained only commented-out examples and never loaded.

- Removed the boot-time unit that re-grouped the fetched signing keys, along with the
  `chutes-keys` group it populated. Those keys are public verification keys, written
  world-readable by the initramfs that fetches and signature-verifies them, so the group
  gated nothing — its only observable effect was a unit that intermittently failed. The
  admission controller reads the keys exactly as before, through the bind mount that was
  already doing the work.

- Removed an unfinished module-attestation and module-signing subsystem from the guest
  security role. Neither was ever installed — both task includes were commented out — but
  the leftovers read as live security controls, including a "reporting thresholds" block
  setting a maximum acceptable number of unsigned kernel modules. Security posture here is
  all-or-nothing and is enforced by measurement and AppArmor, so a threshold knob was
  misleading as well as unused. Twelve files, four unused variables and a README entry
  documenting a setting that configured nothing. The binary-attestation timer, which is
  installed and does run, is unaffected.
