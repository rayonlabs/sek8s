### Fixed

- The build no longer fails at "rtmr3-measure : Install tdx-measure" with `'repo_root' is
  undefined`. `repo_root` was defined in `host` group scope but that role runs in a `vm` play,
  so it resolved nowhere. It now lives in `all` scope, which also removes the `playbook_dir`
  workaround the sr25519 role was carrying for the same reason.
