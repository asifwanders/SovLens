# Releasing SovLens

## Before you push a release tag

CI takes ~30 minutes per matrix entry. Run the local validation harness first
— it catches roughly 80% of red CI builds in under two minutes
(or under ten minutes with `cargo check`).

```bash
./scripts/validate.sh
# or
npm run validate          # from repo root
SKIP_CARGO_CHECK=1 ./scripts/validate.sh   # skip the slow Rust check
```

The harness runs:

| step | what it catches |
|------|-----------------|
| NSIS hook lint (if `makensis` installed) | typos in `frontend/src-tauri/windows/*.nsh` |
| GitHub Actions YAML lint (`actionlint` if installed, else YAML parse) | broken `release.yml` |
| PyInstaller spec parse | syntax errors in `backend/sovlens-backend.spec` |
| `python3 -m py_compile backend/*.py` | Python syntax errors |
| `npm run lint` | ESLint / Next.js lint errors |
| `tsc --noEmit` | TypeScript type errors |
| `cargo check --release` | Rust compile errors (slowest step; skippable) |
| JSON validate | malformed `tauri.conf.json`, capability files, `package.json` |

Optional tools to install for full coverage:

```bash
brew install makensis actionlint
```

## Cutting a tag

```bash
git tag -a v0.1.1 -m "..."
git push origin v0.1.1
```

If you need to retag after a fix:

```bash
git tag -d v0.1.1
git push origin :refs/tags/v0.1.1
git push
git tag -a v0.1.1 -m "..."
git push origin v0.1.1
```

## Manual CI run

The release workflow also supports `workflow_dispatch`, so CI changes can
be tested from the GitHub Actions UI without retagging. Note that when
fired manually, `github.ref_name` will be the branch name rather than a
version tag — useful for smoke-testing the build matrix, but not for
shipping a real release.
