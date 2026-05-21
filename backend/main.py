from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import os
import uuid
import hashlib
import subprocess
import threading
import core
import ingestion
import index_build
import audio_ingest
import yolo_detect
import ocr_detect
import platform_utils
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

# Allow CORS so the Tauri frontend can communicate with FastAPI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    return {
        "is_ingesting": tasks_in_progress > 0,
        "tasks": tasks_in_progress,
        "whisper_available": audio_ingest.WHISPER_AVAILABLE,
        "heic_supported": ingestion.is_heic_supported(),
        "yolo_available": yolo_detect.YOLO_AVAILABLE,
        "ocr_available": ocr_detect.OCR_AVAILABLE,
        "current_model": core.get_model_name(),       # what's loaded in RAM right now
        "configured_model": requested,                 # what the active level requests
        "effective_model": effective,                  # what core will actually load
        "model_fallback": requested != effective,      # True when a fallback is in effect
    }

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

    if not range_header:
        def stream_file():
            with open(file_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        return StreamingResponse(stream_file(), headers=headers)

    start, end = 0, file_size - 1
    range_str = range_header.replace("bytes=", "").split("-")
    if range_str[0]:
        start = int(range_str[0])
    if len(range_str) > 1 and range_str[1]:
        end = int(range_str[1])

    if start >= file_size or end >= file_size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

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
        global tasks_in_progress
        with _tasks_lock:
            tasks_in_progress += 1
        try:
            ingestion.process_folder(folder_path)
            # Auto-build ANN index once table is large enough to benefit
            if index_build.should_build_index():
                index_build.build_ann_index()
        finally:
            with _tasks_lock:
                tasks_in_progress -= 1

    background_tasks.add_task(process_folder_wrapper, req.folder_path)
    return {"message": "Folder added and sync started"}

@app.post("/sync_all")
def sync_all(background_tasks: BackgroundTasks):
    folders = get_folders()
    if not folders:
        return {"message": "No folders to sync"}
        
    def sync_all_wrapper(folder_paths):
        global tasks_in_progress
        with _tasks_lock:
            tasks_in_progress += 1
        try:
            core.cleanup_missing_media()
            for fp in folder_paths:
                if os.path.exists(fp):
                    ingestion.process_folder(fp)
            # Auto-build ANN index once table is large enough to benefit
            if index_build.should_build_index():
                index_build.build_ann_index()
        finally:
            with _tasks_lock:
                tasks_in_progress -= 1

    background_tasks.add_task(sync_all_wrapper, folders)
    return {"message": "Syncing all folders started"}


class ReindexFolderRequest(BaseModel):
    folder_path: str


def _reindex_all_wrapper() -> None:
    global tasks_in_progress
    with _tasks_lock:
        tasks_in_progress += 1
    try:
        folders = get_folders()
        removed = core.purge_all()
        print(f"[reindex_all] Purged {removed} rows from LanceDB.")
        ingestion.clear_progress_file()
        for folder in folders:
            if os.path.exists(folder):
                ingestion.process_folder(folder)
    except Exception as exc:
        print(f"[reindex_all] Error: {exc}")
    finally:
        with _tasks_lock:
            tasks_in_progress -= 1


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
    global tasks_in_progress
    with _tasks_lock:
        tasks_in_progress += 1
    try:
        core.delete_rows_under_folder(folder_path)
        if os.path.exists(folder_path):
            ingestion.process_folder(folder_path)
        else:
            print(f"[reindex_folder] Folder does not exist (no-op): {folder_path}")
    except Exception as exc:
        print(f"[reindex_folder] Error: {exc}")
    finally:
        with _tasks_lock:
            tasks_in_progress -= 1


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
            detail="Whisper not installed. Run: pip install openai-whisper",
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
    """Return current analysis level, its params, and available levels."""
    return {
        "level": config.get_config()["level"],
        "level_data": config.get_current_level_params(),
        "available_levels": config.list_levels(),
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
    """Remove folders from registry + delete their rows from DB."""
    with _tasks_lock:
        if tasks_in_progress > 0:
            raise HTTPException(status_code=409, detail="Ingest in progress. Wait then retry.")
    folders = get_folders()
    removed = []
    for p in req.paths:
        norm = platform_utils.normalize_path(p)
        folders = [f for f in folders if f != p and platform_utils.normalize_path(f) != norm]
        try:
            core.delete_rows_under_folder(norm)
            removed.append(p)
        except Exception as e:
            print(f"[/folders] delete error for {p}: {e}")
    save_folders(folders)
    return {"removed": removed, "remaining_count": len(folders)}


class AddFilesRequest(BaseModel):
    filepaths: List[str]


def _add_files_wrapper(filepaths: List[str]) -> None:
    global tasks_in_progress
    with _tasks_lock:
        tasks_in_progress += 1
    try:
        for fp in filepaths:
            ext = os.path.splitext(fp)[1].lower()
            try:
                if ext in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
                    ingestion.process_video(fp, str(uuid.uuid4()))
                else:
                    ingestion.process_image(fp)
            except Exception as e:
                print(f"[/add_files] failed {fp}: {e}")
    finally:
        with _tasks_lock:
            tasks_in_progress -= 1


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

def _hf_model_downloaded(name: str) -> bool:
    """Check if a HuggingFace / sentence-transformers model is in the local cache."""
    from pathlib import Path
    hf_cache = Path(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")))
    st_cache = Path(os.path.expanduser("~/.cache/torch/sentence_transformers"))
    needle = name.replace("/", "_")
    for base in (hf_cache, st_cache):
        if not base.exists():
            continue
        for child in base.rglob("*"):
            if child.is_dir() and needle in child.name:
                return True
    return False


def _whisper_downloaded() -> bool:
    """Check if any Whisper model is in the local cache."""
    from pathlib import Path
    cache = Path(os.path.expanduser("~/.cache/whisper"))
    if not cache.exists():
        return False
    return any(cache.iterdir())


def _yolo_downloaded() -> bool:
    """Check if yolov8n.pt is on disk (CWD or common locations)."""
    from pathlib import Path
    candidates = [
        Path("yolov8n.pt"),
        Path(os.path.expanduser("~/.cache/ultralytics/assets/yolov8n.pt")),
        Path(os.path.expanduser("~/.cache/ultralytics/yolov8n.pt")),
        Path(platform_utils.get_app_data_dir()) / "yolov8n.pt",
    ]
    return any(p.exists() for p in candidates)


def _easyocr_downloaded() -> bool:
    """Check if EasyOCR model files are present."""
    from pathlib import Path
    cache = Path(os.path.expanduser("~/.EasyOCR"))
    if not cache.exists():
        return False
    return any(cache.rglob("*.pth")) or any(cache.rglob("*.pt"))


def _clip_status() -> dict:
    cfg_params = config.get_current_level_params()
    requested = cfg_params.get("model", "clip-ViT-L-14")
    effective = core.resolve_model_name(requested)
    return {
        "name": effective,
        "loaded": core.get_model_name() == effective,
        "downloaded": _hf_model_downloaded(effective),
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
        "name": "easyocr",
        "available": ocr_detect.OCR_AVAILABLE,
        "loaded": ocr_detect.OCR_AVAILABLE and _ocr_loaded(),
        "downloaded": _easyocr_downloaded(),
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


class WarmupRequest(BaseModel):
    model: str  # "clip" | "whisper" | "yolo" | "ocr"


@app.post("/models/warmup")
def models_warmup(req: WarmupRequest, background_tasks: BackgroundTasks):
    """Force-load the named model in the background. Returns 202 immediately.

    Frontend should poll /models/status until loaded=true.
    BackgroundTask avoids blocking the request thread for cold-load (~30s).
    """
    valid = {"clip", "whisper", "yolo", "ocr"}
    if req.model not in valid:
        raise HTTPException(status_code=400, detail=f"model must be one of {valid}")

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=14793, reload=True)
