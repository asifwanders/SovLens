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

# ONNX Runtime stack — use collect_all so every lazy submodule, data file,
# and native DLL/.so/.dylib is bundled. Hand-listing reliably misses things
# like ort's CoreML/DirectML EP shared libs, ctranslate2's runtime libs,
# rapidocr's bundled ONNX model files, and huggingface_hub's cache helpers.
# Wrap each in try/except so a single missing/broken hook can't sink the
# whole build silently — PyInstaller would otherwise crash mid-spec and
# leave dist/ empty, which build.bat would then fail loudly on.
# Track every failed collect_all() so we can hard-fail the build at the
# bottom of this spec. A swallowed failure (broken hook, version skew)
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

ort_datas, ort_binaries, ort_hidden = _safe_collect("onnxruntime")
fw_datas, fw_binaries, fw_hidden = _safe_collect("faster_whisper")
ct_datas, ct_binaries, ct_hidden = _safe_collect("ctranslate2")
ro_datas, ro_binaries, ro_hidden = _safe_collect("rapidocr_onnxruntime")
hh_datas, hh_binaries, hh_hidden = _safe_collect("huggingface_hub")
# hf_xet is a separate wheel that huggingface_hub probes at runtime —
# without an explicit collect_all PyInstaller misses it entirely, leaving
# end users on the slow Xet-bridge fallback (the source of the "Xet
# Storage is enabled... hf_xet not installed" log spam + the ~500 MB
# Whisper download stalls).
xet_datas, xet_binaries, xet_hidden = _safe_collect("hf_xet")
tk_datas, tk_binaries, tk_hidden = _safe_collect("tokenizers")
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
    # ONNX Runtime — capi is the C extension loaded lazily.
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi._pybind_state",
    # faster-whisper + CTranslate2
    "faster_whisper",
    "ctranslate2",
    # RapidOCR (ships ONNX models internally)
    "rapidocr_onnxruntime",
    # HuggingFace hub (model download for CLIP ONNX bundle)
    "huggingface_hub",
    # Xet transport — huggingface_hub picks it up by name at runtime
    # via importlib.metadata, so PyInstaller's static analysis misses it
    # without an explicit hidden import.
    "hf_xet",
    # Tokenizers (CLIP text encoder)
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
    # ASGI / HTTP
    "anyio",
    "anyio._backends._asyncio",
    "starlette",
    "starlette.middleware.cors",
    "multipart",
    "httpx",
    # Stdlib modules PyInstaller's modulegraph marks "conditional" but some
    # transitive deps actually import. Force-include defensively.
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
] + ort_hidden + fw_hidden + ct_hidden + ro_hidden + hh_hidden + xet_hidden + tk_hidden + ld_hidden + sd_hidden + ff_hidden

# Static model assets that ship inside the bundle. yolov8n.onnx is
# pre-downloaded by CI (release.yml) before this spec runs — the file
# Xenova/yolov8n on HF Hub started returning 401 in May 2026, so a
# bundled copy is the only reliable source for end users. Skip silently
# when missing so dev runs without a pre-downloaded file can still bake
# a binary (yolo_detect falls back to a runtime URL fetch).
_local_models = []
_models_dir = os.path.join(os.path.dirname(SPEC), "models")
_yolo_local = os.path.join(_models_dir, "yolov8n.onnx")
if os.path.isfile(_yolo_local) and os.path.getsize(_yolo_local) > 9 * 1024 * 1024:
    _local_models.append((_yolo_local, "models"))
    print(f"INFO bundling {_yolo_local} -> models/yolov8n.onnx")
else:
    print(f"WARN no bundled yolov8n.onnx at {_yolo_local} — runtime will fetch on first use")

a = Analysis(
    # Entry point — relative to spec file location (backend/).
    ["main.py"],
    pathex=[],
    binaries=(
        ort_binaries + fw_binaries + ct_binaries + ro_binaries
        + hh_binaries + xet_binaries + tk_binaries + ld_binaries
        + sd_binaries + ff_binaries
    ),
    datas=(
        ort_datas + fw_datas + ct_datas + ro_datas
        + hh_datas + xet_datas + tk_datas + ld_datas + sd_datas
        + ff_datas + _local_models
    ),
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the bundle lean — tests and scratch are never needed at runtime.
        # Also exclude any straggling torch/transformers/ultralytics that may
        # land in the venv via dev tools; they are not used at runtime now.
        "tests",
        "scratch",
        "pytest",
        "IPython",
        "ipykernel",
        "notebook",
        "matplotlib",
        "tkinter",
        "torch",
        "torchvision",
        "transformers",
        "sentence_transformers",
        "ultralytics",
        "easyocr",
        "whisper",  # legacy openai-whisper — replaced by faster-whisper
        "scipy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ------------------------------------------------------------------
# onedir mode (exclude_binaries=True + COLLECT).
#
# Why onedir over onefile:
#   * Onefile produces a single large EXE that NSIS must mmap to embed.
#     On Windows-latest CI this fails ("failed creating mmap of ...
#     sovlens-backend.exe", os error 2) intermittently even with
#     Defender exclusions — the file is just too large and AV scan
#     timing windows are unreliable.
#   * Onedir splits into many <50 MB files. NSIS streams each via
#     normal File directives. No mmap pressure.
#   * Onefile also extracts every DLL to %TEMP% on each launch
#     (5-60 s on AV-scanned Win). Onedir launches instantly.
#
# Output layout:
#   dist/sovlens-backend/
#     sovlens-backend(.exe)     # bootloader EXE
#     _internal/                # all DLLs, .so, .dylib, data files
#       ...
# ------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sovlens-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx=True triggers Windows Defender heuristic + can corrupt native
    # DLLs. Net <5% size gain; disabled.
    upx=False,
    upx_exclude=[],
    # console=False suppresses the terminal popup on Windows.
    # On macOS/Linux this has no visible effect.
    console=sys.platform != "win32",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="sovlens-backend",
)
