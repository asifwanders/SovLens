#!/usr/bin/env bash
# Build the sovlens-backend sidecar for the current macOS/Linux platform.
# Run from the repository root:
#   bash frontend/src-tauri/binaries/build.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
BINARIES_DIR="${REPO_ROOT}/frontend/src-tauri/binaries"
VENV_DIR="${BACKEND_DIR}/venv"

TARGET_TRIPLE=$(rustc -Vv | grep host | cut -d' ' -f2)
BINARY_NAME="sovlens-backend-${TARGET_TRIPLE}"

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

SRC="${BACKEND_DIR}/dist/sovlens-backend"
DST="${BINARIES_DIR}/${BINARY_NAME}"

if [[ ! -f "${SRC}" ]]; then
    echo "ERROR: Expected binary not found at ${SRC}" >&2
    exit 1
fi

cp "${SRC}" "${DST}"
chmod +x "${DST}"
size=$(stat -f%z "${DST}" 2>/dev/null || stat -c%s "${DST}")
echo "==> Wrote: ${DST} (${size} bytes)"
if [ "${size}" -lt 10000000 ]; then
    echo "ERROR: sidecar binary suspiciously small (<10 MB)" >&2
    exit 1
fi
