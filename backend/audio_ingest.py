# Audio transcript pipeline. Requires `faster-whisper` (pip install faster-whisper).
# Stub mode active when not installed.
#
# When faster-whisper IS available, this module:
#   1. Extracts 16kHz mono WAV from a video using ffmpeg.
#   2. Transcribes via faster-whisper (CTranslate2 engine, no torch) to produce
#      timed text segments. Silero VAD filter is enabled to skip silence.
#   3. Embeds each segment's text with the CLIP text encoder already loaded in core.py.
#   4. Inserts rows into the shared LanceDB "media" table (type="audio_segment").
#
# Schema note (v3): audio segment rows use the dedicated `text_snippet` column
# (added by migrate_schema_v3()) for transcript text. The `thumbnail` field is
# set to "" for audio_segment rows — it is NOT repurposed as a text carrier.

import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple
import platform_utils

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Availability flag — importers should check this before calling any function.
# ---------------------------------------------------------------------------

try:
    from faster_whisper import WhisperModel as _WhisperModel  # type: ignore
    WHISPER_AVAILABLE: bool = True
except ImportError:
    _WhisperModel = None  # type: ignore
    WHISPER_AVAILABLE = False

_INSTALL_HINT = "faster-whisper not installed. Run: pip install faster-whisper"

# ---------------------------------------------------------------------------
# Lazy whisper model cache
# ---------------------------------------------------------------------------

_whisper_model = None
_whisper_model_name: str = ""
_whisper_load_lock = threading.Lock()


def _detect_device() -> Tuple[str, str]:
    """Pick (device, compute_type) for faster-whisper without importing torch.

    - mac: CPU + int8 (no MPS support in CTranslate2)
    - windows/linux + CUDA: cuda + float16
    - else: CPU + int8

    CUDA presence is probed via ctranslate2.get_cuda_device_count(), so we
    don't pull torch into this module.
    """
    if sys.platform == "darwin":
        return ("cpu", "int8")
    try:
        import ctranslate2  # type: ignore
        if ctranslate2.get_cuda_device_count() > 0:
            return ("cuda", "float16")
    except Exception as exc:
        logger.debug("ctranslate2 CUDA probe failed: %s", exc)
    return ("cpu", "int8")


def _get_whisper_model(name: str = "base"):
    """Return a cached faster-whisper WhisperModel, loading on first call.

    Device/compute_type are picked by _detect_device():
      - mac → cpu/int8 (no MPS)
      - cuda available → cuda/float16
      - else → cpu/int8

    Thread-safe via double-checked locking. faster-whisper sessions are not
    safe for concurrent .transcribe(), but ingestion serializes audio passes,
    so a single cached instance is sufficient.
    """
    global _whisper_model, _whisper_model_name
    if not WHISPER_AVAILABLE:
        raise NotImplementedError(_INSTALL_HINT)
    if _whisper_model is None or _whisper_model_name != name:
        with _whisper_load_lock:
            # Re-check inside lock to handle concurrent first callers
            if _whisper_model is None or _whisper_model_name != name:
                device, compute_type = _detect_device()
                logger.info(
                    "Loading faster-whisper model name=%s device=%s compute_type=%s",
                    name, device, compute_type,
                )
                _whisper_model = _WhisperModel(name, device=device, compute_type=compute_type)
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
        NotImplementedError: When ``faster-whisper`` is not installed, because
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
    """Transcribe *wav_path* with faster-whisper and return timed segment dicts.

    Args:
        wav_path: Path to a 16 kHz mono WAV file.
        model_name: Whisper model size ("tiny", "base", "small", "medium",
            "large-v3"). faster-whisper auto-downloads from HF
            ``Systran/faster-whisper-<size>`` on first use.

    Returns:
        List of dicts: ``{"text": str, "start": float, "end": float}``.
        Returns an empty list if transcription produces no segments.

    Raises:
        NotImplementedError: When ``faster-whisper`` is not installed.
    """
    if not WHISPER_AVAILABLE:
        raise NotImplementedError(_INSTALL_HINT)

    model = _get_whisper_model(model_name)
    # faster-whisper returns (segments_iterator, info). Iterator is lazy —
    # iterate fully to materialize results.
    segments_iter, _info = model.transcribe(wav_path, beam_size=5, vad_filter=True)
    out: List[Dict[str, Any]] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        out.append({"text": text, "start": float(seg.start), "end": float(seg.end)})
    return out


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
        NotImplementedError: When ``faster-whisper`` is not installed.
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
