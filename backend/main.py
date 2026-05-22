from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import os
import sys
import uuid
import hashlib
import subprocess
import threading


# ---------------------------------------------------------------------------
# stdout/stderr safety shim — Win-only
# ---------------------------------------------------------------------------
# When the sidecar is spawned by Tauri with no attached console, its
# stdout/stderr are pipes that the Rust shell drains into backend.log.
# Past a certain pipe buffer state Windows starts raising
# OSError(Errno 22, "Invalid argument") on every write/flush. Each raised
# OSError aborts the calling code path — uvicorn's access logger, our
# own print() lines in /folders DELETE, etc. — turning every logging
# attempt into a 500.
#
# Wrap stdout/stderr in a shim that swallows OSError on write+flush so a
# borked stdout never crashes a request handler. The data is lost (we
# already log to a RotatingFileHandler via logging.basicConfig anyway),
# but the API call survives.
class _SafeStream:
    def __init__(self, underlying):
        self._w = underlying

    def write(self, s):
        try:
            return self._w.write(s)
        except OSError:
            return len(s) if isinstance(s, str) else 0

    def flush(self):
        try:
            self._w.flush()
        except OSError:
            pass

    def __getattr__(self, name):
        return getattr(self._w, name)


if sys.platform == "win32":
    try:
        sys.stdout = _SafeStream(sys.stdout)  # type: ignore[assignment]
        sys.stderr = _SafeStream(sys.stderr)  # type: ignore[assignment]
    except Exception:
        # If the shim install itself fails, leave the originals alone —
        # at worst we get the same Errno 22 crash users already see.
        pass



import core
import ingestion
import index_build
import audio_ingest
import yolo_detect
import ocr_detect
import platform_utils
# Whisper subprocess-invokes bare "ffmpeg". In the PyInstaller frozen exe
# there is no system ffmpeg, so without this PATH injection every audio
# ingest raises [WinError 2]. Must run before whisper.transcribe() ever.
platform_utils.ensure_ffmpeg_on_path()

# ONNX Runtime diagnostics — surface ORT version + EP detection state in
# backend.log on every startup so users / bug reports can distinguish: wrong
# wheel installed, native EP DLL missing, or CPU-only fallback in effect.
def _log_ort_state() -> None:
    try:
        import onnxruntime as _ort
        print(f"[ort] onnxruntime={_ort.__version__}", flush=True)
        print(f"[ort] available_providers={_ort.get_available_providers()}", flush=True)
        try:
            print(f"[ort] selected_providers={platform_utils.get_onnx_providers()}", flush=True)
        except Exception as exc:
            print(f"[ort] get_onnx_providers() error: {exc!r}", flush=True)
    except Exception as exc:
        print(f"[ort] diagnostic crashed: {exc!r}", flush=True)


_log_ort_state()


def _migrate_clip_cache_to_fp16_only() -> None:
    """One-time cleanup: wipe legacy CLIP cache entries from buggy wildcard pulls.

    Versions <= 0.1.0 used snapshot_download with allow_patterns
    "vision_model*.onnx" + "text_model*.onnx" — a wildcard that matched all
    8 quantization variants per submodel (~3.7 GB total) when we only ever
    loaded one pair. 0.1.1 narrows the patterns to fp16 only (~830 MB), but
    existing installs already have the orphan fp32 / quantized blobs sitting
    in ~/.cache/huggingface/hub/models--Xenova--clip-vit-large-patch14.

    Detection heuristic: presence of `vision_model.onnx` (the 1.6 GB fp32
    blob) — only the wildcard pull produced this file. fp16-only pulls
    never write it. If we see it, wipe the whole CLIP snapshot dir and let
    snapshot_download fetch the right files on next launch.

    Idempotent via a stamp file under get_app_data_dir(). Best-effort:
    any failure is swallowed so a borked migration never blocks boot.
    """
    try:
        from pathlib import Path
        import shutil

        stamp = Path(platform_utils.get_app_data_dir()) / ".clip-cache-migrated-0.1.1"
        if stamp.exists():
            return

        hf_hub = Path(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))) / "hub"
        clip_root = hf_hub / "models--Xenova--clip-vit-large-patch14"
        if not clip_root.exists():
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text("no-cache-at-migration\n")
            return

        snapshots_dir = clip_root / "snapshots"
        has_legacy = False
        if snapshots_dir.exists():
            for snap in snapshots_dir.iterdir():
                if (snap / "onnx" / "vision_model.onnx").exists():
                    has_legacy = True
                    break

        if has_legacy:
            try:
                shutil.rmtree(clip_root, ignore_errors=True)
                print(f"[clip-migrate] wiped legacy CLIP cache at {clip_root}", flush=True)
            except Exception as exc:
                print(f"[clip-migrate] wipe failed (non-fatal): {exc!r}", flush=True)

        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("migrated\n")
    except Exception as exc:
        print(f"[clip-migrate] scan crashed (non-fatal): {exc!r}", flush=True)


_migrate_clip_cache_to_fp16_only()
import hls_stream
import json
import mimetypes
import config
import logging
from logging.handlers import RotatingFileHandler

_log_dir = os.path.join(platform_utils.get_app_data_dir(), "logs")
os.makedirs(_log_dir, exist_ok=True)
_handler = RotatingFileHandler(
    os.path.join(_log_dir, "backend.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler, logging.StreamHandler()])

tasks_in_progress = 0
_tasks_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Job tracking
# ---------------------------------------------------------------------------
# Per-operation progress for reindex / add_files / sync. The legacy
# `tasks_in_progress` counter still drives the home page busy banner; the
# `_active_jobs` dict adds granular (done, total, current) per running job
# so the UI can render real progress bars.
#
# _media_generation increments on every successful add / delete so the home
# page knows when to refetch /media without waiting for an ingest-end edge.
# ---------------------------------------------------------------------------
import time as _time
_active_jobs: dict = {}
_media_generation: int = 0


def _start_job(job_type: str, total: int = 0, description: str = "") -> str:
    """Register a new background job. Returns a short opaque id."""
    global tasks_in_progress
    job_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        _active_jobs[job_id] = {
            "id": job_id,
            "type": job_type,
            "description": description,
            "total": int(total),
            "done": 0,
            "current": "",
            "started_at": _time.time(),
        }
        tasks_in_progress += 1
    return job_id


def _update_job(job_id: str, **kwargs) -> None:
    """Merge state into an active job. No-op if job already finished."""
    with _tasks_lock:
        job = _active_jobs.get(job_id)
        if job is not None:
            job.update(kwargs)


def _finish_job(job_id: str) -> None:
    """Remove a job from the active set + decrement the legacy counter.

    Idempotent: a double-finish (defensive `finally` + accidental call) is
    a no-op, so the counter never under-counts.
    """
    global tasks_in_progress
    with _tasks_lock:
        if _active_jobs.pop(job_id, None) is None:
            return
        tasks_in_progress -= 1
        if tasks_in_progress < 0:
            tasks_in_progress = 0


def _bump_media_generation() -> None:
    """Signal to /status pollers that the media table has new content."""
    global _media_generation
    with _tasks_lock:
        _media_generation += 1

FOLDERS_FILE = os.path.join(platform_utils.get_app_data_dir(), "folders.json")

def get_folders():
    if os.path.exists(FOLDERS_FILE):
        with open(FOLDERS_FILE, "r") as f:
            return json.load(f)
    return []

def save_folders(folders):
    os.makedirs(os.path.dirname(FOLDERS_FILE), exist_ok=True)
    with open(FOLDERS_FILE, "w") as f:
        json.dump(folders, f)

app = FastAPI(title="SovLens AI Engine", description="Local AI semantic media search backend")

# Allow CORS so the Tauri frontend can communicate with FastAPI.
#
# Origins:
#   - tauri://localhost            mac WKWebView
#   - http://tauri.localhost       Win WebView2 (since Tauri 2)
#   - https://tauri.localhost      Win WebView2 (https variant)
#   - http://localhost:3000        Next.js dev server
#   - http://127.0.0.1:3000        same, IPv4-explicit variant some browsers use
#
# allow_credentials=False: we don't use cookies/Authorization headers — the
# backend binds to 127.0.0.1 only, so the network layer is the auth. Setting
# allow_credentials=True with a wildcard origin is a CORS spec violation
# anyway (browser silently drops the Access-Control-Allow-Origin header).
#
# allow_methods/headers: explicit lists keep the preflight responses small
# and let a future audit see exactly what's exposed at a glance.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Range"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

class SearchQuery(BaseModel):
    query: str
    limit: int = 20

class SyncFolderRequest(BaseModel):
    folder_path: str

@app.get("/")
def read_root():
    return {"message": "SovLens Backend is running!", "device": core.device}

@app.get("/media")
def get_media(limit: int = 20, offset: int = 0):
    try:
        results = core.get_all_media(limit=limit, offset=offset)
        for r in results:
            r.pop("vector", None)
        return {"items": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/image")
def get_image(path: str):
    path = platform_utils.normalize_path(path)
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/status")
def get_status():
    cfg_params = config.get_current_level_params()
    requested = cfg_params.get("model", "clip-ViT-L-14")
    effective = core.resolve_model_name(requested)
    with _tasks_lock:
        # Deep-copy under the lock so FastAPI can serialize without a torn
        # read while another thread mutates a job's `done`/`current`.
        jobs_snapshot = [dict(v) for v in _active_jobs.values()]
        media_gen = _media_generation
    return {
        "is_ingesting": tasks_in_progress > 0,
        "tasks": tasks_in_progress,
        "jobs": jobs_snapshot,           # per-job progress (see /jobs for the same)
        "media_generation": media_gen,   # bumps on every add/delete so UI knows to refetch
        "whisper_available": audio_ingest.WHISPER_AVAILABLE,
        "heic_supported": ingestion.is_heic_supported(),
        "yolo_available": yolo_detect.YOLO_AVAILABLE,
        "ocr_available": ocr_detect.OCR_AVAILABLE,
        "current_model": core.get_model_name(),       # what's loaded in RAM right now
        "configured_model": requested,                 # what the active level requests
        "effective_model": effective,                  # what core will actually load
        "model_fallback": requested != effective,      # True when a fallback is in effect
    }


@app.get("/jobs")
def get_jobs():
    """Return a snapshot of every active background job (reindex / add / sync).

    The same `jobs` array is included on /status, but exposing a dedicated
    endpoint lets the UI poll progress without pulling the rest of /status.
    """
    with _tasks_lock:
        # Deep-copy under the lock — see /status for the same rationale.
        return {"jobs": [dict(v) for v in _active_jobs.values()], "count": len(_active_jobs)}

def _parse_range_header(range_header: str, file_size: int):
    """Parse an HTTP Range header per RFC 7233.

    Returns one of:
      ("full", None)            -> caller should serve the whole file (header malformed/ignorable)
      ("ok", (start, end))      -> valid satisfiable range, inclusive end
      ("invalid", None)         -> 416 Range Not Satisfiable

    Handles, with explicit reasoning:
      - "bytes=start-end"           valid, both ends present
      - "bytes=start-"              open-ended; end clamped to file_size-1
      - "bytes=-N"                  suffix range; last N bytes (start = max(0, size-N))
      - "bytes=-"                   malformed -> 416
      - "bytes=0-100,200-300"       multipart: not supported -> 416 (cleanly)
      - non-integer values          -> 416
      - start > end                 -> 416
      - start >= file_size          -> 416
      - empty / non-"bytes=" prefix -> serve full file
      - zero-byte file              -> any range request -> 416 (no satisfiable bytes)
    """
    if not range_header:
        return ("full", None)
    s = range_header.strip().lower()
    if not s.startswith("bytes="):
        return ("full", None)
    spec = s[len("bytes="):].strip()
    if not spec:
        return ("full", None)
    # Multipart ranges (comma-separated) are not supported here.
    if "," in spec:
        return ("invalid", None)
    if "-" not in spec:
        return ("invalid", None)
    lhs, rhs = spec.split("-", 1)
    lhs, rhs = lhs.strip(), rhs.strip()

    # Zero-byte file: nothing satisfiable.
    if file_size <= 0:
        return ("invalid", None)

    try:
        if lhs == "" and rhs == "":
            # "bytes=-" — malformed.
            return ("invalid", None)
        if lhs == "":
            # Suffix range: last N bytes.
            n = int(rhs)
            if n <= 0:
                return ("invalid", None)
            start = max(0, file_size - n)
            end = file_size - 1
        elif rhs == "":
            # Open-ended: start to EOF.
            start = int(lhs)
            end = file_size - 1
        else:
            start = int(lhs)
            end = int(rhs)
            # Clamp end to last byte if client over-asked.
            if end >= file_size:
                end = file_size - 1
    except ValueError:
        return ("invalid", None)

    if start < 0 or start >= file_size or end < start:
        return ("invalid", None)
    return ("ok", (start, end))


def range_requests_response(request: Request, file_path: str, content_type: str):
    file_size = os.stat(file_path).st_size
    range_header = request.headers.get("range")

    headers = {
        "content-type": content_type,
        "accept-ranges": "bytes",
        "content-encoding": "identity",
        "content-length": str(file_size),
        "access-control-expose-headers": "content-type, accept-ranges, content-length, content-range, content-encoding",
        "access-control-allow-origin": "*",
    }

    status, parsed = _parse_range_header(range_header or "", file_size)

    if status == "full":
        def stream_file():
            with open(file_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        return StreamingResponse(stream_file(), headers=headers)

    if status == "invalid":
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

    start, end = parsed  # type: ignore[misc]
    chunk_length = end - start + 1
    headers["content-length"] = str(chunk_length)
    headers["content-range"] = f"bytes {start}-{end}/{file_size}"

    def stream_range():
        with open(file_path, "rb") as f:
            f.seek(start)
            bytes_left = chunk_length
            while bytes_left > 0:
                read_size = min(1024 * 1024, bytes_left)
                data = f.read(read_size)
                if not data:
                    break
                yield data
                bytes_left -= len(data)

    return StreamingResponse(stream_range(), headers=headers, status_code=206)

@app.get("/video")
def get_video(path: str, request: Request):
    path = platform_utils.normalize_path(path)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Video not found")

    # WKWebView (Tauri on macOS) cannot decode VP9/HEVC. Transcode to H.264 if needed.
    serve_path = _ensure_webview_compatible(path)

    content_type, _ = mimetypes.guess_type(serve_path)
    if not content_type:
        content_type = "video/mp4"

    return range_requests_response(request, serve_path, content_type)


# --- WebView codec compat layer ---
# WKWebView (Safari/Tauri) supports H.264+AAC in mp4/mov. VP9/HEVC/AV1 fail silently.
# Cache transcoded copies on disk; serve cached file via the normal range endpoint.

_TRANSCODE_CACHE = os.path.join(platform_utils.get_app_data_dir(), "transcoded")
_COMPAT_VIDEO_CODECS = {"h264", "avc1"}
_COMPAT_AUDIO_CODECS = {"aac", "mp3"}
_transcode_lock = threading.Lock()
_transcode_in_progress: dict = {}  # path -> threading.Event

def _probe_codecs(video_path: str) -> tuple:
    """Return (video_codec, audio_codec) lowercase, or ('','') on failure."""
    ff = platform_utils.get_ffmpeg_exe()
    try:
        out = platform_utils.run_subprocess(
            [ff, "-i", video_path, "-hide_banner"],
            timeout=10,
        ).stderr
    except Exception:
        return ("", "")
    vcodec = ""
    acodec = ""
    for line in out.splitlines():
        s = line.strip()
        if "Video:" in s and not vcodec:
            after = s.split("Video:", 1)[1].strip()
            vcodec = after.split()[0].strip(",").lower()
        elif "Audio:" in s and not acodec:
            after = s.split("Audio:", 1)[1].strip()
            acodec = after.split()[0].strip(",").lower()
    return (vcodec, acodec)

def _cache_path_for(video_path: str) -> str:
    """Deterministic cache path keyed by abspath + mtime so re-encodes follow source edits."""
    st = os.stat(video_path)
    key = f"{os.path.abspath(video_path)}|{st.st_mtime_ns}|{st.st_size}"
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return os.path.join(_TRANSCODE_CACHE, f"{h}.mp4")

def _ensure_webview_compatible(video_path: str) -> str:
    """If source codec is WebView-compatible, return original. Else transcode + return cache path.

    Short-circuits on Win/Linux for VP9 (WebView2/Chromium plays it natively).
    Short-circuits on macOS for HEVC (WKWebView + VideoToolbox handles it).
    """
    vcodec, acodec = _probe_codecs(video_path)

    # VP9: skip transcode on platforms where the webview can play it
    if vcodec == "vp9" and platform_utils.webview_plays_vp9():
        return video_path

    # HEVC: skip transcode on platforms where the webview can play it
    if vcodec in {"hevc", "h265"} and platform_utils.webview_plays_hevc():
        return video_path

    if vcodec in _COMPAT_VIDEO_CODECS and (acodec in _COMPAT_AUDIO_CODECS or acodec == ""):
        return video_path

    cache = _cache_path_for(video_path)
    if os.path.exists(cache) and os.path.getsize(cache) > 0:
        return cache

    # Serialize concurrent transcode requests for same source.
    with _transcode_lock:
        ev = _transcode_in_progress.get(video_path)
        if ev is None:
            ev = threading.Event()
            _transcode_in_progress[video_path] = ev
            do_work = True
        else:
            do_work = False
    if not do_work:
        ev.wait(timeout=600)
        return cache if os.path.exists(cache) else video_path

    try:
        os.makedirs(_TRANSCODE_CACHE, exist_ok=True)
        ff = platform_utils.get_ffmpeg_exe()
        tmp = cache + ".part"
        # Use HW decoder args before -i, HW encoder args in output.
        # faststart so first-byte playback works.
        decoder_args = platform_utils.detect_hwaccel_decoder()
        encoder_args = platform_utils.detect_hwaccel_encoder()
        cmd = (
            [ff, "-y"]
            + decoder_args
            + ["-i", video_path]
            + encoder_args
            + [
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-f", "mp4",
                "-loglevel", "error",
                tmp,
            ]
        )
        proc = platform_utils.run_subprocess(cmd, timeout=3600)
        if proc.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, cache)
            return cache
        # Failure: fall back to original (may not play but better than 500).
        if os.path.exists(tmp):
            os.remove(tmp)
        return video_path
    finally:
        with _transcode_lock:
            _transcode_in_progress.pop(video_path, None)
        ev.set()

@app.get("/hls/{video_id}/playlist.m3u8")
def hls_playlist(video_id: str, path: str):
    """Return a static HLS playlist for the given source video.

    Query param ``path`` is the source video absolute path.
    Each segment URL in the playlist is relative so hls.js resolves it against
    the playlist URL base.

    Example response (application/vnd.apple.mpegurl):
        #EXTM3U
        #EXT-X-VERSION:3
        #EXT-X-TARGETDURATION:6
        ...
        #EXTINF:6.000000,
        segment_0.ts
        ...
        #EXT-X-ENDLIST
    """
    path = platform_utils.normalize_path(path)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        playlist_path = hls_stream.get_or_build_playlist(path, video_id=video_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Playlist build failed: {exc}")
    return FileResponse(
        playlist_path,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/hls/{video_id}/segment_{idx}.ts")
def hls_segment(video_id: str, idx: int, path: Optional[str] = None):
    """Transcode and return a single MPEG-TS segment.

    Segment is transcoded on first request and cached. Subsequent requests for
    the same segment are served directly from the cache file.

    ``path`` query parameter is optional: if omitted the video source path is
    resolved from the in-memory registry (populated when the playlist is first
    fetched).  If provided, the path is registered as a side-effect so the
    endpoint remains backwards-compatible with direct segment requests.

    Returns video/mp2t with status 200.
    """
    # Resolve source path: registry first, then optional query param.
    resolved = hls_stream.resolve_video_id(video_id)
    if resolved is None:
        if path is None:
            raise HTTPException(
                status_code=404,
                detail="video_id not found in registry; fetch playlist first",
            )
        # Register via the supplied path so future segment requests need no query.
        resolved = platform_utils.normalize_path(path)
        hls_stream.register_video(resolved, video_id=video_id)
    elif path is not None:
        # Caller supplied path — re-register to keep registry warm.
        hls_stream.register_video(platform_utils.normalize_path(path), video_id=video_id)

    if not os.path.exists(resolved):
        raise HTTPException(status_code=404, detail="Video not found")
    if idx < 0:
        raise HTTPException(status_code=400, detail="Segment index must be >= 0")
    try:
        seg_path = hls_stream.get_or_build_segment(resolved, idx)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Segment build failed: {exc}")
    return FileResponse(
        seg_path,
        media_type="video/mp2t",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/search")
def search(query: SearchQuery):
    try:
        # expand_query is a stub — ready for template-based expansion later
        variants = core.expand_query(query.query)
        query_vector = core.encode_text(variants[0])
        results = core.search_media(
            query_vector,
            limit=query.limit,
            query_text=query.query,
            text_boost=True,
        )
        return {"items": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/build_index")
def build_index(background_tasks: BackgroundTasks):
    """Trigger an ANN index build (or rebuild) as a background task.

    Returns immediately. Safe to call multiple times — LanceDB replaces the index.
    Brute-force kNN is fine up to ~50-100k rows; use this endpoint beyond that.
    """
    def _run_build():
        global tasks_in_progress
        with _tasks_lock:
            tasks_in_progress += 1
        try:
            result = index_build.build_ann_index()
            print(f"[build_index] {result['message']}")
        except Exception as exc:
            print(f"[build_index] Error: {exc}")
        finally:
            with _tasks_lock:
                tasks_in_progress -= 1

    background_tasks.add_task(_run_build)
    return {"message": "ANN index build started in background"}


class AddFileRequest(BaseModel):
    filepath: str

@app.post("/add_file")
def add_file(req: AddFileRequest, background_tasks: BackgroundTasks):
    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=400, detail="File does not exist")
    
    def process_single(filepath):
        global tasks_in_progress
        with _tasks_lock:
            tasks_in_progress += 1
        try:
            ext = os.path.splitext(filepath)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.heic', '.heif']:
                rec = ingestion.process_image(filepath)
                if rec:
                    core.add_media([rec])
            elif ext in ['.mp4', '.mov', '.avi', '.mkv']:
                recs = ingestion.process_video(filepath)
                if recs:
                    core.add_media(recs)
        finally:
            with _tasks_lock:
                tasks_in_progress -= 1
                
    background_tasks.add_task(process_single, req.filepath)
    return {"message": "File processing started"}

@app.post("/add_folder")
def add_folder(req: SyncFolderRequest, background_tasks: BackgroundTasks):
    if not os.path.isdir(req.folder_path):
        raise HTTPException(status_code=400, detail="Invalid folder path")

    folders = get_folders()
    if req.folder_path not in folders:
        folders.append(req.folder_path)
        save_folders(folders)

    def process_folder_wrapper(folder_path):
        try:
            total = len(ingestion.get_media_files(folder_path))
        except Exception:
            total = 0
        job_id = _start_job(
            "add_folder",
            total=total,
            description=f"Indexing {os.path.basename(folder_path) or folder_path}",
        )
        try:
            def _cb(done: int, total_now: int, current: str) -> None:
                _update_job(job_id, done=done, total=total_now, current=current)
            try:
                ingestion.process_folder(folder_path, progress_cb=_cb)
            except TypeError:
                ingestion.process_folder(folder_path)
            if index_build.should_build_index():
                index_build.build_ann_index()
        finally:
            _finish_job(job_id)
            _bump_media_generation()

    background_tasks.add_task(process_folder_wrapper, req.folder_path)
    return {"message": "Folder added and sync started"}

@app.post("/sync_all")
def sync_all(background_tasks: BackgroundTasks):
    folders = get_folders()
    if not folders:
        return {"message": "No folders to sync"}
        
    def sync_all_wrapper(folder_paths):
        pre_total = 0
        for fp in folder_paths:
            if os.path.exists(fp):
                try:
                    pre_total += len(ingestion.get_media_files(fp))
                except Exception:
                    pass
        job_id = _start_job("sync_all", total=pre_total, description="Syncing all folders")
        overall_done = 0
        try:
            core.cleanup_missing_media()
            for fp in folder_paths:
                if not os.path.exists(fp):
                    continue
                def _cb(done: int, _total: int, current: str, _base=overall_done) -> None:
                    _update_job(job_id, done=_base + done, current=current)
                try:
                    ingestion.process_folder(fp, progress_cb=_cb)
                except TypeError:
                    ingestion.process_folder(fp)
                try:
                    overall_done += len(ingestion.get_media_files(fp))
                except Exception:
                    pass
                _update_job(job_id, done=overall_done)
            if index_build.should_build_index():
                index_build.build_ann_index()
        finally:
            _finish_job(job_id)
            _bump_media_generation()

    background_tasks.add_task(sync_all_wrapper, folders)
    return {"message": "Syncing all folders started"}


class ReindexFolderRequest(BaseModel):
    folder_path: str


def _reindex_all_wrapper() -> None:
    """Transactional reindex.

    Strategy: rename the live table to a timestamped backup, create a fresh
    empty live table, and run ingestion into it. On ANY exception, drop the
    partial live table and rename the backup back. On success, drop the
    backup. This guarantees that a mid-run crash never destroys user data.
    """
    # 0. Register the job FIRST so /status reports is_ingesting=true within
    # ~10 ms of the POST. We update `total` after we know it. Previously
    # the file enumeration ran before _start_job, so big libraries spent
    # 5-30 s in a "no job visible" gap and the home-page banner stayed
    # hidden — making the user think reindex never started.
    job_id = _start_job("reindex_all", total=0, description="Re-indexing all folders")
    folders = get_folders()
    reachable: List[str] = [f for f in folders if os.path.isdir(f)]
    for f in folders:
        if not os.path.isdir(f):
            print(f"[reindex_all] WARNING: folder unreachable, skipping: {f}")

    _update_job(job_id, current="Counting files…")
    pre_total = 0
    for folder in reachable:
        try:
            pre_total += len(ingestion.get_media_files(folder))
        except Exception:
            pass
    _update_job(job_id, total=pre_total, current="")
    backup_name = f"media_backup_{int(_time.time())}"
    backup_created = False
    overall_done = 0
    try:
        # 1. Backup live table -> fresh empty table.
        removed = core.backup_and_reset_table(backup_name)
        backup_created = True
        print(f"[reindex_all] Backed up {removed} rows to {backup_name!r}; live table reset.")

        # Progress file references the OLD table state; clear so we don't
        # falsely skip files that need re-encoding.
        ingestion.clear_progress_file()

        # 2. Re-ingest into the now-empty live table, streaming per-file
        # progress into the job.
        for folder in reachable:
            def _cb(done: int, total: int, current: str, _base=overall_done) -> None:
                _update_job(job_id, done=_base + done, current=current)
            try:
                ingestion.process_folder(folder, progress_cb=_cb)
            except TypeError:
                # Backward compat if running against an older ingestion without progress_cb.
                ingestion.process_folder(folder)
            try:
                overall_done += len(ingestion.get_media_files(folder))
            except Exception:
                pass
            _update_job(job_id, done=overall_done)

        # 3. Success — drop the backup.
        core.drop_backup(backup_name)
        backup_created = False
        print(f"[reindex_all] Reindex complete; dropped backup {backup_name!r}.")

    except Exception as exc:
        print(f"[reindex_all] Error: {exc!r} — attempting rollback.")
        if backup_created:
            try:
                core.restore_from_backup(backup_name)
                print(f"[reindex_all] Restored from backup {backup_name!r}.")
            except Exception as restore_exc:
                # Leave backup table in place for manual recovery.
                print(
                    f"[reindex_all] CRITICAL: restore from {backup_name!r} failed: {restore_exc!r}. "
                    f"Backup table is preserved for manual recovery."
                )
    finally:
        _finish_job(job_id)
        _bump_media_generation()


@app.post("/reindex_all")
def reindex_all_endpoint(background_tasks: BackgroundTasks):
    """Purge every row in LanceDB and re-ingest all known files using current level params."""
    with _tasks_lock:
        if tasks_in_progress > 0:
            raise HTTPException(
                status_code=409,
                detail="Ingest in progress. Wait for it to complete before re-indexing.",
            )
    background_tasks.add_task(_reindex_all_wrapper)
    return {"status": "started"}


def _reindex_folder_wrapper(folder_path: str) -> None:
    try:
        total = len(ingestion.get_media_files(folder_path)) if os.path.exists(folder_path) else 0
    except Exception:
        total = 0
    job_id = _start_job(
        "reindex_folder",
        total=total,
        description=f"Re-indexing {os.path.basename(folder_path) or folder_path}",
    )
    try:
        core.delete_rows_under_folder(folder_path)
        if os.path.exists(folder_path):
            def _cb(done: int, _total: int, current: str) -> None:
                _update_job(job_id, done=done, current=current)
            try:
                ingestion.process_folder(folder_path, progress_cb=_cb)
            except TypeError:
                ingestion.process_folder(folder_path)
        else:
            print(f"[reindex_folder] Folder does not exist (no-op): {folder_path}")
    except Exception as exc:
        print(f"[reindex_folder] Error: {exc}")
    finally:
        _finish_job(job_id)
        _bump_media_generation()


@app.post("/reindex_folder")
def reindex_folder_endpoint(req: ReindexFolderRequest, background_tasks: BackgroundTasks):
    """Re-ingest a single folder (delete its rows, then re-process)."""
    with _tasks_lock:
        if tasks_in_progress > 0:
            raise HTTPException(
                status_code=409,
                detail="Ingest in progress. Wait for it to complete before re-indexing.",
            )
    background_tasks.add_task(_reindex_folder_wrapper, req.folder_path)
    return {"status": "started", "folder": req.folder_path}


class AddAudioRequest(BaseModel):
    filepath: str


@app.post("/add_audio_to_video")
def add_audio_to_video(req: AddAudioRequest, background_tasks: BackgroundTasks):
    """Transcribe a video's audio track and insert timed segments into LanceDB.

    Requires ``openai-whisper`` to be installed. Returns 501 with an install
    hint when Whisper is unavailable so the caller can surface a clear message.

    The video must already be indexed (frames present) so that ``video_id`` can
    be looked up. The audio segments share the same ``video_id`` as the frames,
    enabling timeline-aware search results.

    Request body: ``{"filepath": "<absolute path to video>"}``

    Returns:
        202: ``{"message": "Audio transcription started", "filepath": "..."}``
        400: File not found or unsupported extension.
        501: Whisper not installed.
    """
    if not audio_ingest.WHISPER_AVAILABLE:
        raise HTTPException(
            status_code=501,
            detail="Whisper not installed. Run: pip install faster-whisper",
        )

    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=400, detail="File does not exist")

    ext = os.path.splitext(req.filepath)[1].lower()
    if ext not in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        raise HTTPException(status_code=400, detail=f"Unsupported video extension: {ext}")

    # Require the video to be frame-indexed before audio transcription so that
    # audio_segment rows share a valid video_id with their parent frame rows.
    video_id = _resolve_video_id(req.filepath)
    if not video_id:
        raise HTTPException(
            status_code=400,
            detail="Video must be ingested first via /add_file",
        )

    def _run_audio(filepath: str, vid: str) -> None:
        global tasks_in_progress
        with _tasks_lock:
            tasks_in_progress += 1
        try:
            count = audio_ingest.process_video_audio(filepath, vid)
            print(f"[add_audio_to_video] Inserted {count} segments for {filepath}")
        except Exception as exc:
            print(f"[add_audio_to_video] Error processing {filepath}: {exc}")
        finally:
            with _tasks_lock:
                tasks_in_progress -= 1

    background_tasks.add_task(_run_audio, req.filepath, video_id)
    return {"message": "Audio transcription started", "filepath": req.filepath}


def _resolve_video_id(video_path: str) -> Optional[str]:
    """Look up the video_id for an already-indexed video path in LanceDB.

    The path is normalized before querying so it matches the stored canonical form.
    Returns None when no matching row is found (video not yet frame-indexed).
    """
    try:
        norm = platform_utils.normalize_path(video_path)
        safe_path = norm.replace("'", "''")
        rows = (
            core.table.search()
            .where(f"path = '{safe_path}' AND is_primary = true")
            .limit(1)
            .to_list()
        )
        return rows[0].get("video_id") if rows else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

class SetLevelRequest(BaseModel):
    level: str

@app.get("/config")
def get_config_endpoint():
    """Return current analysis level, its params, and available levels.

    Also surfaces `model_fallback` + `effective_model` so the Settings UI can
    show an honest banner when the level's requested model can't run in the
    current build (e.g. Low requests CLIP-B-32 but the fixed 768d schema
    forces a fallback to CLIP-L-14).
    """
    params = config.get_current_level_params()
    requested = params.get("model", "clip-ViT-L-14")
    effective = core.resolve_model_name(requested)
    return {
        "level": config.get_config()["level"],
        "level_data": params,
        "available_levels": config.list_levels(),
        "configured_model": requested,
        "effective_model": effective,
        "model_fallback": requested != effective,
    }

@app.put("/config/level")
def set_level_endpoint(req: SetLevelRequest):
    """Update the active analysis level. Body: {level: 'low'|'medium'|'high'|'extreme'}.

    Also drops the cached CLIP model so the next encode call reloads with the
    model specified by the new level.  Note: if the new level selects a different
    model, existing DB vectors (encoded with the old model) are no longer
    comparable to new-model vectors — a full re-index is required (WS-S6).
    """
    try:
        result = config.set_level(req.level)
        core.reset_model()
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Folder management endpoints
# ---------------------------------------------------------------------------

@app.get("/folders")
def list_folders():
    """List all registered folders with metadata and indexed row counts."""
    folders = get_folders()
    items = []
    for p in folders:
        norm = platform_utils.normalize_path(p)
        count = 0
        try:
            count = core._count_rows_under_folder(norm)
        except Exception:
            pass
        items.append({
            "path": p,
            "normalized_path": norm,
            "exists": os.path.exists(norm),
            "indexed_count": count,
        })
    return {"items": items}


class DeleteFoldersRequest(BaseModel):
    paths: List[str]


@app.delete("/folders")
def delete_folders(req: DeleteFoldersRequest):
    """Remove folders from registry + delete their rows from DB.

    Heavily logged because we disable uvicorn access_log on Win (see
    uvicorn.run below) — without explicit prints we can't see whether
    the endpoint ran at all when a user reports "delete didn't work".
    """
    print(f"[/folders DELETE] paths={req.paths}", flush=True)
    with _tasks_lock:
        if tasks_in_progress > 0:
            print("[/folders DELETE] 409 — ingest in progress", flush=True)
            raise HTTPException(status_code=409, detail="Ingest in progress. Wait then retry.")
    folders = get_folders()
    removed = []
    for p in req.paths:
        norm = platform_utils.normalize_path(p)
        # Pre + post counts let us prove the SQL delete actually ran in
        # the log, separate from any frontend refetch issue.
        before = 0
        try:
            before = core._count_rows_under_folder(norm)
        except Exception:
            pass
        folders = [f for f in folders if f != p and platform_utils.normalize_path(f) != norm]
        try:
            core.delete_rows_under_folder(norm)
            after = 0
            try:
                after = core._count_rows_under_folder(norm)
            except Exception:
                pass
            print(f"[/folders DELETE] {p!r} normalized={norm!r} rows: {before} -> {after}", flush=True)
            removed.append(p)
        except Exception as e:
            print(f"[/folders DELETE] error for {p}: {e!r}", flush=True)
    save_folders(folders)
    # Bump so the home page knows to refetch /media even though no ingest ran.
    if removed:
        _bump_media_generation()
        print(f"[/folders DELETE] bumped media_generation; removed={removed}", flush=True)
    return {"removed": removed, "remaining_count": len(folders)}


class AddFilesRequest(BaseModel):
    filepaths: List[str]


def _add_files_wrapper(filepaths: List[str]) -> None:
    job_id = _start_job("add_files", total=len(filepaths), description=f"Adding {len(filepaths)} file(s)")
    added_any = False
    try:
        existing_paths = core.get_existing_paths()
        for idx, fp in enumerate(filepaths):
            _update_job(job_id, done=idx, current=os.path.basename(fp))
            ext = os.path.splitext(fp)[1].lower()
            norm_fp = platform_utils.normalize_path(fp)
            if norm_fp in existing_paths:
                # Already indexed — skip silently (mirrors process_folder dedup).
                continue
            try:
                if ext in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
                    video_id = str(uuid.uuid4())
                    recs = ingestion.process_video(fp, video_id)
                    if recs:
                        # Persist frame rows BEFORE audio ingest so audio
                        # segments are never orphaned on a mid-flight crash.
                        core.add_media(recs)
                        added_any = True
                        ingestion.ingest_video_audio_if_enabled(norm_fp, video_id)
                        existing_paths.add(norm_fp)
                else:
                    rec = ingestion.process_image(fp)
                    if rec:
                        core.add_media([rec])
                        added_any = True
                        existing_paths.add(norm_fp)
            except Exception as e:
                print(f"[/add_files] failed {fp}: {e}")
        _update_job(job_id, done=len(filepaths), current="")
    finally:
        _finish_job(job_id)
        if added_any:
            _bump_media_generation()


@app.post("/add_files")
def add_files(req: AddFilesRequest, background_tasks: BackgroundTasks):
    """Process multiple individual files as background tasks."""
    accepted: List[str] = []
    rejected = []
    for fp in req.filepaths:
        if not os.path.exists(fp):
            rejected.append({"path": fp, "reason": "not_found"})
            continue
        ext = os.path.splitext(fp)[1].lower()
        if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
                       ".mp4", ".mov", ".mkv", ".webm", ".avi"}:
            rejected.append({"path": fp, "reason": "unsupported_type"})
            continue
        accepted.append(fp)
    if accepted:
        background_tasks.add_task(_add_files_wrapper, accepted)
    return {"accepted": len(accepted), "rejected": rejected}


# ---------------------------------------------------------------------------
# Model status + warmup endpoints
# ---------------------------------------------------------------------------

# CLIP repo id mirrors core._HF_ONNX_REPO_ID — kept local so this module
# doesn't reach into core's private name.
_HF_CLIP_REPO_ID = "Xenova/clip-vit-large-patch14"


def _hf_file_cached(repo_id: str, filename: str, min_bytes: int = 1) -> bool:
    """Strict check: a specific file is fully present in the HF cache.

    Why this matters: the old substring-match on cache dir names returned
    True the moment HF created `models--<org>--<repo>/` — which happens
    BEFORE any file finishes downloading. The Settings UI then hid the
    Download button while no model file existed on disk; on next refresh
    the substring still matched, so we never re-triggered the download.

    Uses huggingface_hub.try_to_load_from_cache so we follow the same
    snapshot/blob symlink dance HF uses internally; a partial `.incomplete`
    file is reported as missing here, matching what hf_hub_download would
    do at runtime.
    """
    try:
        from huggingface_hub import try_to_load_from_cache  # lazy import
        path = try_to_load_from_cache(repo_id=repo_id, filename=filename)
        if not isinstance(path, str):
            return False
        if not os.path.exists(path):
            return False
        try:
            return os.path.getsize(path) >= min_bytes
        except OSError:
            return False
    except Exception:
        return False


def _clip_downloaded() -> bool:
    """CLIP is "downloaded" when tokenizer + the fp16 ONNX pair is on disk.

    core.py's snapshot_download only pulls fp16 (see allow_patterns there),
    so checking for the fp16 pair specifically is correct. We keep an
    fp32 fallback for users mid-upgrade who already have the larger
    vision_model.onnx cached from an older build — those installs report
    downloaded=true and skip the redundant fp16 fetch.
    """
    tok = _hf_file_cached(_HF_CLIP_REPO_ID, "tokenizer.json", min_bytes=10_000)
    if not tok:
        return False
    # fp16 (current build): vision ~580 MB, text ~236 MB.
    if _hf_file_cached(_HF_CLIP_REPO_ID, "onnx/vision_model_fp16.onnx", min_bytes=400 * 1024 * 1024) and \
       _hf_file_cached(_HF_CLIP_REPO_ID, "onnx/text_model_fp16.onnx", min_bytes=150 * 1024 * 1024):
        return True
    # fp32 fallback for legacy caches.
    if _hf_file_cached(_HF_CLIP_REPO_ID, "onnx/vision_model.onnx", min_bytes=800 * 1024 * 1024) and \
       _hf_file_cached(_HF_CLIP_REPO_ID, "onnx/text_model.onnx", min_bytes=300 * 1024 * 1024):
        return True
    return False


def _whisper_downloaded() -> bool:
    """faster-whisper's `model.bin` (CTranslate2) is the gating artifact.

    The repo also ships `config.json` + `tokenizer.json` (small) — those
    can be cached before model.bin finishes, so we only trust model.bin.
    """
    size = "base"
    try:
        size = (config.get_current_level_params().get("whisper_model") or "base").lower()
    except Exception:
        pass
    repo_id = f"Systran/faster-whisper-{size}"
    # base ~150 MB, small ~500 MB, medium ~1.5 GB — pick a safe lower
    # bound of 50 MB to be smaller than even the smallest variant.
    return _hf_file_cached(repo_id, "model.bin", min_bytes=50 * 1024 * 1024)


def _yolo_downloaded() -> bool:
    """True if a usable yolov8n.onnx exists in any of yolo_detect's lookup
    paths. yolo_detect owns the priority order (bundle > dev > cache).
    """
    try:
        for p in yolo_detect._bundled_onnx_paths():
            if p and os.path.isfile(p) and os.path.getsize(p) >= 9 * 1024 * 1024:
                return True
        cached = yolo_detect._runtime_cache_path()
        if os.path.isfile(cached) and os.path.getsize(cached) >= 9 * 1024 * 1024:
            return True
    except Exception:
        pass
    return False




def _ocr_downloaded() -> bool:
    """RapidOCR ships its ONNX models inside the wheel — nothing to download.

    Surface the OCR_AVAILABLE flag as `downloaded` so the UI's Download
    button hides itself once the wheel is installed.
    """
    return bool(getattr(ocr_detect, "OCR_AVAILABLE", False))


def _clip_status() -> dict:
    cfg_params = config.get_current_level_params()
    requested = cfg_params.get("model", "clip-ViT-L-14")
    effective = core.resolve_model_name(requested)
    return {
        "name": effective,
        "loaded": core.get_model_name() == effective,
        "downloaded": _clip_downloaded(),
    }


def _whisper_loaded() -> bool:
    """Return True if a whisper model is currently loaded in memory."""
    try:
        return getattr(audio_ingest, "_whisper_model", None) is not None
    except Exception:
        return False


def _whisper_status() -> dict:
    return {
        "name": "whisper",
        "available": audio_ingest.WHISPER_AVAILABLE,
        "loaded": audio_ingest.WHISPER_AVAILABLE and _whisper_loaded(),
        "downloaded": _whisper_downloaded(),
    }


def _yolo_loaded() -> bool:
    try:
        return getattr(yolo_detect, "_model", None) is not None
    except Exception:
        return False


def _yolo_status() -> dict:
    return {
        "name": "yolov8n",
        "available": yolo_detect.YOLO_AVAILABLE,
        "loaded": yolo_detect.YOLO_AVAILABLE and _yolo_loaded(),
        "downloaded": _yolo_downloaded(),
    }


def _ocr_loaded() -> bool:
    try:
        return getattr(ocr_detect, "_reader", None) is not None
    except Exception:
        return False


def _ocr_status() -> dict:
    return {
        "name": "rapidocr",
        "available": ocr_detect.OCR_AVAILABLE,
        "loaded": ocr_detect.OCR_AVAILABLE and _ocr_loaded(),
        "downloaded": _ocr_downloaded(),
    }


@app.get("/models/status")
def models_status():
    """Return download/load state for each AI model. Fast — no model I/O."""
    return {
        "clip": _clip_status(),
        "whisper": _whisper_status(),
        "yolo": _yolo_status(),
        "ocr": _ocr_status(),
    }


# Expected on-disk sizes per model — used to compute a progress percent
# while a download is in flight. HF doesn't expose per-file byte progress
# without hooking tqdm, but walking the cache dir and dividing by an
# expected total gives a reasonable bar (final 5-10% sometimes lingers as
# the `.incomplete` file finalises).
_EXPECTED_BYTES = {
    # CLIP-L-14 fp16 (vision + text + tokenizer + configs). Pinned to the
    # exact files allow_patterns in core.py pulls — if you ever expand to
    # also ship fp32 / quantized variants, bump this so the splash bar
    # doesn't exceed 100%.
    "clip": 830 * 1024 * 1024,
    "whisper_base": 150 * 1024 * 1024,
    "whisper_small": 500 * 1024 * 1024,
    "whisper_medium": 1500 * 1024 * 1024,
    "whisper_large": 3000 * 1024 * 1024,
    "yolo": 12 * 1024 * 1024,
}


def _dir_bytes(path: str) -> int:
    """Sum file bytes under `path`. Returns 0 if missing. Safe against
    in-flight .incomplete files (counted toward progress)."""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return 0
    total = 0
    try:
        for child in p.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _hf_cache_size(*needles: str) -> int:
    """Total bytes across any HF cache dirs whose name contains a needle."""
    from pathlib import Path
    hf_hub = Path(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))) / "hub"
    if not hf_hub.exists():
        return 0
    sanitized = [n.replace("/", "_").replace("-", "").lower() for n in needles]
    total = 0
    for child in hf_hub.iterdir():
        if not child.is_dir():
            continue
        cname = child.name.replace("-", "").lower()
        if any(n in cname for n in sanitized):
            total += _dir_bytes(str(child))
    return total


@app.get("/models/progress")
def models_progress():
    """Bytes-on-disk vs expected-total per model. Cheap (filesystem walk)."""
    clip_now = _hf_cache_size("clip-vit-large-patch14", "clip-ViT-L-14")
    whisper_size = "base"
    try:
        whisper_size = (config.get_current_level_params().get("whisper_model") or "base").lower()
    except Exception:
        pass
    whisper_total_key = f"whisper_{whisper_size}" if f"whisper_{whisper_size}" in _EXPECTED_BYTES else "whisper_base"
    whisper_now = _hf_cache_size("faster-whisper")
    # YOLO is no longer pulled from HF — it lives in the PyInstaller
    # bundle or in <app_data>/models/. Report 100% as soon as a
    # usable file exists, 0% otherwise. The download is a one-shot
    # streaming fetch that progresses too fast (~12 MB) to bother
    # exposing byte-level progress for.
    yolo_now = _EXPECTED_BYTES["yolo"] if _yolo_downloaded() else 0

    def pct(now: int, total: int) -> int:
        if total <= 0:
            return 0
        return min(100, int(round(100.0 * now / total)))

    return {
        "clip":    {"bytes_now": clip_now,    "bytes_total": _EXPECTED_BYTES["clip"],            "percent": pct(clip_now, _EXPECTED_BYTES["clip"])},
        "whisper": {"bytes_now": whisper_now, "bytes_total": _EXPECTED_BYTES[whisper_total_key], "percent": pct(whisper_now, _EXPECTED_BYTES[whisper_total_key])},
        "yolo":    {"bytes_now": yolo_now,    "bytes_total": _EXPECTED_BYTES["yolo"],            "percent": pct(yolo_now, _EXPECTED_BYTES["yolo"])},
        "ocr":     {"bytes_now": 0,           "bytes_total": 0,                                   "percent": 100},
    }


def _wipe_stale_partials(repo_name_substring: str, stale_after_seconds: int = 60) -> int:
    """Delete .incomplete partials in the HF cache that look abandoned.

    huggingface_hub writes downloads into ``blobs/<sha>.incomplete`` while
    streaming. If a previous warmup stalled (Xet fallback over a flaky
    link can hang at e.g. 141/500 MB and never advance), the .incomplete
    file sits around forever and re-running the warmup just resumes the
    same broken transfer. Removing it forces a clean restart.

    `stale_after_seconds` guards against deleting an actively-running
    download in another thread — we only nuke partials that haven't been
    touched recently.

    Returns the number of files removed (for logging / sanity).
    """
    import time as _time
    from pathlib import Path
    removed = 0
    try:
        hf_hub = Path(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))) / "hub"
        if not hf_hub.exists():
            return 0
        needle = repo_name_substring.replace("/", "_").replace("-", "").lower()
        now = _time.time()
        for child in hf_hub.iterdir():
            if not child.is_dir():
                continue
            cname = child.name.replace("-", "").lower()
            if needle not in cname:
                continue
            for f in child.rglob("*.incomplete"):
                try:
                    age = now - f.stat().st_mtime
                except OSError:
                    continue
                if age >= stale_after_seconds:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
    except Exception as exc:
        print(f"[warmup] stale-partial scan crashed (non-fatal): {exc!r}", flush=True)
    if removed:
        print(f"[warmup] wiped {removed} stale .incomplete file(s) under {repo_name_substring!r}", flush=True)
    return removed


class WarmupRequest(BaseModel):
    model: str  # "clip" | "whisper" | "yolo" | "ocr"


@app.post("/models/warmup")
def models_warmup(req: WarmupRequest, background_tasks: BackgroundTasks):
    """Force-load the named model in the background. Returns 202 immediately.

    Frontend should poll /models/status until loaded=true.
    BackgroundTask avoids blocking the request thread for cold-load (~30s).

    Each warmup wipes stale .incomplete partials for its target repo so a
    previous stalled transfer doesn't poison the resume. Safe because the
    stale-after threshold (60 s) is much longer than any real chunk-write
    interval during an actively-progressing download.
    """
    valid = {"clip", "whisper", "yolo", "ocr"}
    if req.model not in valid:
        raise HTTPException(status_code=400, detail=f"model must be one of {valid}")

    # Map model -> HF repo substring used by _wipe_stale_partials.
    _PARTIAL_NEEDLE = {
        "clip":    "clip-vit-large-patch14",
        "whisper": "faster-whisper",
        # yolo is bundled now — no HF cache to scrub
        "ocr":     "",  # rapidocr ships inside the wheel
    }
    _wipe_stale_partials(_PARTIAL_NEEDLE.get(req.model, ""))

    def _warmup_clip():
        try:
            import numpy as _np
            from PIL import Image as _Img
            dummy = _Img.fromarray(_np.zeros((1, 1, 3), dtype=_np.uint8))
            core.encode_image(dummy)
            print("[warmup] CLIP loaded")
        except Exception as exc:
            print(f"[warmup] CLIP error: {exc}")

    def _warmup_whisper():
        try:
            audio_ingest._get_whisper_model()
            print("[warmup] Whisper loaded")
        except Exception as exc:
            print(f"[warmup] Whisper error: {exc}")

    def _warmup_yolo():
        try:
            yolo_detect._get_model()
            print("[warmup] YOLO loaded")
        except Exception as exc:
            print(f"[warmup] YOLO error: {exc}")

    def _warmup_ocr():
        try:
            ocr_detect._get_reader()
            print("[warmup] OCR loaded")
        except Exception as exc:
            print(f"[warmup] OCR error: {exc}")

    runners = {
        "clip": _warmup_clip,
        "whisper": _warmup_whisper,
        "yolo": _warmup_yolo,
        "ocr": _warmup_ocr,
    }
    background_tasks.add_task(runners[req.model])
    return {"message": f"Warming up {req.model}", "model": req.model}


@app.post("/admin/wipe_data")
def admin_wipe_data(purge_caches: bool = False):
    """Danger: wipe the entire SovLens app data directory.

    Deletes the LanceDB index, logs, HLS cache, YOLO crops, folders.json,
    progress.json — everything under platform_utils.get_app_data_dir().

    When ``purge_caches=true`` is supplied, ALSO removes the HuggingFace,
    Whisper, and EasyOCR caches under the user home so the next launch
    behaves as a fresh install (models redownload on first use). These
    caches may be shared with other AI tools on the machine, so the flag
    is opt-in.

    The backend process exits immediately after responding so file locks
    (LanceDB, log handlers) release cleanly. The Tauri shell will see the
    sidecar die; the user restarts the app to get a fresh empty state.
    """
    import shutil
    import threading as _th

    data_dir = platform_utils.get_app_data_dir()
    extra_dirs: List[str] = []
    if purge_caches:
        home = os.path.expanduser("~")
        for sub in (
            os.environ.get("HF_HOME"),
            os.path.join(home, ".cache", "huggingface"),
            os.path.join(home, ".cache", "whisper"),
            os.path.join(home, ".EasyOCR"),
        ):
            if sub and os.path.isdir(sub) and sub not in extra_dirs:
                extra_dirs.append(sub)

    def _on_rm_error(func, path, _exc_info):
        """rmtree onerror: chmod writable + retry once.

        On Windows a stale mmap handle on a LanceDB segment leaves the file
        read-only-locked; the first unlink raises PermissionError. Flipping
        write bit + retrying clears the typical case.
        """
        import stat as _stat
        try:
            os.chmod(path, _stat.S_IWRITE | _stat.S_IREAD)
        except Exception:
            pass
        try:
            func(path)
        except Exception:
            pass

    def _wipe_and_exit():
        # Tiny delay so the HTTP response actually flushes to the client.
        import time as _time
        import gc as _gc
        _time.sleep(0.5)
        try:
            # Best-effort close of the LanceDB connection so Windows file
            # locks release before rmtree.
            try:
                core.db = None  # type: ignore[assignment]
            except Exception:
                pass
            # Force the LanceDB python object + its mmap'd Arrow segments to
            # be collected before we try to delete the underlying files.
            try:
                _gc.collect()
            except Exception:
                pass
            if os.path.isdir(data_dir):
                # Retry loop — Win mmap handle release is async, so the first
                # rmtree may still raise. Up to 5× × 500ms.
                for _attempt in range(5):
                    try:
                        shutil.rmtree(data_dir, onerror=_on_rm_error)
                        if not os.path.isdir(data_dir):
                            break
                    except Exception:
                        pass
                    _time.sleep(0.5)
                    try:
                        _gc.collect()
                    except Exception:
                        pass
                # Final best-effort sweep so we never block exit.
                if os.path.isdir(data_dir):
                    shutil.rmtree(data_dir, ignore_errors=True)
            for extra in extra_dirs:
                try:
                    shutil.rmtree(extra, ignore_errors=True)
                except Exception:
                    pass
        finally:
            os._exit(0)

    _th.Thread(target=_wipe_and_exit, daemon=True).start()
    return {"status": "ok", "wiped": data_dir, "extra_wiped": extra_dirs}


if __name__ == "__main__":
    import sys
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=14793)
    parser.add_argument("--host", default="127.0.0.1")
    args, _ = parser.parse_known_args()
    # IMPORTANT: pass the `app` object (not "main:app" import string) and
    # reload=False. Under PyInstaller's frozen exe, reload=True + import-string
    # causes uvicorn's reloader to re-exec the bootloader → backend never binds.
    # access_log=False on Windows: when launched as a child of the Tauri
    # shell with no console, uvicorn's per-request log emit goes through
    # sys.stdout which is a captured pipe. logging.flush() raises
    # OSError(Errno 22, "Invalid argument") on every request once the
    # pipe buffer hits a non-trivial size, polluting backend.log with
    # repeated tracebacks (~30 lines per polled /models/status). The
    # access info is rarely useful at runtime; killing it cleans up the
    # log file and skips a syscall per request.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=False,  # reload=True under PyInstaller frozen exe re-execs the bootloader -> never binds
        log_level="info",
        access_log=False,
    )
