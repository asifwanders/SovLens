"""YOLOv8 object-detection helper for SovLens.

ONNX Runtime implementation — drops the torch + ultralytics dependency.
Lazy-loads the nano ONNX model on first use. Falls back to a no-op stub
when onnxruntime or numpy is not installed (YOLO_AVAILABLE = False).

Model: `Xenova/yolov8n` from Hugging Face Hub (yolov8n.onnx, ~12 MB).

Python 3.9+.
"""

import os
import threading
from typing import List, Tuple

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

_HF_REPO = "Xenova/yolov8n"
_ONNX_FILENAME = "onnx/model.onnx"   # Path inside the HF repo snapshot.
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

def _resolve_onnx_path() -> str:
    """Download (or reuse cached) YOLOv8n ONNX from Hugging Face Hub."""
    from huggingface_hub import hf_hub_download  # lazy import
    return hf_hub_download(repo_id=_HF_REPO, filename=_ONNX_FILENAME)


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
