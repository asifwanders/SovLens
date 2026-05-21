# SovLens Backend Sidecar Binaries

Tauri requires platform-specific binaries named with the target triple suffix.
Place the compiled backend executables here before running `npm run tauri build`.

## Expected filenames

| Platform         | Filename                                              |
|------------------|-------------------------------------------------------|
| macOS ARM64      | `sovlens-backend-aarch64-apple-darwin`               |
| macOS x86_64     | `sovlens-backend-x86_64-apple-darwin`                |
| Windows x86_64   | `sovlens-backend-x86_64-pc-windows-msvc.exe`         |
| Linux x86_64     | `sovlens-backend-x86_64-unknown-linux-gnu`           |

## How to build (PyInstaller)

Run the appropriate build script from the repository root, or manually:

```bash
# macOS / Linux
pyinstaller --onefile backend/main.py --name sovlens-backend
cp dist/sovlens-backend src-tauri/binaries/sovlens-backend-$(rustc -Vv | grep host | cut -d' ' -f2)

# Windows (PowerShell)
pyinstaller --onefile backend/main.py --name sovlens-backend
copy dist\sovlens-backend.exe src-tauri\binaries\sovlens-backend-x86_64-pc-windows-msvc.exe
```

## Dev mode

The sidecar is only spawned in release builds (`cfg!(not(debug_assertions))`).
During development, start the Python backend manually:
```bash
cd backend && python main.py
```
