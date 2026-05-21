@echo off
REM Build the sovlens-backend sidecar for Windows x86_64.
REM Run from the repository root:
REM   frontend\src-tauri\binaries\build.bat

setlocal EnableDelayedExpansion

REM Resolve repo root relative to this script's location (binaries\ is 3 levels deep).
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\..\..\"
set "REPO_ROOT=%CD%"
popd

set "BACKEND_DIR=%REPO_ROOT%\backend"
set "BINARIES_DIR=%REPO_ROOT%\frontend\src-tauri\binaries"
set "VENV_DIR=%BACKEND_DIR%\venv"

REM Resolve target triple via rustc.
for /f "tokens=2" %%T in ('rustc -Vv ^| findstr /C:"host"') do set "TARGET_TRIPLE=%%T"
set "BINARY_NAME=sovlens-backend-%TARGET_TRIPLE%.exe"

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

set "SRC=%BACKEND_DIR%\dist\sovlens-backend.exe"
set "DST=%BINARIES_DIR%\%BINARY_NAME%"

if not exist "%SRC%" (
    echo ERROR: Expected binary not found at %SRC% >&2
    exit /b 1
)

copy /Y "%SRC%" "%DST%"
echo =^> Wrote: %DST%
endlocal
