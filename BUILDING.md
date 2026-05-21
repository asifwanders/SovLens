# Building SovLens

## Development mode (no sidecar needed)

```bash
# Terminal 1 — Python backend
cd backend
python main.py          # listens on port 14793

# Terminal 2 — Tauri dev
cd frontend
npm run tauri dev
```

The Rust code skips sidecar spawn when `debug_assertions` is enabled (i.e., all dev builds).

## Production build

### 1. Build the Python sidecar

macOS / Linux:
```bash
cd frontend/src-tauri/binaries
./build.sh              # auto-detects target triple, copies binary here
```

Windows (PowerShell from repo root):
```bat
cd frontend\src-tauri\binaries
build.bat
```

This uses PyInstaller to produce a single-file executable and copies it to
`frontend/src-tauri/binaries/` with the correct Tauri triple-suffix name.

Required binary names by platform:
- `sovlens-backend-aarch64-apple-darwin`
- `sovlens-backend-x86_64-apple-darwin`
- `sovlens-backend-x86_64-pc-windows-msvc.exe`
- `sovlens-backend-x86_64-unknown-linux-gnu`

### 2. Bundle the Tauri app

```bash
cd frontend
npm run tauri build
```

The sidecar binary is bundled automatically via `bundle.externalBin` in `tauri.conf.json`.
On first launch the sidecar is spawned by Tauri and listens on port 14793.

## Cross-platform notes

- The `reveal_in_explorer` Tauri command uses `explorer.exe /select,` on Windows,
  `open -R` on macOS, and `xdg-open <parent>` on Linux — no frontend changes needed
  when targeting new platforms.
- The `@tauri-apps/plugin-os` runtime call auto-adjusts the button label in the UI.
