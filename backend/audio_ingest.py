# Audio transcript pipeline. Requires `openai-whisper` (pip install openai-whisper).
# Stub mode active when not installed.
#
# When whisper IS available, this module:
#   1. Extracts 16kHz mono WAV from a video using ffmpeg.
#   2. Transcribes via Whisper to produce timed text segments.
#   3. Embeds each segment's text with the CLIP text encoder already loaded in core.py.
#   4. Inserts rows into the shared LanceDB "media" table (type="audio_segment").
#
# Schema note (v3): audio segment rows use the dedicated `text_snippet` column
# (added by migrate_schema_v3()) for transcript text. The `thumbnail` field is
# set to "" for audio_segment rows — it is NOT repurposed as a text carrier.

import logging
import os
import threading
from typing import Any, Dict, List, Optional
import platform_utils

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Availability flag — importers should check this before calling any function.
# ---------------------------------------------------------------------------

try:
    import whisper as _whisper  # type: ignore
    WHISPER_AVAILABLE: bool = True
except ImportError:
    WHISPER_AVAILABLE = False

_INSTALL_HINT = "Whisper not installed. Run: pip install openai-whisper"

# ---------------------------------------------------------------------------
# Lazy whisper model cache
# ---------------------------------------------------------------------------

_whisper_model = None
_whisper_model_name: str = ""
_whisper_load_lock = threading.Lock()


def _get_whisper_model(name: str = "base"):
    """Return a cached Whisper model, loading it on first call.

    Uses platform_utils.WHISPER_DEVICE which excludes MPS (Whisper correctness
    issues on Apple MPS) and picks CUDA when available.
    Thread-safe via double-checked locking: avoids redundant loads under concurrency.
    """
    global _whisper_model, _whisper_model_name
    if _whisper_model is None or _whisper_model_name != name:
        with _whisper_load_lock:
            # Re-check inside lock to handle concurrent first callers
            if _whisper_model is None or _whisper_model_name != name:
                _whisper_model = _whisper.load_model(name, device=platform_utils.WHISPER_DEVICE)
                _whisper_model_name = name
    return _whisper_model

# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------


def extract_audio(video_path: str, out_wav: str) -> bool:
    """Extract a 16 kHz mono WAV from *video_path* and write to *out_wav*.

    Uses the system ``ffmpeg`` binary — no Python dependency beyond subprocess.

    Args:
        video_path: Absolute path to the source video file.
        out_wav: Destination path for the extracted WAV file.

    Returns:
        True on success, False if ffmpeg is unavailable or the extraction fails.

    Raises:
        NotImplementedError: When ``openai-whisper`` is not installed, because
            audio extraction is only useful when transcription is also possible.
    """
    if not WHISPER_AVAILABLE:
        raise NotImplementedError(_INSTALL_HINT)

    ffmpeg_bin = platform_utils.get_ffmpeg_exe()
    cmd = [
        ffmpeg_bin, "-y",
        "-i", video_path,
        "-ar", "16000",   # 16 kHz — required by Whisper
        "-ac", "1",        # mono
        "-vn",             # strip video stream
        out_wav,
    ]
    result = platform_utils.run_subprocess(cmd, timeout=300)
    if result.returncode != 0:
        logger.warning("ffmpeg extraction failed for %s: %s", video_path, result.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


def transcribe(wav_path: str, model_name: str = "base") -> List[Dict[str, Any]]:
    """Transcribe *wav_path* with Whisper and return timed segment dicts.

    Args:
        wav_path: Path to a 16 kHz mono WAV file.
        model_name: Whisper model size ("tiny", "base", "small", "medium", "large").
            "base" gives a good speed/accuracy trade-off for most use cases.

    Returns:
        List of dicts: ``{"text": str, "start": float, "end": float}``.
        Returns an empty list if transcription produces no segments.

    Raises:
        NotImplementedError: When ``openai-whisper`` is not installed.
    """
    if not WHISPER_AVAILABLE:
        raise NotImplementedError(_INSTALL_HINT)

    model = _get_whisper_model(model_name)
    result = model.transcribe(wav_path, fp16=False)
    segments = result.get("segments", [])
    return [
        {"text": seg["text"].strip(), "start": float(seg["start"]), "end": float(seg["end"])}
        for seg in segments
        if seg.get("text", "").strip()
    ]


# ---------------------------------------------------------------------------
# Full pipeline: extract → transcribe → embed → insert
# ---------------------------------------------------------------------------


def process_video_audio(video_path: str, video_id: str, model_name: Optional[str] = None) -> int:
    """Run the full audio pipeline for one video file.

    Extracts audio, transcribes, embeds each segment with CLIP's text encoder,
    and inserts rows into the shared LanceDB ``media`` table.

    Each segment row uses:
        - ``type = "audio_segment"``
        - ``timestamp = segment.start``
        - ``is_primary = False``  (so it is excluded from gallery views)
        - ``video_id = <video_id>``  (shared with the video's frame rows)
        - ``path = video_path``
        - ``thumbnail = segment.text``  (overloaded — see module docstring)

    Args:
        video_path: Absolute path to the video file.
        video_id: UUID that ties these audio rows to the video's frame rows.
        model_name: Whisper model size. If None, reads from config level params.

    Returns:
        Number of segments successfully inserted (0 on any failure).

    Raises:
        NotImplementedError: When ``openai-whisper`` is not installed.
    """
    if not WHISPER_AVAILABLE:
        raise NotImplementedError(_INSTALL_HINT)

    import uuid
    import core  # local import to avoid circular dependency at module load time

    # Resolve model name from config if not explicitly supplied
    if model_name is None:
        try:
            import config as _cfg
            model_name = _cfg.get_current_level_params().get("whisper_model", "base")
        except Exception:
            model_name = "base"

    tmp_wav = os.path.join(platform_utils.get_temp_dir(), f"sovlens_audio_{uuid.uuid4().hex}.wav")
    try:
        if not extract_audio(video_path, tmp_wav):
            logger.warning("Audio extraction failed for %s — skipping transcription.", video_path)
            return 0

        segments = transcribe(tmp_wav, model_name=model_name)
        if not segments:
            logger.info("No speech segments found in %s.", video_path)
            return 0

        texts = [seg["text"] for seg in segments]
        # Batch-encode all segment texts with the shared CLIP model
        vectors = core.encode_texts_batch(texts)

        records = []
        for seg, vec in zip(segments, vectors):
            records.append({
                "vector": vec,
                "id": f"{video_id}_audio_{uuid.uuid4().hex}",
                "path": video_path,
                "thumbnail": "",
                "type": "audio_segment",
                "timestamp": seg["start"],
                "is_primary": False,
                "video_id": video_id,
                "text_snippet": seg["text"],  # dedicated column added in schema v3
            })

        core.add_media(records)
        logger.info("Inserted %d audio segments for video_id=%s.", len(records), video_id)
        return len(records)

    finally:
        # Always remove the temp WAV to avoid leaving large files behind
        if os.path.exists(tmp_wav):
            os.remove(tmp_wav)


# ---------------------------------------------------------------------------
# Schema migration placeholder (no-op in stub mode)
# ---------------------------------------------------------------------------


def migrate_schema_v3() -> None:
    """Migrate the LanceDB schema to add a dedicated ``text_snippet`` column.

    Delegates to core.migrate_schema_v3() which does the actual table recreation.
    No-op when Whisper is not installed (schema migration not needed yet).
    """
    if not WHISPER_AVAILABLE:
        logger.debug("migrate_schema_v3: whisper not installed — skipping.")
        return

    import core  # local import to avoid circular dependency at module load time
    core.migrate_schema_v3()
