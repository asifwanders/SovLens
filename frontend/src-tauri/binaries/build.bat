@echo off
REM Build the sovlens-backend sidecar (onedir) for Windows x86_64, then pack
REM into a single zip that ships as a bundle resource.
REM Run from the repository root:
REM   frontend\src-tauri\binaries\build.bat
REM
REM Why a zip: see build.sh header. Tauri's bundle.resources walker chokes on
REM PyInstaller's symlink farms / nested onedir; a single archive shipped as
REM a resource sidesteps it and the Rust shell extracts on first launch.

setlocal EnableDelayedExpansion

REM Resolve repo root relative to this script's location (binaries\ is 3 levels deep).
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\..\..\"
set "REPO_ROOT=%CD%"
popd

set "BACKEND_DIR=%REPO_ROOT%\backend"
set "BINARIES_DIR=%REPO_ROOT%\frontend\src-tauri\binaries"
set "VENV_DIR=%BACKEND_DIR%\venv"

REM Resolve target triple (informational only — archive ships via
REM bundle.resources, not externalBin).
for /f "tokens=2" %%T in ('rustc -Vv ^| findstr /C:"host"') do set "TARGET_TRIPLE=%%T"

echo =^> Target triple: %TARGET_TRIPLE%
echo =^> Activating venv at %VENV_DIR%

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo ERROR: Could not activate venv at %VENV_DIR% >&2
    exit /b 1
)

echo =^> Running PyInstaller (spec: backend\sovlens-backend.spec)
pushd "%BACKEND_DIR%"
pyinstaller sovlens-backend.spec --distpath dist --workpath build --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller failed >&2
    popd
    exit /b 1
)
popd

REM PyInstaller onedir output: dist\sovlens-backend\{sovlens-backend.exe, _internal\...}
set "SRC_DIR=%BACKEND_DIR%\dist\sovlens-backend"
set "SRC_EXE=%SRC_DIR%\sovlens-backend.exe"
set "ARCHIVE=%BINARIES_DIR%\sovlens-backend.tar.gz"

if not exist "%SRC_DIR%" (
    echo ERROR: Expected onedir folder not found at %SRC_DIR% >&2
    exit /b 1
)
if not exist "%SRC_EXE%" (
    echo ERROR: Expected bootloader exe not found at %SRC_EXE% >&2
    exit /b 1
)

if not exist "%BINARIES_DIR%" mkdir "%BINARIES_DIR%"

REM Remove any stale archive first so a partial write doesn't fool the size guard.
if exist "%ARCHIVE%" del /F /Q "%ARCHIVE%"

REM tar.exe ships on Windows 10+ (since build 17063). -czf produces gzip
REM tarball; same format used by mac/linux build.sh so the Rust shell
REM only needs ONE extraction codepath. -C changes into the source
REM parent so the archive root is `sovlens-backend\` (not the full
REM PyInstaller dist path).
echo =^> Packing archive: %ARCHIVE%
tar -czf "%ARCHIVE%" -C "%BACKEND_DIR%\dist" sovlens-backend
if errorlevel 1 (
    echo ERROR: tar gzip pack failed >&2
    exit /b 1
)

if not exist "%ARCHIVE%" (
    echo ERROR: archive not produced at %ARCHIVE% >&2
    exit /b 1
)

REM Aggregate-size guard on the compressed zip. PyInstaller onedir with the
REM ONNX-only stack is ~150-250MB; zip lands ~80-130MB compressed. Guard at
REM 80MB so a broken (empty/partial) build can't slip through.
for %%S in ("%ARCHIVE%") do set "ARCHIVE_BYTES=%%~zS"
echo =^> Wrote: %ARCHIVE% (compressed: %ARCHIVE_BYTES% bytes)

if %ARCHIVE_BYTES% LSS 80000000 (
    echo ERROR: sidecar archive suspiciously small ^(^<80 MB compressed^) >&2
    exit /b 1
)

endlocal
