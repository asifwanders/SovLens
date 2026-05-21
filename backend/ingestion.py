import io
import os
import json
import uuid
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import core
import config
import audio_ingest
import yolo_detect
import ocr_detect
import platform_utils
from PIL import Image

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIC_SUPPORTED = True
except ImportError:
    _HEIC_SUPPORTED = False


def is_heic_supported() -> bool:
    """Return True if pillow-heif is installed and HEIC/HEIF decoding is available."""
    return _HEIC_SUPPORTED
from scenedetect import detect, ContentDetector
import imageio_ffmpeg

# ---------------------------------------------------------------------------
# Config constants — defaults used when config is unavailable
# ---------------------------------------------------------------------------

_DEFAULT_MIN_SCENE_FOR_SAMPLING_S = 5.0   # scenes shorter than this get only the cut frame
_DEFAULT_FRAME_SAMPLE_INTERVAL_S = 3.0    # sample one extra frame every N seconds within long scenes
_DEFAULT_MAX_FRAMES_PER_SCENE = 20        # hard cap per scene to prevent runaway on static shots
_DEFAULT_PHASH_THRESHOLD = 5              # max Hamming distance to treat two frames as duplicates
_MAX_CROPS_PER_VIDEO = 400               # hard ceiling on YOLO crop rows per video; prevents DB row explosion on long videos at Extreme level

# Keep legacy names as aliases so any external callers don't break immediately
MIN_SCENE_FOR_SAMPLING_S = _DEFAULT_MIN_SCENE_FOR_SAMPLING_S
FRAME_SAMPLE_INTERVAL_S = _DEFAULT_FRAME_SAMPLE_INTERVAL_S
MAX_FRAMES_PER_SCENE = _DEFAULT_MAX_FRAMES_PER_SCENE
PHASH_THRESHOLD = _DEFAULT_PHASH_THRESHOLD

DB_BATCH_SIZE = 10              # flush to LanceDB after accumulating this many records


def _get_ingest_params() -> Dict[str, Any]:
    """Return current level params, falling back to defaults on any error."""
    try:
        return config.get_current_level_params()
    except Exception:
        return {
            "frame_sample_interval_s": _DEFAULT_FRAME_SAMPLE_INTERVAL_S,
            "max_frames_per_scene": _DEFAULT_MAX_FRAMES_PER_SCENE,
            "min_scene_for_sampling_s": _DEFAULT_MIN_SCENE_FOR_SAMPLING_S,
            "phash_threshold": _DEFAULT_PHASH_THRESHOLD,
            "audio_enabled": False,
            "yolo_enabled": False,
            "whisper_model": "base",
        }

# Progress checkpoint stored in the per-user app data dir (cross-platform)
_PROGRESS_PATH = os.path.join(platform_utils.get_app_data_dir(), "progress.json")


# ---------------------------------------------------------------------------
# Progress checkpoint helpers
# ---------------------------------------------------------------------------

def clear_progress_file() -> None:
    """Wipe progress.json so all videos are treated as un-indexed on next run."""
    if os.path.exists(_PROGRESS_PATH):
        try:
            os.remove(_PROGRESS_PATH)
        except OSError as e:
            print(f"[ingestion] could not remove progress file: {e}")


def _load_progress() -> Dict:
    """Load (or initialise) the per-video progress checkpoint from disk."""
    if os.path.exists(_PROGRESS_PATH):
        try:
            with open(_PROGRESS_PATH, "r") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def _save_progress(progress: Dict) -> None:
    """Atomically persist the progress checkpoint to disk."""
    os.makedirs(os.path.dirname(_PROGRESS_PATH), exist_ok=True)
    tmp = _PROGRESS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(progress, fh, indent=2)
    os.replace(tmp, _PROGRESS_PATH)


# ---------------------------------------------------------------------------
# Frame extraction helpers
# ---------------------------------------------------------------------------

# Resolved once at module load so every call reuses the same path
_FFMPEG_EXE = platform_utils.get_ffmpeg_exe()


def _safe_basename(p: str) -> str:
    """Return a filesystem-safe stem of *p* for use in ffmpeg image2 patterns.

    Strips ``%`` and other printf format-spec characters (``%04d`` etc.) from
    the filename stem so the ffmpeg image2 muxer does not misinterpret them.
    Replaced characters become underscores.
    """
    stem = os.path.splitext(os.path.basename(p))[0]
    # Replace any character that could be a printf format specifier or cause
    # shell/ffmpeg pattern misinterpretation.
    return re.sub(r"[%]", "_", stem)


def _extract_single_frame(
    video_path: str, timestamp: float, out_file: str
) -> bool:
    """Extract one frame at `timestamp` seconds via ffmpeg; returns True on success.

    Kept as fallback for when the batch path fails.
    """
    cmd = [
        _FFMPEG_EXE, "-y", "-ss", str(timestamp), "-i", video_path,
        "-vframes", "1", "-q:v", "2", out_file,
    ]
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 60}
    if platform_utils.IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        result = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and os.path.exists(out_file)


def _split_jpeg_stream(data: bytes) -> List[bytes]:
    """Parse a raw JPEG-concatenated byte stream into individual JPEG blobs.

    LEGACY / no longer used in the hot path — superseded by _extract_frames_batch
    which writes numbered image2 files directly so no stream parsing is needed.
    Kept for reference; safe to remove in a future cleanup pass.

    ffmpeg image2pipe with mjpeg codec emits frames back-to-back with no
    framing; each frame starts with \\xff\\xd8 (SOI) and ends with \\xff\\xd9 (EOI).
    WARNING: naive SOI/EOI scan can break on JPEGs with embedded EXIF/JFIF thumbnails.
    """
    frames: List[bytes] = []
    start = 0
    while True:
        soi = data.find(b"\xff\xd8", start)
        if soi == -1:
            break
        eoi = data.find(b"\xff\xd9", soi + 2)
        if eoi == -1:
            break
        frames.append(data[soi : eoi + 2])
        start = eoi + 2
    return frames


def _extract_frames_batch_chunk(
    video_path: str,
    timestamp_pairs: List[Tuple[float, bool]],
    output_dir: str,
    chunk_offset: int,
) -> List[Dict]:
    """Extract one chunk (up to 200 timestamps) via ffmpeg image2 numbered output.

    Uses -f image2 so ffmpeg writes individual .jpg files directly — no stream
    parsing required, avoiding fragility with EXIF/JFIF thumbnails.
    Returns list of {"path", "timestamp", "is_primary"} dicts, or [] on failure.
    """
    if not timestamp_pairs:
        return []

    half = 0.05
    clauses = [
        f"between(t\\,{max(0.0, ts - half):.6f}\\,{ts + half:.6f})"
        for ts, _ in timestamp_pairs
    ]
    select_expr = "+".join(clauses)
    filter_str = f"select='{select_expr}'"

    video_basename = _safe_basename(video_path)
    # Use a pattern that encodes the chunk offset so parallel chunks don't collide
    out_pattern = os.path.join(output_dir, f"{video_basename}_c{chunk_offset}_%04d.jpg")

    cmd = [
        _FFMPEG_EXE, "-y", "-i", video_path,
        "-vf", filter_str,
        "-vsync", "vfr",
        "-f", "image2",
        "-frame_pts", "0",
        "-q:v", "2",
        "-loglevel", "error",
        out_pattern,
    ]

    try:
        result = platform_utils.run_subprocess(cmd)
    except Exception as exc:
        print(f"[ingestion] batch ffmpeg launch failed: {exc}")
        return []

    if result.returncode != 0:
        print(f"[ingestion] batch ffmpeg error: {result.stderr[:300]}")
        return []

    # Glob output files in sorted order — ffmpeg names them %04d starting at 0001
    import glob
    glob_pattern = os.path.join(output_dir, f"{video_basename}_c{chunk_offset}_*.jpg")
    out_files = sorted(glob.glob(glob_pattern))

    if len(out_files) != len(timestamp_pairs):
        print(
            f"[ingestion] batch extract mismatch: expected {len(timestamp_pairs)} frames, "
            f"got {len(out_files)} — falling back to per-frame extraction."
        )
        # Clean up partial files so fallback starts clean
        for f in out_files:
            try:
                os.remove(f)
            except OSError:
                pass
        return []

    results: List[Dict] = []
    for out_file, (ts, is_primary) in zip(out_files, timestamp_pairs):
        results.append({"path": out_file, "timestamp": ts, "is_primary": is_primary})

    return results


def _extract_frames_batch(
    video_path: str,
    timestamp_pairs: List[Tuple[float, bool]],
    output_dir: str,
) -> List[Dict]:
    """Extract all requested frames in a single ffmpeg invocation via image2 numbered files.

    Splits into chunks of 200 timestamps when the select expression would become
    very large (>100k chars or >200 timestamps) to avoid ffmpeg command-line limits.
    Returns list of {"path", "timestamp", "is_primary"} dicts; returns empty list
    on any failure so the caller can fall back to per-frame extraction.
    """
    if not timestamp_pairs:
        return []

    CHUNK_SIZE = 200

    # Fast path: single chunk
    half = 0.05
    clauses = [
        f"between(t\\,{max(0.0, ts - half):.6f}\\,{ts + half:.6f})"
        for ts, _ in timestamp_pairs
    ]
    select_expr = "+".join(clauses)

    if len(select_expr) <= 100_000 and len(timestamp_pairs) <= CHUNK_SIZE:
        return _extract_frames_batch_chunk(video_path, timestamp_pairs, output_dir, 0)

    # Chunked path: split into groups of CHUNK_SIZE
    results: List[Dict] = []
    for chunk_idx in range(0, len(timestamp_pairs), CHUNK_SIZE):
        chunk = timestamp_pairs[chunk_idx:chunk_idx + CHUNK_SIZE]
        chunk_results = _extract_frames_batch_chunk(
            video_path, chunk, output_dir, chunk_idx
        )
        if len(chunk_results) != len(chunk):
            # Chunk failed — signal failure so caller uses per-frame fallback
            return []
        results.extend(chunk_results)

    return results


def _build_frame_timestamps(
    scene_list: list,
    params: Optional[Dict[str, Any]] = None,
) -> List[Tuple[float, bool]]:
    """
    Convert PySceneDetect scene list into (timestamp, is_primary) pairs.

    For each scene, always include the cut frame.  For scenes longer than
    min_scene_for_sampling_s, also sample every frame_sample_interval_s seconds
    up to max_frames_per_scene total per scene.
    Returns list sorted by timestamp; first entry overall is marked is_primary.
    """
    if params is None:
        params = _get_ingest_params()

    min_scene = params.get("min_scene_for_sampling_s", _DEFAULT_MIN_SCENE_FOR_SAMPLING_S)
    interval = params.get("frame_sample_interval_s", _DEFAULT_FRAME_SAMPLE_INTERVAL_S)
    max_frames = params.get("max_frames_per_scene", _DEFAULT_MAX_FRAMES_PER_SCENE)

    timestamps: List[Tuple[float, bool]] = []

    for scene_idx, scene in enumerate(scene_list):
        start_s = scene[0].get_seconds()
        end_s = scene[1].get_seconds()
        duration = end_s - start_s

        scene_ts: List[float] = [start_s]

        if duration >= min_scene:
            t = start_s + interval
            while t < end_s and len(scene_ts) < max_frames:
                scene_ts.append(t)
                t += interval

        for ts in scene_ts:
            timestamps.append((ts, False))  # is_primary assigned below

    timestamps.sort(key=lambda x: x[0])

    # Re-mark the very first frame as primary
    if timestamps:
        timestamps[0] = (timestamps[0][0], True)

    return timestamps


def extract_video_frames(
    video_path: str,
    output_dir: str,
    params: Optional[Dict[str, Any]] = None,
) -> List[Dict]:
    """
    Return list of {"path", "timestamp", "is_primary"} dicts for all selected frames.

    Uses PySceneDetect cuts as baseline, with intra-scene sampling for long scenes.
    Attempts a single-invocation batch ffmpeg extraction; falls back to per-frame
    extraction if the batch path returns 0 frames or a count mismatch.
    """
    if params is None:
        params = _get_ingest_params()

    try:
        scene_list = detect(video_path, ContentDetector())

        if not scene_list:
            # No scene cuts — extract first frame only
            out_file = os.path.join(output_dir, "frame_0.jpg")
            if _extract_single_frame(video_path, 0.0, out_file):
                return [{"path": out_file, "timestamp": 0.0, "is_primary": True}]
            return []

        timestamp_pairs = _build_frame_timestamps(scene_list, params=params)

        # Try fast batch path first
        frames_info = _extract_frames_batch(video_path, timestamp_pairs, output_dir)

        if frames_info:
            return frames_info

        # Fallback: one ffmpeg subprocess per frame (original behaviour)
        print(f"[ingestion] falling back to per-frame extraction for {video_path}")
        frames_info = []
        for i, (ts, is_primary) in enumerate(timestamp_pairs):
            out_file = os.path.join(output_dir, f"frame_{i}.jpg")
            if _extract_single_frame(video_path, ts, out_file):
                frames_info.append({"path": out_file, "timestamp": ts, "is_primary": is_primary})

        return frames_info

    except Exception as e:
        print(f"Error extracting frames from {video_path}: {e}")
        return []


# ---------------------------------------------------------------------------
# pHash-based deduplication
# ---------------------------------------------------------------------------

def _dedup_frames_by_phash(
    frames: List[Dict],
    threshold: int = _DEFAULT_PHASH_THRESHOLD,
) -> List[Dict]:
    """
    Remove near-duplicate frames using perceptual hash Hamming distance.

    Frames are assumed sorted by timestamp.  A frame is dropped if its pHash
    is within *threshold* bits of the most recently *kept* frame's hash.
    The is_primary flag on the first kept frame is preserved.
    """
    if not frames:
        return frames

    kept: List[Dict] = []
    prev_hash: Optional[int] = None

    for frame in frames:
        try:
            img = Image.open(frame["path"]).convert("RGB")
            h = core.compute_phash(img)
        except Exception:
            # If we cannot hash it, keep it rather than silently drop
            kept.append(frame)
            prev_hash = None
            continue

        if prev_hash is None or core.phash_hamming(h, prev_hash) >= threshold:
            frame["_phash"] = h
            frame["_cached_pil"] = img  # cache so process_video doesn't re-open
            kept.append(frame)
            prev_hash = h
        # else: near-duplicate — skip

    return kept


# ---------------------------------------------------------------------------
# Media processors
# ---------------------------------------------------------------------------

def process_image(filepath: str) -> Optional[Dict]:
    """Encode a single image file and return a DB-ready record dict.

    *filepath* is normalized to canonical form before storage.
    """
    filepath = platform_utils.normalize_path(filepath)
    try:
        img = Image.open(filepath).convert("RGB")
        vec = core.encode_image(img)
        params = _get_ingest_params()
        text_snippet = ""
        if params.get("ocr_enabled") and ocr_detect.OCR_AVAILABLE:
            text_snippet = ocr_detect.extract_text(img)
        return {
            "vector": vec,
            "id": str(uuid.uuid4()),
            "path": filepath,
            "thumbnail": filepath,
            "type": "image",
            "timestamp": 0.0,
            "is_primary": True,
            "video_id": "",
            "text_snippet": text_snippet,
        }
    except Exception as e:
        print(f"Error processing image {filepath}: {e}")
        return None


def process_video(filepath: str, video_id: Optional[str] = None) -> List[Dict]:
    """
    Extract, deduplicate, and batch-encode all frames for one video file.

    All frames from the same video share one video_id UUID.
    If video_id is not provided, a new UUID is generated.
    *filepath* is normalized to canonical form before storage.

    Respects current level params:
    - frame sampling knobs
    - audio_enabled → auto-triggers audio_ingest.process_video_audio
    - yolo_enabled  → detects objects, crops, embeds each crop as extra rows
    """
    params = _get_ingest_params()
    filepath = platform_utils.normalize_path(filepath)
    if video_id is None:
        video_id = str(uuid.uuid4())
    thumb_dir = os.path.join(platform_utils.get_app_data_dir(), "thumbnails", video_id)
    os.makedirs(thumb_dir, exist_ok=True)

    frames = extract_video_frames(filepath, thumb_dir, params=params)
    if not frames:
        return []

    # Sort by timestamp before dedup
    frames.sort(key=lambda f: f["timestamp"])
    frames = _dedup_frames_by_phash(frames, threshold=params.get("phash_threshold", _DEFAULT_PHASH_THRESHOLD))

    # Load all surviving frames into memory once, then batch-encode
    # Use cached PIL images from dedup pass when available to avoid double decode
    pil_images: List[Image.Image] = []
    valid_frames: List[Dict] = []
    for f in frames:
        cached = f.pop("_cached_pil", None)
        try:
            img = cached if cached is not None else Image.open(f["path"]).convert("RGB")
            pil_images.append(img)
            valid_frames.append(f)
        except Exception:
            pass

    if not pil_images:
        return []

    vectors = core.encode_images_batch(pil_images)

    ocr_on = params.get("ocr_enabled") and ocr_detect.OCR_AVAILABLE

    records: List[Dict] = []
    for (f, vec), pil in zip(zip(valid_frames, vectors), pil_images):
        text_snippet = ""
        if ocr_on:
            try:
                text_snippet = ocr_detect.extract_text(pil)
            except Exception as exc:
                print(f"[ingestion] OCR error on frame {f['path']}: {exc}")
        records.append({
            "vector": vec,
            "id": str(uuid.uuid4()),
            "path": filepath,
            "thumbnail": f["path"],
            "type": "video",
            "timestamp": f["timestamp"],
            "is_primary": f["is_primary"],
            "video_id": video_id,
            "text_snippet": text_snippet,
        })

    # -----------------------------------------------------------------------
    # YOLO object-detection pre-pass (Extreme level)
    # -----------------------------------------------------------------------
    if params.get("yolo_enabled") and yolo_detect.YOLO_AVAILABLE:
        yolo_crops_dir = os.path.join(platform_utils.get_app_data_dir(), "yolo_crops", video_id)
        os.makedirs(yolo_crops_dir, exist_ok=True)

        all_crops: List[Image.Image] = []
        crop_meta: List[Dict] = []  # parallel list: {timestamp, frame_idx, crop_idx}
        crops_inserted_for_this_video = 0
        _cap_logged = False

        for frame_idx, (f, img) in enumerate(zip(valid_frames, pil_images)):
            if crops_inserted_for_this_video >= _MAX_CROPS_PER_VIDEO:
                if not _cap_logged:
                    print(
                        f"[ingestion] YOLO crop cap ({_MAX_CROPS_PER_VIDEO}) reached for "
                        f"{filepath} — skipping crop collection for remaining frames."
                    )
                    _cap_logged = True
                continue

            try:
                crops = yolo_detect.detect_and_crop(img)
            except Exception as exc:
                print(f"[ingestion] YOLO detect error on frame {frame_idx}: {exc}")
                crops = []

            for crop_idx, crop in enumerate(crops):
                if crops_inserted_for_this_video >= _MAX_CROPS_PER_VIDEO:
                    break
                # Resize to max 400 px on longest side
                crop.thumbnail((400, 400), Image.LANCZOS)
                crop_path = os.path.join(yolo_crops_dir, f"{frame_idx}_{crop_idx}.jpg")
                try:
                    crop.save(crop_path, "JPEG", quality=85)
                except Exception:
                    continue
                all_crops.append(crop)
                crop_meta.append({
                    "timestamp": f["timestamp"],
                    "frame_idx": frame_idx,
                    "crop_idx": crop_idx,
                    "crop_path": crop_path,
                })
                crops_inserted_for_this_video += 1

        if all_crops:
            try:
                crop_vectors = core.encode_images_batch(all_crops)
                for meta, cvec in zip(crop_meta, crop_vectors):
                    records.append({
                        "vector": cvec,
                        "id": str(uuid.uuid4()),
                        "path": filepath,
                        "thumbnail": meta["crop_path"],
                        "type": "video",
                        "timestamp": meta["timestamp"],
                        "is_primary": False,
                        "video_id": video_id,
                        "text_snippet": "",
                    })
            except Exception as exc:
                print(f"[ingestion] YOLO crop embedding error: {exc}")

    elif params.get("yolo_enabled") and not yolo_detect.YOLO_AVAILABLE:
        print("[ingestion] yolo_enabled=True but ultralytics not installed — skipping YOLO pass.")

    # -----------------------------------------------------------------------
    # Auto audio ingest (High / Extreme levels)
    # -----------------------------------------------------------------------
    if params.get("audio_enabled"):
        if audio_ingest.WHISPER_AVAILABLE:
            try:
                whisper_model = params.get("whisper_model", "base")
                audio_ingest.process_video_audio(filepath, video_id, model_name=whisper_model)
            except Exception as exc:
                print(f"[ingestion] Auto audio ingest error for {filepath}: {exc}")
        else:
            print("[ingestion] audio_enabled=True but openai-whisper not installed — skipping audio ingest.")

    return records


# ---------------------------------------------------------------------------
# Folder ingestion with checkpoint-based resume
# ---------------------------------------------------------------------------

def get_media_files(folder_path: str) -> List[str]:
    """Recursively collect all supported image and video paths under folder_path."""
    supported_extensions = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".avi", ".mkv"}
    heic_extensions = {".heic", ".heif"}
    if _HEIC_SUPPORTED:
        supported_extensions |= heic_extensions
    else:
        print("[ingestion] pillow-heif not available — HEIC/HEIF files will be skipped. "
              "Install with: pip install pillow-heif")
    files: List[str] = []
    for root, _, filenames in os.walk(folder_path):
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() in supported_extensions:
                files.append(os.path.join(root, filename))
    return files


def process_folder(folder_path: str) -> None:
    """
    Ingest all media in folder_path into LanceDB.

    Resumes gracefully: completed files are skipped; in-progress videos are
    cleaned from the DB and re-ingested from scratch.
    """
    files = get_media_files(folder_path)
    existing_paths = core.get_existing_paths()
    progress = _load_progress()

    # Clean up any video that was interrupted mid-ingest
    for vpath, info in list(progress.items()):
        if info.get("status") == "in_progress":
            vid = info.get("video_id")
            if vid:
                print(f"Resuming interrupted video (cleaning partial rows): {vpath}")
                core.delete_rows_by_video_id(vid)
            # Remove from existing_paths so it gets re-processed
            existing_paths.discard(platform_utils.normalize_path(vpath))
            progress[vpath]["status"] = "restarting"

    records: List[Dict] = []

    def _flush() -> None:
        if records:
            core.add_media(records)
            records.clear()

    image_exts = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
    video_exts = {".mp4", ".mov", ".avi", ".mkv"}

    for f in files:
        f = platform_utils.normalize_path(f)
        file_status = progress.get(f, {}).get("status")
        # Skip if already in DB AND not an interrupted in-progress entry
        if f in existing_paths and file_status != "in_progress":
            continue

        ext = os.path.splitext(f)[1].lower()

        if ext in image_exts:
            rec = process_image(f)
            if rec:
                records.append(rec)
                progress[f] = {"status": "done", "video_id": ""}
                _save_progress(progress)

        elif ext in video_exts:
            # Hoist UUID generation here so progress.json and DB rows share the same id
            video_id = str(uuid.uuid4())
            # Mark in-progress before starting so a crash is recoverable
            progress[f] = {
                "status": "in_progress",
                "video_id": video_id,
                "frames_total": 0,
                "frames_done": 0,
            }
            _save_progress(progress)

            recs = process_video(f, video_id=video_id)

            if recs:
                records.extend(recs)
                _flush()
                progress[f] = {
                    "status": "done",
                    "video_id": video_id,
                    "frames_total": len(recs),
                    "frames_done": len(recs),
                }
            else:
                progress[f] = {
                    "status": "done",
                    "video_id": "",
                    "frames_total": 0,
                    "frames_done": 0,
                }
            _save_progress(progress)

        if len(records) >= DB_BATCH_SIZE:
            _flush()

    _flush()
