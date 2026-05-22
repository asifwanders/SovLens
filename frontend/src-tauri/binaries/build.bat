@echo off
REM Build the sovlens-backend sidecar (onedir) for Windows x86_64.
REM Run from the repository root:
REM   frontend\src-tauri\binaries\build.bat
REM
REM Onedir vs onefile: see comments in backend\sovlens-backend.spec. Short
REM version: onefile produced a ~1 GB EXE that NSIS could not mmap on
REM windows-latest CI. Onedir splits into many smaller files; NSIS handles
REM them via individual File directives without the mmap wall.

setlocal EnableDelayedExpansion

REM Resolve repo root relative to this script's location (binaries\ is 3 levels deep).
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\..\..\"
set "REPO_ROOT=%CD%"
popd

set "BACKEND_DIR=%REPO_ROOT%\backend"
set "BINARIES_DIR=%REPO_ROOT%\frontend\src-tauri\binaries"
set "VENV_DIR=%BACKEND_DIR%\venv"

REM Resolve target triple (informational only — onedir ships via
REM bundle.resources, not externalBin, so we no longer need the
REM triple suffix on the destination folder).
for /f "tokens=2" %%T in ('rustc -Vv ^| findstr /C:"host"') do set "TARGET_TRIPLE=%%T"
set "DEST_DIR_NAME=sovlens-backend"

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
set "DST_DIR=%BINARIES_DIR%\%DEST_DIR_NAME%"

if not exist "%SRC_DIR%" (
    echo ERROR: Expected onedir folder not found at %SRC_DIR% >&2
    exit /b 1
)
if not exist "%SRC_EXE%" (
    echo ERROR: Expected bootloader exe not found at %SRC_EXE% >&2
    exit /b 1
)

REM Wipe stale destination so old files from a previous build don't linger.
if exist "%DST_DIR%" rmdir /S /Q "%DST_DIR%"
mkdir "%DST_DIR%"

REM xcopy /E recurse, /I treat dst as dir, /Y overwrite, /H include hidden,
REM /K preserve attrs (read-only matters for some torch DLLs).
xcopy /E /I /Y /H /K "%SRC_DIR%" "%DST_DIR%" >nul
if errorlevel 1 (
    echo ERROR: xcopy of onedir failed >&2
    exit /b 1
)

if not exist "%DST_DIR%\sovlens-backend.exe" (
    echo ERROR: dst exe missing post-copy ^(likely Defender^): %DST_DIR%\sovlens-backend.exe >&2
    exit /b 1
)

REM Aggregate-size guard. Defender real-time scanning has been observed
REM quarantining individual DLLs between write and read; an empty/partial
REM onedir would slip past a per-file check, so we sum the folder.
set "TOTAL_BYTES=0"
for /f "tokens=*" %%S in ('dir /S /-C "%DST_DIR%" ^| findstr /C:"File(s)"') do (
    for /f "tokens=3" %%B in ("%%S") do set "TOTAL_BYTES=%%B"
)
echo =^> Wrote: %DST_DIR% (total: %TOTAL_BYTES% bytes)

REM 200 MB lower bound — torch + CUDA wheel alone is ~700 MB, but be lenient
REM in case the layout changes. A real build is ~1.5-2 GB.
if %TOTAL_BYTES% LSS 200000000 (
    echo ERROR: sidecar onedir suspiciously small ^(^<200 MB total^) >&2
    exit /b 1
)

endlocal
