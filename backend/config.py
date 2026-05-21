"""Persistent analysis-level configuration for SovLens.

Stores user-selected ingestion quality level in a JSON file under the
platform app-data directory.  All public functions are thread-safe.

Python 3.9+.  No additional pip dependencies.
"""

import json
import os
import threading
from typing import Any, Dict, List, Optional

import platform_utils

CONFIG_PATH = os.path.join(platform_utils.get_app_data_dir(), "config.json")

# Level definitions — knobs that get applied to ingestion + model selection.
LEVELS: Dict[str, Dict[str, Any]] = {
    "low": {
        "label": "Low",
        "description": "Fast indexing. Best for casual photo libraries.",
        "frame_sample_interval_s": 10.0,
        "max_frames_per_scene": 5,
        "min_scene_for_sampling_s": 8.0,
        "phash_threshold": 8,
        "model": "clip-ViT-B-32",          # 512d, ~150MB, fastest
        "audio_enabled": False,
        "whisper_model": "base",
        "yolo_enabled": False,
        "ocr_enabled": False,
        "speed_estimate": "1x",
    },
    "medium": {
        "label": "Medium (Default)",
        "description": "Balanced. Good recall for most queries.",
        "frame_sample_interval_s": 3.0,
        "max_frames_per_scene": 20,
        "min_scene_for_sampling_s": 5.0,
        "phash_threshold": 5,
        "model": "clip-ViT-L-14",           # 768d, ~890MB
        "audio_enabled": False,
        "whisper_model": "base",
        "yolo_enabled": False,
        "ocr_enabled": False,
        "speed_estimate": "3-5x slower than Low",
    },
    "high": {
        "label": "High",
        "description": "Dense frame sampling + voice transcription.",
        "frame_sample_interval_s": 1.5,
        "max_frames_per_scene": 40,
        "min_scene_for_sampling_s": 3.0,
        "phash_threshold": 3,
        "model": "clip-ViT-L-14",
        "audio_enabled": True,
        "whisper_model": "base",
        "yolo_enabled": False,
        "ocr_enabled": True,
        "speed_estimate": "8-12x slower than Low",
    },
    "extreme": {
        "label": "Extreme",
        "description": "Finds tiny objects (an iPhone in a vacation clip). Heavy GPU + disk usage.",
        "frame_sample_interval_s": 0.5,
        "max_frames_per_scene": 100,
        "min_scene_for_sampling_s": 2.0,
        "phash_threshold": 2,
        "model": "clip-ViT-L-14",            # leave at L-14 unless H/14 / SigLIP installed by WS-S3
        "audio_enabled": True,
        "whisper_model": "small",            # higher accuracy than base
        "yolo_enabled": True,                # object detection pre-pass — WS-S4 implements
        "ocr_enabled": True,
        "speed_estimate": "20-40x slower than Low",
    },
}

DEFAULT_LEVEL = "medium"

_lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None


def _load_from_disk() -> Dict[str, Any]:
    """Read config.json from disk. Returns default config if missing or corrupt."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            level = data.get("level", DEFAULT_LEVEL)
            if level not in LEVELS:
                level = DEFAULT_LEVEL
            return {"level": level}
        except (json.JSONDecodeError, OSError):
            pass
    return {"level": DEFAULT_LEVEL}


def _save_to_disk(data: Dict[str, Any]) -> None:
    """Atomically write config dict to CONFIG_PATH using temp+rename."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get_config() -> Dict[str, Any]:
    """Return current config dict with keys ``level`` and ``level_data``.

    The result is cached in memory after the first disk read.
    """
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_disk()
        level = _cache["level"]
    return {"level": level, "level_data": LEVELS[level]}


def set_level(level: str) -> Dict[str, Any]:
    """Persist a new analysis level and update the in-memory cache.

    Args:
        level: One of the keys in ``LEVELS`` (``"low"``, ``"medium"``,
               ``"high"``, ``"extreme"``).

    Returns:
        Updated config dict (same shape as :func:`get_config`).

    Raises:
        ValueError: If *level* is not a recognised key in ``LEVELS``.
    """
    if level not in LEVELS:
        raise ValueError(
            f"Invalid level {level!r}. Must be one of: {list(LEVELS)}"
        )
    global _cache
    with _lock:
        new_data = {"level": level}
        _save_to_disk(new_data)
        _cache = new_data
    return {"level": level, "level_data": LEVELS[level]}


def get_current_level_params() -> Dict[str, Any]:
    """Return the params dict for the current level (the value from ``LEVELS``)."""
    cfg = get_config()
    return cfg["level_data"]


def list_levels() -> List[Dict[str, Any]]:
    """Return a list of level summaries for UI display.

    Each entry contains ``key``, ``label``, ``description``, and
    ``speed_estimate``.
    """
    return [
        {
            "key": key,
            "label": val["label"],
            "description": val["description"],
            "speed_estimate": val["speed_estimate"],
        }
        for key, val in LEVELS.items()
    ]
