# GPU Admin Tools — build recipe (maintainer-only)

This directory is the **build recipe** for NVIDIA's GPU admin tools wheel. It lives under
`src/chutes-cvm/tools/` — in the package project but OUTSIDE the importable `chutes_cvm/`, so it
never ships in the wheel. The built wheel itself is committed inside the package at
`src/chutes-cvm/chutes_cvm/scripts/gpu-tools/nvidia_gpu_admin_tools-*.whl` (bundled + resolved by
`chutes_cvm.paths.gpu_tools_dir`). Rebuild with `make bundle-gpu-tools`.

## Wheel Package

The wheel (`nvidia_gpu_admin_tools-*.whl`) is a pre-built Python package installed on the host to
configure GPU modes (CC mode vs PPCIe mode) for GPU passthrough in TDX VMs. NVIDIA's upstream
`gpu-admin-tools` ships as a loose script repo with no packaging, so `bundle-tools.sh` injects
`entry_point.py` + `pyproject.toml` (in this directory) to give it a `nvidia-gpu-tools`
console-script and builds the wheel from that.

### Source

The wheel is built from:
- Repository: https://github.com/NVIDIA/gpu-admin-tools
- Release: [v2026.06.05](https://github.com/NVIDIA/gpu-admin-tools/releases/tag/v2026.06.05)
- Built using: `poetry build --format wheel` or `python3 -m build --wheel`

### Building the Wheel

To rebuild the wheel package (for maintainers):

```bash
make bundle-gpu-tools        # or: src/chutes-cvm/tools/gpu-tools/bundle-tools.sh
```

This script will:
1. Clone the gpu-admin-tools repository
2. Use the `pyproject.toml` + `entry_point.py` in this directory (the `nvidia-gpu-tools` entry point)
3. Build a wheel package
4. **Place the wheel into the `chutes-cvm` package** (`src/chutes-cvm/chutes_cvm/scripts/gpu-tools/`; override with `WHEEL_OUT_DIR`)
5. Clean up all source/build files (only the recipe remains here)

**Note:** Commit the rebuilt `.whl` (in the package) and this recipe. The wheel is the only artifact that ships in the `chutes-cvm` package.

### Usage

Installation happens once, at CLI-setup time: the package's `install.sh` (run standalone, or by the
`host_tools` ansible role / guest build) `pip install`s this bundled wheel into the chutes-cvm venv and symlinks
`nvidia-gpu-tools` onto PATH. At launch time the code only verifies it runs
(`chutes_cvm.guest.gpu.tools.ensure_gpu_tools_available`) — it does not install on the fly.
Users don't install anything manually.

### License

This tool is part of NVIDIA's gpu-admin-tools repository. Please refer to the repository for license information.
