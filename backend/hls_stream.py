"""HLS on-demand streaming for SovLens.

Produces HLS playlists and MPEG-TS segments by transcoding only the requested
time window. Uses hardware acceleration where available (VideoToolbox on macOS,
NVENC on Windows with CUDA). Segments are cached under APP_DATA/hls/<video_sha>/.

Python 3.9+. No new pip dependencies.
"""

import hashlib
import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

import platform_utils

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HLS_CACHE_DIR: str = os.path.join(platform_utils.get_app_data_dir(), "hls")
SEGMENT_DURATION: int = 6          # seconds per segment
MAX_CACHE_BYTES: int = 10 * 1024 * 1024 * 1024  # 10 GB

# Per-segment locks: (cache_key, idx) -> threading.Lock
_seg_locks: Dict[Tuple[str, int], threading.Lock] = {}
_seg_locks_mutex = threading.Lock()

# Video ID registry: sha1(16char) -> normalized source path
_video_registry: Dict[str, str] = {}
_registry_lock = threading.Lock()


def register_video(src: str, video_id: Optional[str] = None) -> str:
    """Store *src* in the registry under *video_id* (and also its sha1 key).

    If *video_id* is None a stable sha1-based key is used.  Storing under the
    caller-supplied id lets the segment endpoint resolve paths when hls.js uses
    an arbitrary video_id (e.g. item.id from the media DB).

    Returns the sha1-based key.
    """
    norm = platform_utils.normalize_path(src)
    sha1_id = hashlib.sha1(norm.encode()).hexdigest()[:16]
    with _registry_lock:
        _video_registry[sha1_id] = norm
        if video_id is not None and video_id != sha1_id:
            _video_registry[video_id] = norm
    return sha1_id


def resolve_video_id(video_id: str) -> Optional[str]:
    """Return the registered source path for *video_id*, or None if not found."""
    with _registry_lock:
        return _video_registry.get(video_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cache_key(src: str) -> str:
    """SHA1 of (abspath + mtime_ns + size) — changes when file is edited."""
    st = os.stat(src)
    key = f"{os.path.abspath(src)}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha1(key.encode()).hexdigest()


def _cache_dir_for(src: str) -> str:
    return os.path.join(HLS_CACHE_DIR, _cache_key(src))


def _probe_duration(src: str) -> float:
    """Return video duration in seconds via ffprobe-style stderr parse."""
    ff = platform_utils.get_ffmpeg_exe()
    try:
        result = platform_utils.run_subprocess(
            [ff, "-i", src, "-hide_banner"],
            timeout=15,
        )
        # ffmpeg writes duration to stderr: "Duration: HH:MM:SS.ss"
        for line in result.stderr.splitlines():
            s = line.strip()
            if s.startswith("Duration:"):
                dur_str = s.split(",")[0].replace("Duration:", "").strip()
                parts = dur_str.split(":")
                if len(parts) == 3:
                    h, m, sec = parts
                    return float(h) * 3600 + float(m) * 60 + float(sec)
    except Exception:
        pass
    return 0.0


def _probe_video_codec(src: str) -> str:
    """Return the video codec name (lower-case) or '' on failure."""
    ff = platform_utils.get_ffmpeg_exe()
    try:
        result = platform_utils.run_subprocess(
            [ff, "-i", src, "-hide_banner"],
            timeout=15,
        )
        for line in result.stderr.splitlines():
            s = line.strip()
            if "Video:" in s:
                after = s.split("Video:", 1)[1].strip()
                return after.split()[0].strip(",").lower()
    except Exception:
        pass
    return ""


def _get_segment_lock(key: str, idx: int) -> threading.Lock:
    lock_key = (key, idx)
    with _seg_locks_mutex:
        if lock_key not in _seg_locks:
            _seg_locks[lock_key] = threading.Lock()
        return _seg_locks[lock_key]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_or_build_playlist(src: str, video_id: Optional[str] = None) -> str:
    """Return path to playlist.m3u8; create if missing.

    The playlist uses relative segment URLs so hls.js resolves them correctly
    against the playlist URL base.  Also registers the source in the video
    registry (under both the sha1 key and the optional caller-supplied
    *video_id*) so segments can be served without a ``path`` query parameter.
    """
    src = platform_utils.normalize_path(src)
    register_video(src, video_id=video_id)  # populate registry before anything else
    cache_dir = _cache_dir_for(src)
    playlist_path = os.path.join(cache_dir, "playlist.m3u8")
    meta_path = os.path.join(cache_dir, "meta.json")

    if os.path.exists(playlist_path) and os.path.exists(meta_path):
        # Touch mtime for LRU eviction tracking
        os.utime(cache_dir, None)
        return playlist_path

    os.makedirs(cache_dir, exist_ok=True)

    duration = _probe_duration(src)
    if duration <= 0:
        raise ValueError(f"Could not probe duration for: {src}")

    codec_in = _probe_video_codec(src)
    encoder_used = platform_utils.detect_hwaccel_encoder()

    # Number of segments
    import math
    n_segments = math.ceil(duration / SEGMENT_DURATION)

    # Build HLS playlist (EXT-X-VERSION 3 — widely supported)
    lines: List[str] = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for i in range(n_segments):
        seg_duration = min(SEGMENT_DURATION, duration - i * SEGMENT_DURATION)
        lines.append(f"#EXTINF:{seg_duration:.6f},")
        lines.append(f"segment_{i}.ts")
    lines.append("#EXT-X-ENDLIST")

    playlist_content = "\n".join(lines) + "\n"

    # Write atomically
    tmp_playlist = playlist_path + ".part"
    with open(tmp_playlist, "w", encoding="utf-8") as f:
        f.write(playlist_content)
    os.replace(tmp_playlist, playlist_path)

    meta = {
        "duration": duration,
        "segments": n_segments,
        "segment_duration": SEGMENT_DURATION,
        "codec_in": codec_in,
        "encoder_used": encoder_used,
        "src": src,
    }
    tmp_meta = meta_path + ".part"
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp_meta, meta_path)

    return playlist_path


def get_or_build_segment(src: str, idx: int) -> str:
    """Return path to segment_{idx}.ts; transcode the time window if missing.

    Uses a per-segment lock to prevent duplicate concurrent ffmpeg processes.
    Writes to a .part file then renames atomically.
    """
    src = platform_utils.normalize_path(src)
    cache_dir = _cache_dir_for(src)
    seg_path = os.path.join(cache_dir, f"segment_{idx}.ts")

    # Bump mtime FIRST — protects active builds from the LRU eviction pass.
    if os.path.isdir(cache_dir):
        os.utime(cache_dir, None)

    if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
        return seg_path

    lock = _get_segment_lock(_cache_key(src), idx)
    with lock:
        # Double-check after acquiring lock
        if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
            return seg_path

        os.makedirs(cache_dir, exist_ok=True)

        start = idx * SEGMENT_DURATION
        tmp_seg = seg_path + ".part"

        ff = platform_utils.get_ffmpeg_exe()
        decoder_args = platform_utils.detect_hwaccel_decoder()
        encoder_args = platform_utils.detect_hwaccel_encoder()

        # Compute the actual duration for this segment (handles last segment).
        meta_path = os.path.join(cache_dir, "meta.json")
        seg_actual_duration: float = float(SEGMENT_DURATION)
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as _mf:
                    _meta = json.load(_mf)
                total_dur: float = float(_meta.get("duration", 0))
                if total_dur > 0:
                    remaining = total_dur - start
                    seg_actual_duration = min(float(SEGMENT_DURATION), remaining)
            except Exception:
                pass

        cmd: List[str] = (
            [ff, "-y"]
            + decoder_args
            + ["-ss", str(start), "-t", str(seg_actual_duration), "-i", src]
            + encoder_args
            + [
                "-c:a", "aac", "-b:a", "128k",
                "-f", "mpegts",
                "-mpegts_copyts", "1",
                "-loglevel", "error",
                tmp_seg,
            ]
        )

        t0 = time.time()
        proc = platform_utils.run_subprocess(cmd, timeout=300)
        elapsed = time.time() - t0

        if proc.returncode != 0 or not os.path.exists(tmp_seg):
            if os.path.exists(tmp_seg):
                os.remove(tmp_seg)
            raise RuntimeError(
                f"ffmpeg failed for segment {idx} (rc={proc.returncode}): {proc.stderr[-500:]}"
            )

        os.replace(tmp_seg, seg_path)
        print(f"[hls_stream] segment_{idx}.ts built in {elapsed:.1f}s ({src})")

    # Evict after each new segment (non-blocking via thread)
    threading.Thread(target=evict_old_cache, daemon=True).start()

    return seg_path


def evict_old_cache(max_bytes: int = MAX_CACHE_BYTES) -> int:
    """Delete oldest HLS cache directories (by mtime) until total size < max_bytes.

    Returns the number of bytes freed.
    """
    if not os.path.isdir(HLS_CACHE_DIR):
        return 0

    # Collect (mtime, size, path) for each video cache dir
    entries: List[Tuple[float, int, str]] = []
    for name in os.listdir(HLS_CACHE_DIR):
        d = os.path.join(HLS_CACHE_DIR, name)
        if not os.path.isdir(d):
            continue
        try:
            mtime = os.path.getmtime(d)
            size = sum(
                os.path.getsize(os.path.join(d, f))
                for f in os.listdir(d)
                if os.path.isfile(os.path.join(d, f))
            )
            entries.append((mtime, size, d))
        except OSError:
            continue

    total = sum(e[1] for e in entries)
    if total <= max_bytes:
        return 0

    # Sort oldest first
    entries.sort(key=lambda e: e[0])
    freed = 0
    for mtime, size, d in entries:
        if total <= max_bytes:
            break
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            total -= size
            freed += size
            print(f"[hls_stream] evicted cache dir: {d} ({size // 1024 // 1024} MB)")
        except OSError:
            pass

    return freed


# Run eviction at import time (non-blocking)
threading.Thread(target=evict_old_cache, daemon=True).start()
