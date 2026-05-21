"""YOLOv8 object-detection helper for SovLens.

Lazy-loads the nano model on first use. Falls back to a no-op stub when
ultralytics is not installed (YOLO_AVAILABLE = False).

Python 3.9+.
"""

import threading
from typing import List

from PIL import Image

# ---------------------------------------------------------------------------
# Availability flag
# ---------------------------------------------------------------------------

YOLO_AVAILABLE: bool = False

try:
    from ultralytics import YOLO as _Y  # type: ignore
    YOLO_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Module-level lazy cache
# ---------------------------------------------------------------------------

_yolo_model = None
_yolo_load_lock = threading.Lock()

_DEFAULT_MODEL = "yolov8n.pt"   # Nano — fastest. Will auto-download ~6 MB on first use.
_MIN_CONFIDENCE = 0.25
_MAX_CROPS_PER_FRAME = 8


def _get_model():
    """Lazy-load YOLOv8 nano model. Returns None if YOLO unavailable."""
    global _yolo_model
    if not YOLO_AVAILABLE:
        return None
    with _yolo_load_lock:
        if _yolo_model is None:
            _yolo_model = _Y(_DEFAULT_MODEL)
    return _yolo_model


def detect_and_crop(
    pil_image: Image.Image,
    confidence: float = _MIN_CONFIDENCE,
    max_crops: int = _MAX_CROPS_PER_FRAME,
) -> List[Image.Image]:
    """Run YOLOv8 on a PIL frame and return cropped PIL images of detected objects.

    Returns an empty list if no detections or YOLO is unavailable.

    Args:
        pil_image: Source frame as a PIL RGB image.
        confidence: Minimum detection confidence (0–1).
        max_crops: Hard cap on the number of crops returned per frame.

    Returns:
        List of PIL Image crops, sorted by confidence descending, up to *max_crops*.
    """
    m = _get_model()
    if m is None:
        return []

    # ultralytics accepts PIL directly; verbose=False suppresses per-frame console spam.
    results = m.predict(pil_image, conf=confidence, verbose=False)
    if not results:
        return []

    boxes = results[0].boxes
    if boxes is None or len(boxes) == 0:
        return []

    # Sort detections by confidence descending, take top N
    confs = boxes.conf.cpu().numpy()
    xyxy = boxes.xyxy.cpu().numpy()
    order = confs.argsort()[::-1][:max_crops]

    crops: List[Image.Image] = []
    w, h = pil_image.size
    for i in order:
        x1, y1, x2, y2 = xyxy[i]
        bw = x2 - x1
        bh = y2 - y1
        # Add 5 % padding around each box
        pad = 0.05
        x1 = max(0, int(x1 - bw * pad))
        y1 = max(0, int(y1 - bh * pad))
        x2 = min(w, int(x2 + bw * pad))
        y2 = min(h, int(y2 + bh * pad))
        if x2 > x1 and y2 > y1:
            crops.append(pil_image.crop((x1, y1, x2, y2)))

    return crops
