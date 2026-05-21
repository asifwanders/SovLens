@echo off
REM Build the sovlens-backend sidecar for Windows x86_64.
REM Run from the repository root.

set BACKEND_DIR=%1
if "%BACKEND_DIR%"=="" set BACKEND_DIR=backend
set BINARIES_DIR=%~dp0
set BINARY_NAME=sovlens-backend-x86_64-pc-windows-msvc.exe

echo Building Python sidecar for Windows x86_64...
cd /d "%BACKEND_DIR%"
pyinstaller --onefile main.py --name sovlens-backend --distpath dist
cd /d "%~dp0..\.."

copy /Y "%BACKEND_DIR%\dist\sovlens-backend.exe" "%BINARIES_DIR%%BINARY_NAME%"
echo Wrote: %BINARIES_DIR%%BINARY_NAME%
