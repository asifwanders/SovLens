# SovLens Backend Sidecar Binaries

Tauri requires platform-specific binaries named with the Rust target-triple suffix.
Place compiled backend executables here before running `npm run tauri build`.
The binary is only spawned in release builds — dev mode starts the Python server manually.

## Expected filenames

| Platform       | Filename                                             |
|----------------|------------------------------------------------------|
| macOS ARM64    | `sovlens-backend-aarch64-apple-darwin`               |
| macOS x86_64   | `sovlens-backend-x86_64-apple-darwin`                |
| Windows x86_64 | `sovlens-backend-x86_64-pc-windows-msvc.exe`         |
| Linux x86_64   | `sovlens-backend-x86_64-unknown-linux-gnu`           |

## How to build (PyInstaller)

### Prerequisites

```bash
# 1. Create & activate the backend virtualenv
cd backend
python3.11 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 2. Install runtime deps (respecting constraints)
pip install -c constraints.txt -r requirements.txt

# 3. Install build-only deps (PyInstaller — not shipped to end users)
pip install -r requirements-build.txt
```

### macOS / Linux

Run from repo root:

```bash
bash frontend/src-tauri/binaries/build.sh
```

The script:
1. Activates `backend/venv`
2. Runs `pyinstaller backend/sovlens-backend.spec --distpath dist --workpath build`
3. Copies `backend/dist/sovlens-backend` → `frontend/src-tauri/binaries/sovlens-backend-<triple>`

Target triples produced:

| Runner    | Triple                      |
|-----------|-----------------------------|
| macos-14  | `aarch64-apple-darwin`      |
| macos-13  | `x86_64-apple-darwin`       |

### Windows

Run from repo root (Command Prompt or pwsh):

```bat
frontend\src-tauri\binaries\build.bat
```

The script activates `backend\venv`, runs PyInstaller with the spec, then copies
`backend\dist\sovlens-backend.exe` → `frontend\src-tauri\binaries\sovlens-backend-x86_64-pc-windows-msvc.exe`.

On Windows, install CUDA torch **before** `requirements.txt` so the GPU wheel wins:

```powershell
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -c constraints.txt -r requirements.txt
```

### PyInstaller spec

`backend/sovlens-backend.spec` builds a single-file (`--onefile`) executable with:

- Hidden imports for all heavy runtime libraries (lancedb, sentence_transformers, easyocr,
  whisper, ultralytics, scenedetect, imageio_ffmpeg, pillow_heif, FastAPI/uvicorn).
- `console=False` on Windows — no terminal popup.
- Models are **not** bundled — they download to the user's cache at first run.

## Automated CI builds

`.github/workflows/release.yml` builds all three platform binaries and packages
the Tauri installer on every `v*.*.*` tag push. Artifacts are uploaded directly
to the GitHub Release via `tauri-apps/tauri-action`.

## Dev mode

The sidecar is only spawned in release builds (`cfg!(not(debug_assertions))`).
During development, start the Python backend manually:

```bash
cd backend && python main.py
# FastAPI listens on http://127.0.0.1:14793
```
