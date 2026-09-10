### Fixed

- The sr25519 role's "rustup not found" error now names the account it searched under and the
  path it searched, and gives the command to install rustup for that account. rustup is a
  per-user install, so the previous advice — run `build-setup.yml` — was a dead end for the case
  that actually produces this error: build-setup having already run, for a different user.

