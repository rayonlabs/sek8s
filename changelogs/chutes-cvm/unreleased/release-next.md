### Fixed

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
