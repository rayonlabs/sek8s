#!/bin/bash
# Bundle GPU admin tools from NVIDIA gpu-admin-tools repository.
#
# Maintainer-only build recipe (run via `make bundle-gpu-tools`). NVIDIA's gpu-admin-tools
# ships as a loose script repo with NO packaging, so this clones it, injects entry_point.py +
# pyproject.toml (next to this script) to give it a `nvidia-gpu-tools` console-script, builds a
# wheel, and drops the wheel into the chutes-cvm package (the only artifact that ships). This
# recipe lives under src/chutes-cvm/tools/ — in the package project but OUTSIDE the importable
# chutes_cvm/, so it is never shipped in the wheel.

set -e

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
# Build INPUTS (entry_point.py, pyproject.toml) live next to this script.
TARGET_DIR="${SCRIPT_DIR}"
# Build OUTPUT: the wheel is committed inside the chutes-cvm package (bundled + resolved by
# chutes_cvm.paths.gpu_tools_dir). chutes_cvm/ is a sibling of tools/ under src/chutes-cvm/.
# Override WHEEL_OUT_DIR to place it elsewhere.
WHEEL_OUT_DIR="${WHEEL_OUT_DIR:-${SCRIPT_DIR%/tools/gpu-tools}/chutes_cvm/scripts/gpu-tools}"
GPU_ADMIN_TOOLS_URL="https://github.com/NVIDIA/gpu-admin-tools.git"
GPU_ADMIN_TOOLS_TAG="${GPU_ADMIN_TOOLS_TAG:-v2026.06.05}"
BUILD_DIR="${TARGET_DIR}/.build"

echo "Bundling GPU admin tools from NVIDIA gpu-admin-tools repository..."
echo "Repository: ${GPU_ADMIN_TOOLS_URL}"
echo "Tag: ${GPU_ADMIN_TOOLS_TAG}"
echo "Target: ${TARGET_DIR}"
echo ""

# Clean up any previous build
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

echo "Cloning gpu-admin-tools repository at ${GPU_ADMIN_TOOLS_TAG}..."
git clone --depth 1 --branch "${GPU_ADMIN_TOOLS_TAG}" "${GPU_ADMIN_TOOLS_URL}" "${BUILD_DIR}/gpu-admin-tools" 2>&1 | grep -v "^Cloning\|^remote:\|^Resolving\|^Receiving\|^Updating" || true

REPO_DIR="${BUILD_DIR}/gpu-admin-tools"

if [ ! -f "${REPO_DIR}/nvidia_gpu_tools.py" ]; then
    echo "Error: Could not find nvidia_gpu_tools.py in gpu-admin-tools repository"
    exit 1
fi

echo "Copying repository files to build directory..."
BUILD_SRC_DIR="${BUILD_DIR}/source"
mkdir -p "${BUILD_SRC_DIR}"

# Copy the main script
cp "${REPO_DIR}/nvidia_gpu_tools.py" "${BUILD_SRC_DIR}/"

# Copy required module directories
for dir in utils gpu pci cli; do
    if [ -d "${REPO_DIR}/${dir}" ]; then
        echo "  Copying ${dir}/ directory..."
        cp -r "${REPO_DIR}/${dir}" "${BUILD_SRC_DIR}/"
    fi
done

# Copy entry_point.py and pyproject.toml from our repo
echo "  Copying entry_point.py and pyproject.toml from repository..."
cp "${TARGET_DIR}/entry_point.py" "${BUILD_SRC_DIR}/"
cp "${TARGET_DIR}/pyproject.toml" "${BUILD_SRC_DIR}/"

echo ""
echo "Building wheel package..."
cd "${BUILD_SRC_DIR}"

BUILD_VENV="${BUILD_DIR}/build-venv"
python3 -m venv "${BUILD_VENV}"
"${BUILD_VENV}/bin/pip" install -q --upgrade pip build wheel poetry-core

# Try poetry build first, fall back to python -m build
if command -v poetry &> /dev/null; then
    echo "  Using poetry to build wheel..."
    poetry build --format wheel 2>&1 | grep -v "^Building\|^Created" || true
    WHEEL_FILE=$(find dist -name "*.whl" 2>/dev/null | head -1)
else
    echo "  Using isolated venv python -m build..."
    "${BUILD_VENV}/bin/python" -m build --wheel 2>&1 | grep -v "^Creating\|^Adding\|^Copying\|^Building" || true
    WHEEL_FILE=$(find dist -name "*.whl" 2>/dev/null | head -1)
fi
    
# Find the built wheel and move it into the chutes-cvm package (the shipped artifact).
if [ -n "${WHEEL_FILE}" ] && [ -f "${WHEEL_FILE}" ]; then
    WHEEL_NAME=$(basename "${WHEEL_FILE}")
    mkdir -p "${WHEEL_OUT_DIR}"
    # Remove any existing wheel files
    rm -f "${WHEEL_OUT_DIR}"/*.whl
    mv "${WHEEL_FILE}" "${WHEEL_OUT_DIR}/${WHEEL_NAME}"
    echo ""
    echo "✓ Successfully built wheel package"
    echo "  Location: ${WHEEL_OUT_DIR}/${WHEEL_NAME}"
    echo ""
    echo "The wheel file is ready to be committed to the repository."
    echo "chutes-cvm installs it (into a venv on PATH) when nvidia-gpu-tools is not present."
else
    echo "Error: Could not find built wheel file"
    exit 1
fi

# Clean up build directory and any leftover source files
rm -rf "${BUILD_DIR}"
# Remove any source files that might have been left behind (but keep entry_point.py)
rm -rf "${TARGET_DIR}"/utils "${TARGET_DIR}"/gpu "${TARGET_DIR}"/pci "${TARGET_DIR}"/cli
rm -f "${TARGET_DIR}"/nvidia_gpu_tools.py "${TARGET_DIR}"/setup.py
rm -rf "${TARGET_DIR}"/build "${TARGET_DIR}"/dist "${TARGET_DIR}"/*.egg-info

echo ""
echo "✓ GPU admin tools bundled successfully"
echo "  Wheel file: ${WHEEL_OUT_DIR}/${WHEEL_NAME}"
echo ""
echo "Only the wheel file should be committed to the repository."
