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

## Auto-updater (tauri-plugin-updater)

SovLens uses Tauri's ed25519-signed updater. Updates are fetched from:
`https://github.com/asifwanders/SovLens/releases/latest/download/latest.json`

### Key generation (one-time, already done)

The ed25519 keypair was generated with:
```bash
python3 -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
# ... (see scripts/gen_updater_key.py for full script)
"
```

Private key is stored at `~/.tauri/sovlens-updater.key` — **never commit this file**.

Public key (already embedded in `tauri.conf.json`):
```
RWTePG/I/YHINrWhy6RwxVNNXDr3/qZ+B+1j4pgj+jAE/ae78XPs+7fO
```

### GitHub Actions secret

Add the **full contents** of `~/.tauri/sovlens-updater.key` as a GitHub secret named:
```
TAURI_SIGNING_PRIVATE_KEY
```

If `TAURI_SIGNING_PRIVATE_KEY` is not set in the workflow environment, the build will
succeed but update packages will not be signed. **Unsigned packages are rejected by the
updater in production** — users will be stuck on their current version until a signed
release is published.

### Release workflow

In `.github/workflows/release.yml`, ensure the build step passes:
```yaml
env:
  TAURI_SIGNING_PRIVATE_KEY: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}
  TAURI_SIGNING_PRIVATE_KEY_PASSWORD: ""   # empty = no password on key
```

Tauri's `tauri-action@v0` will auto-generate `latest.json` when the env is set.
The manifest format it produces:
```json
{
  "version": "v0.1.0",
  "notes": "Release notes here",
  "pub_date": "2026-05-21T00:00:00Z",
  "platforms": {
    "darwin-aarch64": {
      "signature": "<minisign sig>",
      "url": "https://github.com/asifwanders/SovLens/releases/download/v0.1.0/SovLens_0.1.0_aarch64.dmg.tar.gz"
    },
    "darwin-x86_64": { "signature": "...", "url": "..." },
    "windows-x86_64": { "signature": "...", "url": "..." }
  }
}
```

### Key loss warning

If the private key is lost, you must generate a new keypair, update `plugins.updater.pubkey`
in `tauri.conf.json`, and ship a new release. **Users on old versions will not auto-update
to a release signed with the new key** — they must reinstall manually.

## Cross-platform notes

- The `reveal_in_explorer` Tauri command uses `explorer.exe /select,` on Windows,
  `open -R` on macOS, and `xdg-open <parent>` on Linux — no frontend changes needed
  when targeting new platforms.
- The `@tauri-apps/plugin-os` runtime call auto-adjusts the button label in the UI.
