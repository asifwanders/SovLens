#!/usr/bin/env bash
# Build the sovlens-backend sidecar (onedir) for the current macOS/Linux platform.
# Run from the repository root:
#   bash frontend/src-tauri/binaries/build.sh
#
# Onedir vs onefile: see comments in backend/sovlens-backend.spec. Short
# version: onefile produced a ~1 GB EXE that NSIS could not mmap on Win CI.
# Onedir splits into many smaller files. The whole folder ships via
# bundle.resources in tauri.conf.json and is launched manually from Rust.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
BINARIES_DIR="${REPO_ROOT}/frontend/src-tauri/binaries"
VENV_DIR="${BACKEND_DIR}/venv"

TARGET_TRIPLE=$(rustc -Vv | grep host | cut -d' ' -f2)
# Onedir is shipped via bundle.resources (not externalBin), so we no longer
# need Tauri's target-triple suffix convention. A stable folder name keeps
# tauri.conf.json's resources glob simple and per-runner-portable.
DEST_DIR_NAME="sovlens-backend"

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
DST_DIR="${BINARIES_DIR}/${DEST_DIR_NAME}"

if [[ ! -d "${SRC_DIR}" ]]; then
    echo "ERROR: Expected onedir output not found at ${SRC_DIR}" >&2
    exit 1
fi
if [[ ! -f "${SRC_EXE}" ]]; then
    echo "ERROR: Expected bootloader not found at ${SRC_EXE}" >&2
    exit 1
fi

# Replace destination atomically: rm first so leftover stale files from a
# previous build don't linger and bloat the bundle / get out of sync.
rm -rf "${DST_DIR}"
mkdir -p "${DST_DIR}"
# -a preserves perms + symlinks (libtorch ships .dylib symlinks on mac).
cp -a "${SRC_DIR}/." "${DST_DIR}/"
chmod +x "${DST_DIR}/sovlens-backend"

# Total-folder size guard. The onedir output is many files; the loader EXE
# itself is tiny (~1-2 MB on Unix). We guard the aggregate to catch a
# broken build that produced an empty/partial _internal/.
total_size=$(du -sk "${DST_DIR}" | awk '{print $1 * 1024}')
echo "==> Wrote: ${DST_DIR} (${total_size} bytes total)"
if [ "${total_size}" -lt 200000000 ]; then
    echo "ERROR: sidecar onedir suspiciously small (<200 MB total)" >&2
    exit 1
fi
