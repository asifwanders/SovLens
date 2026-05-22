"""RapidOCR (ONNX) text extraction from images/frames.

Lazy-loaded — model only loads on first call.
Returns concatenated extracted text per image; empty string if no text or OCR unavailable.

Migrated from EasyOCR (torch) to rapidocr-onnxruntime to drop the torch dep
from the OCR path. Ships PaddleOCR det+rec+cls as ONNX (~10 MB total).
"""
import threading
from typing import List
from PIL import Image
import numpy as np
import platform_utils

_reader = None
_load_lock = threading.Lock()
OCR_AVAILABLE = False

try:
    from rapidocr_onnxruntime import RapidOCR as _RapidOCR  # type: ignore
    OCR_AVAILABLE = True
except ImportError:
    _RapidOCR = None  # type: ignore

_MIN_CONFIDENCE = 0.4
_MAX_CHARS_PER_IMAGE = 1000  # cap stored text length


def _want_cuda() -> bool:
    """True if onnxruntime CUDA EP is available on this host."""
    try:
        return "CUDAExecutionProvider" in platform_utils.get_onnx_providers()
    except Exception:
        return False


def _get_reader():
    """Lazy-init RapidOCR engine. Returns None if unavailable."""
    global _reader
    if not OCR_AVAILABLE or _RapidOCR is None:
        return None
    with _load_lock:
        if _reader is None:
            cuda = _want_cuda()
            try:
                if cuda:
                    _reader = _RapidOCR(det_use_cuda=True, rec_use_cuda=True, cls_use_cuda=True)
                else:
                    _reader = _RapidOCR()
            except TypeError:
                # Older/newer rapidocr versions may not accept cuda kwargs — fall back.
                _reader = _RapidOCR()
        return _reader


# Back-compat alias: main.py calls `ocr_detect._get_reader()` to warm the model.
_get_ocr_engine = _get_reader


def extract_text(pil_image: Image.Image, min_confidence: float = _MIN_CONFIDENCE) -> str:
    """Run OCR on a PIL image. Return concatenated text (space-separated) of detections
    above confidence threshold. Empty string if OCR unavailable or no text found."""
    engine = _get_reader()
    if engine is None:
        return ""
    try:
        # RapidOCR expects BGR numpy array (OpenCV convention).
        rgb = np.array(pil_image.convert("RGB"))
        bgr = rgb[:, :, ::-1]
        result, _elapsed = engine(bgr)
        # result: List[[box, text, score]] or None when no text detected.
        parts: List[str] = []
        if result:
            for item in result:
                if not item or len(item) < 3:
                    continue
                _box, text, score = item[0], item[1], item[2]
                try:
                    conf = float(score)
                except (TypeError, ValueError):
                    continue
                if conf >= min_confidence and text and str(text).strip():
                    parts.append(str(text).strip())
        joined = " ".join(parts)
        if len(joined) > _MAX_CHARS_PER_IMAGE:
            joined = joined[:_MAX_CHARS_PER_IMAGE]
        return joined
    except Exception as e:
        print(f"[ocr_detect] extract_text failed: {e}")
        return ""
