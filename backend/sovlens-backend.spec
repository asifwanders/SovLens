# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for SovLens backend sidecar.
# Build from repo root:
#   pyinstaller backend/sovlens-backend.spec
#
# Output: backend/dist/sovlens-backend  (or .exe on Windows)

import sys
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Torch + adjacent: use collect_all so every lazy submodule, data file,
# and native DLL is bundled. Hand-listing reliably misses things like
# torch._dynamo, torch.distributed.config, MKL DLLs on Win, etc.
# Wrap each in try/except so a single missing/broken hook can't sink the
# whole build silently — PyInstaller would otherwise crash mid-spec and
# leave dist/ empty, which build.bat would then fail loudly on.
# Track every failed collect_all() so we can hard-fail the build at the
# bottom of this spec. A swallowed failure (broken hook, torch version skew)
# previously yielded a binary missing critical DLLs that still passed the
# 10MB build.bat size guard. Override only via env SOVLENS_ALLOW_PARTIAL_COLLECT=1.
_COLLECT_FAILURES = []

def _safe_collect(name):
    try:
        return collect_all(name)
    except Exception as exc:
        print(f"WARN collect_all({name}) failed: {exc!r}")
        _COLLECT_FAILURES.append((name, repr(exc)))
        return ([], [], [])

torch_datas, torch_binaries, torch_hidden = _safe_collect("torch")
tv_datas, tv_binaries, tv_hidden = _safe_collect("torchvision")
tr_datas, tr_binaries, tr_hidden = _safe_collect("transformers")
st_datas, st_binaries, st_hidden = _safe_collect("sentence_transformers")
ul_datas, ul_binaries, ul_hidden = _safe_collect("ultralytics")
eo_datas, eo_binaries, eo_hidden = _safe_collect("easyocr")
wh_datas, wh_binaries, wh_hidden = _safe_collect("whisper")
ld_datas, ld_binaries, ld_hidden = _safe_collect("lancedb")
sd_datas, sd_binaries, sd_hidden = _safe_collect("scenedetect")
ff_datas, ff_binaries, ff_hidden = _safe_collect("imageio_ffmpeg")

if _COLLECT_FAILURES and not os.environ.get("SOVLENS_ALLOW_PARTIAL_COLLECT"):
    _msg = "; ".join(f"{n}: {e}" for n, e in _COLLECT_FAILURES)
    raise SystemExit(
        f"FATAL: PyInstaller collect_all() failed for: {_msg}. "
        "Set SOVLENS_ALLOW_PARTIAL_COLLECT=1 to override (NOT recommended for releases)."
    )

# ------------------------------------------------------------------
# Hidden imports — libraries that PyInstaller can't auto-detect
# because they are loaded at runtime, via plugins, or via C extensions.
# ------------------------------------------------------------------
HIDDEN_IMPORTS = [
    # pyarrow (LanceDB picked up via collect_all)
    "pyarrow",
    "pyarrow.lib",
    "pyarrow._compute",
    "pyarrow._csv",
    "pyarrow._json",
    "pyarrow._parquet",
    # tokenizers (transformers picked up via collect_all)
    "tokenizers",
    # imageio / ffmpeg
    "imageio_ffmpeg",
    "imageio_ffmpeg._utils",
    # Pillow / HEIF
    "PIL",
    "PIL.Image",
    "pillow_heif",
    # FastAPI / Uvicorn — uvicorn picks worker types at runtime
    "fastapi",
    "fastapi.middleware.cors",
    "uvicorn",
    "uvicorn.main",
    "uvicorn.lifespan.on",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    # Pydantic
    "pydantic",
    "pydantic.v1",
    # OpenCV (headless)
    "cv2",
    "numpy",
    # Scipy
    "scipy",
    "scipy.spatial.transform._rotation_groups",
    # ASGI / HTTP
    "anyio",
    "anyio._backends._asyncio",
    "starlette",
    "starlette.middleware.cors",
    "multipart",
    "httpx",
    # Stdlib modules PyInstaller's modulegraph marks "conditional" but
    # torch/transformers/ultralytics actually import. Force-include.
    "unittest",
    "unittest.mock",
    "unittest.case",
    "unittest.util",
    "pkg_resources",
    "pkg_resources.extern",
    "pkg_resources.py2_warn",
    "pydoc",
    "doctest",
    "inspect",
    "sqlite3",
    "importlib.metadata",
    "importlib.resources",
    "xml.etree.ElementTree",
    "csv",
    "shelve",
    "email.mime.multipart",
    "email.mime.text",
] + torch_hidden + tv_hidden + tr_hidden + st_hidden + ul_hidden + eo_hidden + wh_hidden + ld_hidden + sd_hidden + ff_hidden

a = Analysis(
    # Entry point — relative to spec file location (backend/).
    ["main.py"],
    pathex=[],
    binaries=(
        torch_binaries + tv_binaries + tr_binaries + st_binaries
        + ul_binaries + eo_binaries + wh_binaries + ld_binaries + sd_binaries
        + ff_binaries
    ),
    datas=(
        torch_datas + tv_datas + tr_datas + st_datas
        + ul_datas + eo_datas + wh_datas + ld_datas + sd_datas
        + ff_datas
    ),
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the bundle lean — tests and scratch are never needed at runtime.
        # CRITICAL: do NOT exclude "unittest" — torch.utils._config_module
        # imports it at module load and the frozen exe will crash on start.
        "tests",
        "scratch",
        "pytest",
        "IPython",
        "ipykernel",
        "notebook",
        "matplotlib",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="sovlens-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx=True triggers Windows Defender heuristic + corrupts some torch
    # DLLs on Win. Net <5% size gain; disabled.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=False suppresses the terminal popup on Windows.
    # On macOS/Linux this has no visible effect.
    console=sys.platform != "win32",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
