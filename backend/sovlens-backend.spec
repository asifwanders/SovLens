# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for SovLens backend sidecar.
# Build from repo root:
#   pyinstaller backend/sovlens-backend.spec
#
# Output: backend/dist/sovlens-backend  (or .exe on Windows)

import sys
import os

block_cipher = None

# ------------------------------------------------------------------
# Hidden imports — libraries that PyInstaller can't auto-detect
# because they are loaded at runtime, via plugins, or via C extensions.
# ------------------------------------------------------------------
HIDDEN_IMPORTS = [
    # LanceDB / Arrow
    "lancedb",
    "lancedb.table",
    "lancedb.index",
    "lancedb.embeddings",
    "pyarrow",
    "pyarrow.lib",
    "pyarrow._compute",
    "pyarrow._csv",
    "pyarrow._json",
    "pyarrow._parquet",
    # Sentence Transformers / HuggingFace
    "sentence_transformers",
    "sentence_transformers.models",
    "sentence_transformers.losses",
    "transformers",
    "transformers.models.clip",
    "tokenizers",
    # EasyOCR
    "easyocr",
    "easyocr.detection",
    "easyocr.recognition",
    # Whisper
    "whisper",
    "whisper.model",
    "whisper.audio",
    "whisper.tokenizer",
    # Ultralytics YOLO
    "ultralytics",
    "ultralytics.nn",
    "ultralytics.models",
    "ultralytics.utils",
    # SceneDetect
    "scenedetect",
    "scenedetect.detectors",
    "scenedetect.video_manager",
    # imageio / ffmpeg
    "imageio_ffmpeg",
    "imageio_ffmpeg._utils",
    # Pillow / HEIF
    "PIL",
    "PIL.Image",
    "pillow_heif",
    # FastAPI / Uvicorn
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
    # Torch / numpy
    "torch",
    "torchvision",
    "numpy",
    # Scipy
    "scipy",
    "scipy.spatial.transform._rotation_groups",
    # Misc stdlib / internal
    "email.mime.multipart",
    "email.mime.text",
    "pkg_resources.py2_warn",
    "anyio",
    "anyio._backends._asyncio",
    "starlette",
    "starlette.middleware.cors",
    "multipart",
    "httpx",
]

a = Analysis(
    # Entry point — relative to spec file location (backend/).
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the bundle lean — tests and scratch are never needed at runtime.
        "tests",
        "scratch",
        "pytest",
        "unittest",
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
    upx=True,
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
