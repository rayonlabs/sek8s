# chutes-cvm

CLI and toolkit for operating Chutes confidential GPU VMs.

Installed as the `chutes-cvm` command (published to PyPI). Commands cover the host
lifecycle (`host setup` / `host verify` / `host submit-profile` / `host tune`), VM launch,
and measurement generation. The library (`chutes_cvm.guest`, `chutes_cvm.host`,
`chutes_cvm.measurement`) is importable on its own; the CLI is one consumer of it.

```
pip install chutes-cvm
chutes-cvm host verify
```
