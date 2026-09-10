### Changed

- Bumped the pinned guest HWE kernel from `7.0.0-28.28~24.04.1` to `7.0.0-31.31~24.04.1`.
  The pin is deliberate — it keeps the guest kernel, and so the measurement baseline,
  reproducible across rebuilds — but noble-updates and noble-security carry only the newest
  HWE ABI, so the old version stopped resolving and the build failed outright at "Install HWE
  kernel and headers (pinned)". The new version is the current archive kernel and ships from
  noble-security. Guest RTMR measurements change accordingly.
