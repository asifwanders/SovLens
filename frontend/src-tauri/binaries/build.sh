#!/usr/bin/env bash
# Build the sovlens-backend sidecar (onedir) for the current macOS/Linux platform,
# then pack the onedir into a single tarball that ships as a bundle resource.
# Run from the repository root:
#   bash frontend/src-tauri/binaries/build.sh
#
# Why a tarball: Tauri's bundle.resources walks the tree and re-registers every
# file. PyInstaller onedir contains PIL/.dylibs + torch/.dylibs symlink farms,
# and even with `cp -RL` to dereference, the resource pass kept failing with
# `File exists (os error 17)` due to duplicate registrations. A single archive
# file sidesteps the entire walker problem; the Rust shell extracts it once on
# first launch into the user's app-data dir.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
BINARIES_DIR="${REPO_ROOT}/frontend/src-tauri/binaries"
VENV_DIR="${BACKEND_DIR}/venv"

TARGET_TRIPLE=$(rustc -Vv | grep host | cut -d' ' -f2)

echo "==> Target triple: ${TARGET_TRIPLE}"
echo "==> Activating venv at ${VENV_DIR}"

# Activate the backend virtualenv (created by: python -m venv backend/venv)
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"

echo "==> Running PyInstaller (spec: backend/sovlens-backend.spec)"
# PyInstaller resolves relative paths from the spec's directory.
cd "${BACKEND_DIR}"
pyinstaller sovlens-backend.spec --distpath dist --workpath build --noconfirm
cd "${REPO_ROOT}"

# PyInstaller onedir output: dist/sovlens-backend/{sovlens-backend, _internal/...}
SRC_DIR="${BACKEND_DIR}/dist/sovlens-backend"
SRC_EXE="${SRC_DIR}/sovlens-backend"
TARBALL="${BINARIES_DIR}/sovlens-backend.tar.gz"

if [[ ! -d "${SRC_DIR}" ]]; then
    echo "ERROR: Expected onedir output not found at ${SRC_DIR}" >&2
    exit 1
fi
if [[ ! -f "${SRC_EXE}" ]]; then
    echo "ERROR: Expected bootloader not found at ${SRC_EXE}" >&2
    exit 1
fi

mkdir -p "${BINARIES_DIR}"

# Pack the onedir into a single gzipped tar.
# `-h` dereferences symlinks so PIL/.dylibs + torch/.dylibs symlink farms become
# real files inside the archive — eliminates double-extraction collisions on
# the consumer side and matches what `cp -RL` used to do at copy time.
echo "==> Packing tarball: ${TARBALL}"
rm -f "${TARBALL}"
cd "${BACKEND_DIR}/dist"
tar -chzf "${TARBALL}" sovlens-backend
cd "${REPO_ROOT}"

if [[ ! -f "${TARBALL}" ]]; then
    echo "ERROR: tarball not produced at ${TARBALL}" >&2
    exit 1
fi

# Aggregate-size guard on the COMPRESSED tarball. Onedir is ~150-250 MB raw;
# gzip compresses ~30-50%, so the tarball lands ~80-130 MB. We guard at 80 MB
# so a broken (empty/partial) PyInstaller build can't slip through.
tarball_size=$(stat -f%z "${TARBALL}" 2>/dev/null || stat -c%s "${TARBALL}")
echo "==> Wrote: ${TARBALL} (${tarball_size} bytes compressed)"
if [ "${tarball_size}" -lt 80000000 ]; then
    echo "ERROR: sidecar tarball suspiciously small (<80 MB compressed)" >&2
    exit 1
fi
