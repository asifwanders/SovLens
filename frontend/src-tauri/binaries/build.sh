#!/usr/bin/env bash
# Build the sovlens-backend sidecar for the current macOS/Linux platform.
# Run from the repository root.
set -euo pipefail

BACKEND_DIR="${1:-backend}"
BINARIES_DIR="$(dirname "$0")"
TARGET_TRIPLE=$(rustc -Vv | grep host | cut -d' ' -f2)
BINARY_NAME="sovlens-backend-${TARGET_TRIPLE}"

echo "Building Python sidecar for ${TARGET_TRIPLE}..."
cd "${BACKEND_DIR}"
pyinstaller --onefile main.py --name sovlens-backend --distpath dist
cd -

cp "${BACKEND_DIR}/dist/sovlens-backend" "${BINARIES_DIR}/${BINARY_NAME}"
chmod +x "${BINARIES_DIR}/${BINARY_NAME}"
echo "Wrote: ${BINARIES_DIR}/${BINARY_NAME}"
