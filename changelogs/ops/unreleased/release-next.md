### Fixed

- `build-setup.yml` provisioned rustup for the account Ansible connects as, which is not
  necessarily the account that runs the build. Builds are normally driven as root — most of the
  build needs it anyway (nbd, cryptsetup, virsh, libvirt) — so rustup landed in the login user's
  home and the build failed half an hour in, at the sr25519 signer, with an error that read like
  the host had never been provisioned. The playbook now takes a single `build_user` (default
  `root`) and provisions everything per-user for that account: rustup, and ownership of the
  checkout. Override it to build unprivileged: `-e build_user=ubuntu`.

- `build-setup.yml` now verifies rustup is usable by the build user before it finishes, rather
  than leaving the mismatch to surface mid-build. The build user's home is read from passwd
  rather than carried in a second variable, so the account and its home cannot disagree, and the
  idempotency check no longer reads `ansible_facts.env.HOME` — which a `become: true` play
  gathers as root's home regardless of which account the install actually ran as.
