"""Cross-platform utility helpers for SovLens.

Provides stable abstractions over OS differences (paths, encodings, hardware
acceleration, subprocess encoding) so callers never need sys.platform guards.

Python 3.9+.  No new pip dependencies.
"""

import os
import sys
import tempfile
import subprocess
from typing import List, Optional, Tuple  # noqa: F401 (Tuple exported for callers)

# ---------------------------------------------------------------------------
# Platform flags
# ---------------------------------------------------------------------------

IS_WINDOWS: bool = sys.platform == "win32"
IS_MACOS: bool = sys.platform == "darwin"
IS_LINUX: bool = not IS_WINDOWS and not IS_MACOS


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_LEGACY_APP_DATA = os.path.join(os.path.expanduser("~"), ".sovlens")
_app_data_migrated = False


def get_app_data_dir() -> str:
    """%LOCALAPPDATA%\\SovLens on Win, ~/Library/Application Support/SovLens on mac, ~/.sovlens on Linux.

    Auto-migrates content from legacy ~/.sovlens on first call (mac+Win) so users
    who installed before WS-X2 path migration don't lose folders.json / progress.json.
    """
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        target = os.path.join(base, "SovLens")
    elif IS_MACOS:
        target = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "SovLens")
    else:
        # Linux already uses ~/.sovlens; no migration needed.
        return _LEGACY_APP_DATA

    # One-shot migration: copy files from ~/.sovlens that don't already exist in target.
    global _app_data_migrated
    if not _app_data_migrated:
        _app_data_migrated = True
        try:
            if os.path.isdir(_LEGACY_APP_DATA):
                os.makedirs(target, exist_ok=True)
                for name in os.listdir(_LEGACY_APP_DATA):
                    src = os.path.join(_LEGACY_APP_DATA, name)
                    dst = os.path.join(target, name)
                    if not os.path.exists(dst):
                        try:
                            if os.path.isdir(src):
                                import shutil
                                shutil.copytree(src, dst)
                            else:
                                import shutil
                                shutil.copy2(src, dst)
                            print(f"[platform_utils] Migrated legacy app data: {name}")
                        except Exception as e:
                            print(f"[platform_utils] Migrate failed for {name}: {e}")
        except Exception as e:
            print(f"[platform_utils] Legacy data migration scan failed: {e}")

    return target


def get_temp_dir() -> str:
    """Return the system temporary directory via tempfile.gettempdir()."""
    return tempfile.gettempdir()


def normalize_path(p: str) -> str:
    """Canonical storage form: os.path.normcase(os.path.abspath(p)).

    On macOS/Linux normcase is identity (case preserved).
    On Windows it lower-cases and converts forward-slashes to backslashes.
    Use this for all path values stored in LanceDB and for comparison keys.

    On Windows we also strip the ``\\\\?\\`` extended-length / UNC prefix so
    the same physical file always normalises to the same row. Without this
    one caller using GetFinalPathNameByHandle and another using a relative
    path land as two distinct entries in LanceDB.
    """
    if IS_WINDOWS and isinstance(p, str):
        if p.startswith("\\\\?\\UNC\\"):
            # \\?\UNC\server\share\... -> \\server\share\...
            p = "\\\\" + p[len("\\\\?\\UNC\\"):]
        elif p.startswith("\\\\?\\"):
            p = p[len("\\\\?\\"):]
    return os.path.normcase(os.path.abspath(p))


# ---------------------------------------------------------------------------
# Hardware acceleration
# ---------------------------------------------------------------------------

def detect_torch_device() -> str:
    """_legacy_ — return 'cuda', 'mps', or 'cpu' based on available torch backends.

    Kept for backward compatibility with any out-of-tree callers. The SovLens
    backend no longer ships torch, so this almost always returns 'cpu' inside
    the frozen sidecar. New code should use ``get_onnx_providers()`` instead.
    """
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


def get_onnx_providers() -> List[str]:
    """Return the ONNX Runtime execution provider list in priority order.

    Filtered against what the local `onnxruntime` build actually exposes via
    `get_available_providers()`. Falls back to `["CPUExecutionProvider"]` if
    nothing else is available (should never happen — CPU EP is always present).

    Priority by platform:
      * macOS   : CoreML, CPU
      * Windows : CUDA, DirectML, CPU
      * Linux   : CUDA, CPU

    onnxruntime import is deferred so this module still loads if the package
    is missing (caller will hit ImportError when actually trying to use ORT).
    """
    if IS_MACOS:
        preferred = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    elif IS_WINDOWS:
        preferred = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    else:
        preferred = ["CUDAExecutionProvider", "CPUExecutionProvider"]

    try:
        import onnxruntime as ort  # type: ignore
        available = set(ort.get_available_providers())
    except Exception:
        available = set()

    final = [p for p in preferred if p in available]
    if not final:
        # CPU EP always ships with onnxruntime; if it's somehow missing the
        # caller will fail at session creation with a clear message.
        return ["CPUExecutionProvider"]
    return final


# ---------------------------------------------------------------------------
# ffmpeg hw encoder/decoder detection (probed once at import, cached)
# ---------------------------------------------------------------------------

def _probe_ffmpeg_encoders() -> str:
    """Return the raw stdout from ``ffmpeg -encoders``, or '' on failure."""
    try:
        ff = get_ffmpeg_exe()
        result = run_subprocess([ff, "-encoders"], timeout=15)
        return result.stdout
    except Exception:
        return ""


_FFMPEG_ENCODER_LIST: str = _probe_ffmpeg_encoders()  # cached at import time


def detect_hwaccel_encoder() -> List[str]:
    """Return ffmpeg output args for the best available H.264 encoder.

    Priority:
    * Windows + CUDA  -> h264_nvenc
    * macOS           -> h264_videotoolbox
    * Else            -> libx264 ultrafast

    Result is derived from the encoder list probed at import time.
    """
    if IS_WINDOWS and "h264_nvenc" in _FFMPEG_ENCODER_LIST:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23"]
    if IS_MACOS and "h264_videotoolbox" in _FFMPEG_ENCODER_LIST:
        return ["-c:v", "h264_videotoolbox", "-b:v", "5M"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]


def detect_hwaccel_decoder() -> List[str]:
    """Return ffmpeg input args (before -i) for hardware-accelerated decode.

    * Windows + CUDA  -> ['-hwaccel', 'cuda']
    * macOS           -> ['-hwaccel', 'videotoolbox']
    * Else            -> []
    """
    if IS_WINDOWS and "h264_nvenc" in _FFMPEG_ENCODER_LIST:
        return ["-hwaccel", "cuda"]
    if IS_MACOS:
        return ["-hwaccel", "videotoolbox"]
    return []


# ---------------------------------------------------------------------------
# WebView codec support
# ---------------------------------------------------------------------------

def webview_plays_vp9() -> bool:
    """True on Windows/Linux (Chromium / WebView2); False on macOS (WKWebView)."""
    return not IS_MACOS


def webview_plays_hevc() -> bool:
    """True on macOS (VideoToolbox) and conservatively False elsewhere.

    Windows 11 *can* decode HEVC with a hardware decoder but requires the
    optional HEVC Video Extensions store package — we default False to avoid
    silent playback failures.
    """
    return IS_MACOS


# ---------------------------------------------------------------------------
# ffmpeg executable (cached)
# ---------------------------------------------------------------------------

_FFMPEG_EXE: Optional[str] = None
_FFMPEG_PATH_INJECTED = False


def ensure_ffmpeg_on_path() -> None:
    """Whisper's `load_audio` hardcodes `cmd=["ffmpeg", ...]` and ignores any
    explicit path. In a PyInstaller frozen exe there is no ffmpeg on PATH, so
    every `whisper.transcribe()` call raises `[WinError 2] file not found`.

    Fix: copy the imageio_ffmpeg binary to a stable dir named exactly
    `ffmpeg.exe` (Win) / `ffmpeg` (mac/linux), prepend that dir to PATH.
    Idempotent.
    """
    global _FFMPEG_PATH_INJECTED
    if _FFMPEG_PATH_INJECTED:
        return
    try:
        import shutil
        src = get_ffmpeg_exe()
        target_name = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
        src_name = os.path.basename(src)
        if src_name == target_name:
            d = os.path.dirname(src)
        else:
            d = os.path.join(get_temp_dir(), "sovlens_ffmpeg")
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, target_name)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
                if not IS_WINDOWS:
                    os.chmod(dst, 0o755)
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        _FFMPEG_PATH_INJECTED = True
    except Exception:
        # Best-effort. If it fails, the WinError 2 will still surface upstream
        # and the caller can fall back to alternative paths.
        pass


_CUDNN_PATH_INJECTED = False


def ensure_cudnn_on_path() -> None:
    """Make cuDNN 9 DLLs discoverable by CTranslate2 on Windows.

    CTranslate2 ≥4.5 ships cublas + cudart statically inside its `_ext.pyd`
    but loads cuDNN at runtime via the OS loader. Without the cuDNN DLLs
    on PATH (or registered via `os.add_dll_directory`),
    `ctranslate2.get_cuda_device_count()` returns 0 and faster-whisper
    silently falls back to CPU+int8 — even on a RTX card with a current
    driver. End-user symptom: "Extreme uses CPU not GPU".

    The PyPI wheel `nvidia-cudnn-cu12` ships the DLLs under
    `<site-packages>/nvidia/cudnn/bin/`. In a PyInstaller frozen sidecar
    the same tree lives under `sys._MEIPASS/nvidia/cudnn/bin/`. Both are
    probed; whichever exists first is registered.

    No-op on mac/linux (`nvidia-cudnn-cu12` is Windows-only by spec).
    Idempotent.
    """
    global _CUDNN_PATH_INJECTED
    if _CUDNN_PATH_INJECTED or not IS_WINDOWS:
        return
    candidates: List[str] = []
    # 1. PyInstaller frozen layout
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "nvidia", "cudnn", "bin"))
    # 2. site-packages (dev mode or non-frozen run). nvidia.cudnn is a
    # PEP-420 namespace package — __file__ is None, so use __path__[0].
    try:
        import nvidia.cudnn as _cudnn  # type: ignore
        for _pp in list(getattr(_cudnn, "__path__", [])):
            candidates.append(os.path.join(_pp, "bin"))
    except Exception:
        pass
    registered = False
    for p in candidates:
        if not os.path.isdir(p):
            continue
        try:
            # Python 3.8+ explicit DLL search path — required because PATH
            # alone isn't honored by LoadLibrary in some Win10+ configs
            # when secure DLL search is enabled.
            try:
                os.add_dll_directory(p)
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
            print(f"[cudnn] registered DLL dir: {p}", flush=True)
            registered = True
        except Exception as exc:
            print(f"[cudnn] failed to register {p}: {exc!r}", flush=True)
    if not registered:
        print("[cudnn] no cuDNN bin dir found — whisper will fall back to CPU", flush=True)
    _CUDNN_PATH_INJECTED = True


def get_ffmpeg_exe() -> str:
    """Return the path to the bundled ffmpeg executable via imageio_ffmpeg.

    Result is cached after the first call.
    """
    global _FFMPEG_EXE
    if _FFMPEG_EXE is None:
        from imageio_ffmpeg import get_ffmpeg_exe as _get  # type: ignore
        _FFMPEG_EXE = _get()
    return _FFMPEG_EXE


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

def run_subprocess(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Run *cmd* with UTF-8 text mode as default.

    Fixes the Windows cp1252 default encoding that breaks non-ASCII ffmpeg
    output parsing.  Callers may override any kwarg (e.g. timeout, env).

    Defaults applied unless overridden:
        encoding='utf-8', errors='replace', text=True, capture_output=True
    """
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    kwargs.setdefault("text", True)
    kwargs.setdefault("capture_output", True)
    # PyInstaller console=False + GUI-only Tauri parent means each child
    # spawned without CREATE_NO_WINDOW pops a conhost window. Suppress
    # at the source.
    if IS_WINDOWS:
        kwargs.setdefault("creationflags", 0x08000000)  # CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


# ---------------------------------------------------------------------------
# File-manager reveal
# ---------------------------------------------------------------------------

def reveal_in_file_manager_cmd(path: str) -> List[str]:
    """Return the OS command that reveals *path* in the native file manager.

    * Windows : ['explorer.exe', '/select,', path]
    * macOS   : ['open', '-R', path]
    * Linux   : ['xdg-open', <parent directory>]
    """
    if IS_WINDOWS:
        return ["explorer.exe", f"/select,{path}"]
    if IS_MACOS:
        return ["open", "-R", path]
    return ["xdg-open", os.path.dirname(path)]
