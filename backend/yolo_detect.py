"""YOLOv8 object-detection helper for SovLens.

ONNX Runtime implementation — drops the torch + ultralytics dependency.
Lazy-loads the nano ONNX model on first use. Falls back to a no-op stub
when onnxruntime or numpy is not installed (YOLO_AVAILABLE = False).

Model source priority (first match wins):
  1. PyInstaller bundled file at <_MEIPASS>/models/yolov8n.onnx — the
     release path. CI downloads the file before PyInstaller bake; spec
     ships it as a datafile.
  2. Repo-local dev path: backend/models/yolov8n.onnx — for editors
     running the unbundled `python main.py`.
  3. On-demand fetch from
     github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.onnx,
     cached under <app_data_dir>/models/yolov8n.onnx. Used only when both
     bundled paths are absent (e.g. dev box pre-download).

Was previously sourced from HF Hub `Xenova/yolov8n` which started
returning 401 in May 2026, killing YOLO downloads for every install.

Python 3.9+.
"""

import os
import sys
import threading
from typing import List, Optional, Tuple

from PIL import Image

import platform_utils

# ---------------------------------------------------------------------------
# Availability flag
# ---------------------------------------------------------------------------

YOLO_AVAILABLE: bool = False

try:
    import numpy as _np  # type: ignore
    import onnxruntime as _ort  # type: ignore
    YOLO_AVAILABLE = True
except ImportError:
    _np = None  # type: ignore
    _ort = None  # type: ignore

# ---------------------------------------------------------------------------
# Module-level lazy cache
# ---------------------------------------------------------------------------

# `_model` is the cached (InferenceSession, class_names) tuple. main.py
# probes `getattr(yolo_detect, "_model", None)` to detect a warm model.
_model = None
_yolo_load_lock = threading.Lock()
_yolo_infer_lock = threading.Lock()

_YOLO_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.onnx"
_YOLO_FILENAME = "yolov8n.onnx"
_YOLO_MIN_BYTES = 9 * 1024 * 1024  # real file ~12.3 MB
_INPUT_SIZE = 640
_MIN_CONFIDENCE = 0.25
_IOU_THRESHOLD = 0.45
_MAX_CROPS_PER_FRAME = 8

# 80 COCO class names (YOLOv8 default training set).
_COCO_CLASSES: Tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)


# ---------------------------------------------------------------------------
# Model loader
# ---------------------------------------------------------------------------

def _bundled_onnx_paths() -> List[str]:
    """Candidate paths where the ONNX file might live alongside the code."""
    paths: List[str] = []
    # PyInstaller frozen: data files live under sys._MEIPASS at the
    # bundle root. Our spec ships backend/models/yolov8n.onnx -> models/.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(os.path.join(meipass, "models", _YOLO_FILENAME))
    # Repo-local dev mode: backend/models/yolov8n.onnx next to this file.
    here = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(here, "models", _YOLO_FILENAME))
    return paths


def _runtime_cache_path() -> str:
    """Per-user writable fallback cache (used when no bundled file exists)."""
    base = os.path.join(platform_utils.get_app_data_dir(), "models")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, _YOLO_FILENAME)


def _file_ok(path: Optional[str]) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        return os.path.getsize(path) >= _YOLO_MIN_BYTES
    except OSError:
        return False


def _download_yolo(dst: str) -> None:
    """Fetch yolov8n.onnx from the Ultralytics GitHub release into `dst`.

    Streams to a .part file then atomic-renames so a mid-download crash
    or quit can't leave a half-written file that passes the size check.
    """
    import urllib.request
    import urllib.error

    tmp = dst + ".part"
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # 30s connect timeout is plenty for a 12 MB asset.
        with urllib.request.urlopen(_YOLO_URL, timeout=30) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, dst)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # Don't leave a half-written .part lying around; size guard would
        # still catch it but tidy is nicer.
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"yolov8n.onnx download failed: {exc!r}") from exc


def _resolve_onnx_path() -> str:
    """Return a path to yolov8n.onnx, downloading once on demand if missing.

    Priority: PyInstaller bundle > repo-local dev > user cache (download).
    """
    for p in _bundled_onnx_paths():
        if _file_ok(p):
            return p
    cached = _runtime_cache_path()
    if _file_ok(cached):
        return cached
    print(f"[yolo] fetching {_YOLO_URL} -> {cached}", flush=True)
    _download_yolo(cached)
    if not _file_ok(cached):
        raise RuntimeError(f"yolov8n.onnx at {cached} is too small after download")
    return cached


def _get_model():
    """Lazy-load YOLOv8n ONNX session. Returns (session, class_names) or None."""
    global _model
    if not YOLO_AVAILABLE:
        return None
    with _yolo_load_lock:
        if _model is None:
            onnx_path = _resolve_onnx_path()
            providers = platform_utils.get_onnx_providers()
            so = _ort.SessionOptions()
            so.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
            session = _ort.InferenceSession(
                onnx_path, sess_options=so, providers=providers
            )
            _model = (session, _COCO_CLASSES)
    return _model


# ---------------------------------------------------------------------------
# Pre/post processing
# ---------------------------------------------------------------------------

def _letterbox(img_arr, new_size: int = _INPUT_SIZE):
    """Resize + pad image to (new_size, new_size) preserving aspect ratio.

    Returns (padded_array, scale, pad_x, pad_y) where padded_array is uint8
    HWC RGB, scale is the resize ratio applied, and pad_x/pad_y are the
    left/top padding amounts in the letterboxed image.
    """
    h, w = img_arr.shape[:2]
    scale = min(new_size / w, new_size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))

    # Resize via PIL to avoid an opencv dependency.
    pil_resized = Image.fromarray(img_arr).resize((nw, nh), Image.BILINEAR)
    resized = _np.asarray(pil_resized)

    canvas = _np.full((new_size, new_size, 3), 114, dtype=_np.uint8)
    pad_x = (new_size - nw) // 2
    pad_y = (new_size - nh) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return canvas, scale, pad_x, pad_y


def _nms(boxes, scores, iou_threshold: float):
    """Pure-numpy non-max suppression. boxes are xyxy, returns kept indices."""
    if len(boxes) == 0:
        return []
    x1 = boxes[:, 0]; y1 = boxes[:, 1]; x2 = boxes[:, 2]; y2 = boxes[:, 3]
    areas = (x2 - x1).clip(min=0) * (y2 - y1).clip(min=0)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        xx1 = _np.maximum(x1[i], x1[order[1:]])
        yy1 = _np.maximum(y1[i], y1[order[1:]])
        xx2 = _np.minimum(x2[i], x2[order[1:]])
        yy2 = _np.minimum(y2[i], y2[order[1:]])
        inter = (xx2 - xx1).clip(min=0) * (yy2 - yy1).clip(min=0)
        union = areas[i] + areas[order[1:]] - inter
        iou = _np.where(union > 0, inter / union, 0.0)
        order = order[1:][iou <= iou_threshold]
    return keep


def _decode(output, scale: float, pad_x: int, pad_y: int,
            conf_threshold: float, iou_threshold: float):
    """Decode YOLOv8 raw output -> list of (x1,y1,x2,y2,score,class_id) in original image coords."""
    # output shape: (1, 84, 8400) typical -> transpose to (8400, 84)
    pred = output[0]
    if pred.shape[0] in (84, 85) and pred.shape[1] > pred.shape[0]:
        pred = pred.T  # (N, 84)

    boxes_xywh = pred[:, :4]
    cls_scores = pred[:, 4:]
    class_ids = cls_scores.argmax(axis=1)
    scores = cls_scores.max(axis=1)

    mask = scores >= conf_threshold
    if not mask.any():
        return []
    boxes_xywh = boxes_xywh[mask]
    scores = scores[mask]
    class_ids = class_ids[mask]

    # xywh (center) -> xyxy in 640-letterboxed space
    cx = boxes_xywh[:, 0]; cy = boxes_xywh[:, 1]
    bw = boxes_xywh[:, 2]; bh = boxes_xywh[:, 3]
    x1 = cx - bw / 2; y1 = cy - bh / 2
    x2 = cx + bw / 2; y2 = cy + bh / 2

    # Undo letterbox: subtract pad then divide by scale -> original image coords.
    x1 = (x1 - pad_x) / scale
    y1 = (y1 - pad_y) / scale
    x2 = (x2 - pad_x) / scale
    y2 = (y2 - pad_y) / scale

    boxes_xyxy = _np.stack([x1, y1, x2, y2], axis=1)
    keep = _nms(boxes_xyxy, scores, iou_threshold)
    return [
        (float(boxes_xyxy[i, 0]), float(boxes_xyxy[i, 1]),
         float(boxes_xyxy[i, 2]), float(boxes_xyxy[i, 3]),
         float(scores[i]), int(class_ids[i]))
        for i in keep
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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
    session, _classes = m

    if pil_image.mode != "RGB":
        pil_image = pil_image.convert("RGB")

    img_arr = _np.asarray(pil_image)
    canvas, scale, pad_x, pad_y = _letterbox(img_arr, _INPUT_SIZE)

    # HWC uint8 -> NCHW float32 normalized to [0,1]
    inp = canvas.astype(_np.float32) / 255.0
    inp = _np.transpose(inp, (2, 0, 1))[None, ...]  # (1,3,640,640)

    input_name = session.get_inputs()[0].name
    with _yolo_infer_lock:
        outputs = session.run(None, {input_name: inp})

    detections = _decode(
        outputs[0], scale, pad_x, pad_y, confidence, _IOU_THRESHOLD
    )
    if not detections:
        return []

    # Sort by score desc, take top N.
    detections.sort(key=lambda d: d[4], reverse=True)
    detections = detections[:max_crops]

    crops: List[Image.Image] = []
    w, h = pil_image.size
    for x1, y1, x2, y2, _score, _cid in detections:
        bw = x2 - x1
        bh = y2 - y1
        pad = 0.05
        cx1 = max(0, int(x1 - bw * pad))
        cy1 = max(0, int(y1 - bh * pad))
        cx2 = min(w, int(x2 + bw * pad))
        cy2 = min(h, int(y2 + bh * pad))
        if cx2 > cx1 and cy2 > cy1:
            crops.append(pil_image.crop((cx1, cy1, cx2, cy2)))

    return crops
