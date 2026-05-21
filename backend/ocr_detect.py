"""EasyOCR-based text extraction from images/frames.

Lazy-loaded — model only loads on first call.
Returns concatenated extracted text per image; empty string if no text or OCR unavailable.
"""
import threading
from typing import Optional, List
from PIL import Image
import numpy as np
import platform_utils

_reader = None
_load_lock = threading.Lock()
OCR_AVAILABLE = False

try:
    import easyocr as _easyocr   # type: ignore
    OCR_AVAILABLE = True
except ImportError:
    pass

_DEFAULT_LANGS = ["en"]
_GPU = platform_utils.detect_torch_device() == "cuda"  # easyocr only treats cuda as GPU; mps falls back to CPU
_MIN_CONFIDENCE = 0.4
_MAX_CHARS_PER_IMAGE = 1000  # cap stored text length


def _get_reader():
    """Lazy-init EasyOCR reader. Returns None if unavailable."""
    global _reader
    if not OCR_AVAILABLE:
        return None
    with _load_lock:
        if _reader is None:
            _reader = _easyocr.Reader(_DEFAULT_LANGS, gpu=_GPU, verbose=False)
        return _reader


def extract_text(pil_image: Image.Image, min_confidence: float = _MIN_CONFIDENCE) -> str:
    """Run OCR on a PIL image. Return concatenated text (space-separated) of detections
    above confidence threshold. Empty string if OCR unavailable or no text found."""
    r = _get_reader()
    if r is None:
        return ""
    try:
        arr = np.array(pil_image.convert("RGB"))
        results = r.readtext(arr, detail=1, paragraph=False)
        # results = [(bbox, text, confidence), ...]
        parts: List[str] = []
        for item in results:
            if len(item) < 3:
                continue
            _, text, conf = item[0], item[1], item[2]
            if conf >= min_confidence and text and text.strip():
                parts.append(text.strip())
        joined = " ".join(parts)
        if len(joined) > _MAX_CHARS_PER_IMAGE:
            joined = joined[:_MAX_CHARS_PER_IMAGE]
        return joined
    except Exception as e:
        print(f"[ocr_detect] extract_text failed: {e}")
        return ""
