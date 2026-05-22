import os
import json
import threading
import lancedb
import pyarrow as pa
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image
from scipy.fft import dct as _dct
import platform_utils
import config

# ONNX Runtime replaces the torch + sentence-transformers stack for CLIP.
# `device` is kept as a legacy module-level for callers / status endpoints
# but now holds the active ONNX EP short-name ("coreml" / "cuda" / "dml" / "cpu")
# rather than a torch device string. It is populated on first session init.
device: str = "cpu"

# ---------------------------------------------------------------------------
# Lazy CLIP model management
# ---------------------------------------------------------------------------
# NOTE: Changing the analysis level may select a different model.
# Existing DB rows are encoded with the model that was active at ingest time.
# Swapping models makes those vectors incompatible with new queries.
# A full re-index is required after a model change (handled by UI flow WS-S6).

# Only models that produce 768-dimensional vectors are accepted because the
# LanceDB schema fixes the vector column to list_(float32, 768).
# ViT-B-32 outputs 512d and is REJECTED — see _resolve_model_name().
_SUPPORTED_768D = {"clip-ViT-L-14"}
# The single model that has ever been used to populate the DB. All historical
# rows are assumed to have been encoded with this model when backfilling the
# `model_name` column during migration.
LEGACY_MODEL_NAME = "clip-ViT-L-14"

# ONNX session state. The "model" is now a pair of ORT InferenceSessions
# (vision + text) plus a tokenizer + preprocessing constants. The public
# accessor get_model() is removed; callers used it only inside encode_*
# helpers which now go through _ensure_sessions() directly.
_vision_session: Any = None  # onnxruntime.InferenceSession
_text_session: Any = None    # onnxruntime.InferenceSession
_tokenizer: Any = None       # tokenizers.Tokenizer
_model_name: Optional[str] = None
_model_lock = threading.Lock()

# ---------------------------------------------------------------------------
# CLIP-ViT-L/14 preprocessing constants (image side).
# These match openai/clip and sentence-transformers/clip-ViT-L-14 exactly so
# vectors remain comparable with rows previously encoded by the torch model.
# ---------------------------------------------------------------------------
_CLIP_IMAGE_SIZE = 224
_CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
_CLIP_CONTEXT_LENGTH = 77

# Hugging Face repo that ships ONNX exports of CLIP-ViT-L/14 (text+vision)
# plus tokenizer.json and preprocessor_config.json. Xenova's mirror is the
# canonical source for transformers.js-compatible ONNX bundles.
_HF_ONNX_REPO_ID = "Xenova/clip-vit-large-patch14"
# Subdirectory inside the repo that holds the ONNX files. Xenova ships both
# fp32 (~600MB) and quantized (~150MB) variants; we prefer fp16 if available,
# then fp32. Quantized int8 hurts retrieval recall and is skipped.
_ONNX_SUBDIR = "onnx"


def _resolve_model_name(requested: str) -> str:
    """Map a config model name to a supported 768d model.

    Returns (effective_name, did_fall_back).

    The current LanceDB schema fixes the vector column at 768d, so only
    `clip-ViT-L-14` is honoured. A request for any other model (e.g.
    `clip-ViT-B-32` which is 512d) falls back to L-14 and the caller can
    surface that fact to the UI. The fallback is NO LONGER silent: the
    `/status` and `/config` endpoints expose `model_fallback` so the
    Settings UI can show an honest banner.
    """
    if requested in _SUPPORTED_768D:
        return requested
    print(
        f"WARN: model '{requested}' not 768d-compatible with current schema; "
        f"using {LEGACY_MODEL_NAME} instead. Re-index required if model is changed."
    )
    return LEGACY_MODEL_NAME


def _ep_short_name(provider: str) -> str:
    """Map an ONNX Runtime provider id to a short label for `device`."""
    return {
        "CoreMLExecutionProvider": "coreml",
        "CUDAExecutionProvider": "cuda",
        "DmlExecutionProvider": "dml",
        "CPUExecutionProvider": "cpu",
    }.get(provider, provider.replace("ExecutionProvider", "").lower())


def _pick_onnx_files(onnx_dir: str) -> Tuple[str, str]:
    """Return absolute paths to (vision_model, text_model) ONNX files.

    Prefers fp16 (smaller, faster on CoreML/CUDA) when shipped, else fp32.
    Skips int8/quantized variants — they noticeably hurt retrieval recall.
    Raises FileNotFoundError with a descriptive message if neither is found.
    """
    candidates = [
        ("vision_model_fp16.onnx", "text_model_fp16.onnx"),
        ("vision_model.onnx", "text_model.onnx"),
    ]
    for vname, tname in candidates:
        vp = os.path.join(onnx_dir, vname)
        tp = os.path.join(onnx_dir, tname)
        if os.path.isfile(vp) and os.path.isfile(tp):
            return vp, tp
    raise FileNotFoundError(
        f"No CLIP ONNX files found in {onnx_dir}. "
        f"Expected one of: {candidates}"
    )


def _ensure_sessions() -> Tuple[Any, Any, Any]:
    """Lazy-load and cache ONNX vision+text sessions plus the tokenizer.

    Thread-safe: only one thread runs session init; others wait at the lock.
    Returns (vision_session, text_session, tokenizer).
    """
    global _vision_session, _text_session, _tokenizer, _model_name, device

    if _vision_session is not None and _text_session is not None and _tokenizer is not None:
        return _vision_session, _text_session, _tokenizer

    with _model_lock:
        if _vision_session is not None and _text_session is not None and _tokenizer is not None:
            return _vision_session, _text_session, _tokenizer

        # Deferred imports — avoids hard dep at module import time and keeps
        # core.py importable in environments that only need DB helpers.
        import onnxruntime as ort  # type: ignore
        from huggingface_hub import snapshot_download  # type: ignore
        from tokenizers import Tokenizer  # type: ignore

        cfg = config.get_current_level_params()
        wanted = _resolve_model_name(cfg.get("model", LEGACY_MODEL_NAME))

        providers = platform_utils.get_onnx_providers()
        print(f"[core] Loading CLIP ONNX ({wanted}) with providers={providers}")

        # Pull the ONNX bundle from HF Hub on first use; cached for subsequent
        # launches in ~/.cache/huggingface.
        local_dir = snapshot_download(
            repo_id=_HF_ONNX_REPO_ID,
            allow_patterns=[
                "onnx/vision_model*.onnx",
                "onnx/text_model*.onnx",
                "tokenizer.json",
                "tokenizer_config.json",
                "preprocessor_config.json",
                "special_tokens_map.json",
                "vocab.json",
                "merges.txt",
            ],
        )

        onnx_dir = os.path.join(local_dir, _ONNX_SUBDIR)
        vision_path, text_path = _pick_onnx_files(onnx_dir)

        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        vs = ort.InferenceSession(vision_path, sess_options=sess_opts, providers=providers)
        ts = ort.InferenceSession(text_path, sess_options=sess_opts, providers=providers)

        # Reflect the EP actually selected (ORT silently downgrades if e.g.
        # CUDA libs are missing).
        active = vs.get_providers()[0] if vs.get_providers() else "CPUExecutionProvider"
        device = _ep_short_name(active)
        print(f"[core] CLIP ONNX active EP: {active} ({device})")

        tok_path = os.path.join(local_dir, "tokenizer.json")
        tok = Tokenizer.from_file(tok_path)
        # CLIP's HF tokenizer ships with no built-in padding/truncation rules;
        # apply them explicitly so input_ids is always 1x77 int64.
        tok.enable_truncation(max_length=_CLIP_CONTEXT_LENGTH)
        tok.enable_padding(length=_CLIP_CONTEXT_LENGTH, pad_id=0, pad_token="<|endoftext|>")

        _vision_session = vs
        _text_session = ts
        _tokenizer = tok
        _model_name = wanted
        return vs, ts, tok


def get_model_name() -> str:
    """Return the name of the currently-loaded model, or 'unloaded'."""
    return _model_name or "unloaded"


def resolve_model_name(requested: str) -> str:
    """Public wrapper around _resolve_model_name for use in status endpoints."""
    return _resolve_model_name(requested)


def reset_model() -> None:
    """Drop cached ONNX sessions so the next encode call reloads.

    Called by /config/level after a level change so the new model (if any) is
    picked up lazily on the next encode request.
    """
    global _vision_session, _text_session, _tokenizer, _model_name
    with _model_lock:
        _vision_session = None
        _text_session = None
        _tokenizer = None
        _model_name = None

# ---------------------------------------------------------------------------
# LanceDB storage location
# ---------------------------------------------------------------------------
# The DB MUST live under the per-user app data directory, NOT inside the
# package/source tree. Storing it next to backend/*.py breaks packaged installs
# (read-only Resources dir on mac, app translocation, multi-user installs,
# upgrades that wipe the bundle). The old location was
# `<backend>/lancedb_data`; we now use `<app_data>/lancedb` and one-shot
# migrate the legacy directory if it exists.
_LEGACY_DB_PATH = os.path.join(os.path.dirname(__file__), "lancedb_data")
DB_PATH = os.path.join(platform_utils.get_app_data_dir(), "lancedb")


def _migrate_legacy_db_dir() -> None:
    """One-shot move of <backend>/lancedb_data -> <app_data>/lancedb.

    Paranoid: only runs when the legacy dir exists AND the new dir does not
    (or is empty). Uses copy + leaves the old dir in place so a botched move
    can't lose data. Failure is logged but never fatal.
    """
    try:
        if not os.path.isdir(_LEGACY_DB_PATH):
            return
        # Skip if new location already has any content (already-migrated user).
        if os.path.isdir(DB_PATH) and os.listdir(DB_PATH):
            return
        os.makedirs(DB_PATH, exist_ok=True)
        import shutil
        for name in os.listdir(_LEGACY_DB_PATH):
            src = os.path.join(_LEGACY_DB_PATH, name)
            dst = os.path.join(DB_PATH, name)
            if os.path.exists(dst):
                continue
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        print(f"[core] Migrated legacy LanceDB dir: {_LEGACY_DB_PATH} -> {DB_PATH} (old dir left intact).")
    except Exception as e:
        print(f"[core] Legacy LanceDB migration failed (continuing): {e}")


os.makedirs(DB_PATH, exist_ok=True)
_migrate_legacy_db_dir()
db = lancedb.connect(DB_PATH)

# Define schema for our media table
# Ensure vectors match the model output size (768 for clip-ViT-L-14)
schema = pa.schema([
    pa.field("vector", pa.list_(pa.float32(), 768)),
    pa.field("id", pa.string()),
    pa.field("path", pa.string()),
    pa.field("thumbnail", pa.string()),      # path to the generated thumbnail
    pa.field("type", pa.string()),            # "image", "video", or "audio_segment"
    pa.field("timestamp", pa.float64()),      # timestamp for video frames, 0.0 for images
    pa.field("is_primary", pa.bool_()),       # Only true for first frame of a video, or images
    pa.field("video_id", pa.string()),        # UUID shared across all frames of the same video; "" for images
    pa.field("text_snippet", pa.string()),    # transcript text for audio_segment rows; "" for others
    pa.field("model_name", pa.string()),      # CLIP model used to encode this vector (e.g. "clip-ViT-L-14")
])

TABLE_NAME = "media"

# ---------------------------------------------------------------------------
# Schema migration helpers
# ---------------------------------------------------------------------------

def _open_or_create_table():
    """Open existing table or create fresh; run schema migrations as needed.

    Migrations are applied in version order (v2 → v3) so that each migration
    can assume the previous version's columns are already present.
    """
    global table  # declared once; used by both migration branches below

    if TABLE_NAME not in db.table_names():
        return db.create_table(TABLE_NAME, schema=schema)

    tbl = db.open_table(TABLE_NAME)
    existing_fields = {f.name for f in tbl.schema}

    # v2: add video_id (must run before v3 because v3 reads all rows)
    if "video_id" not in existing_fields:
        print("WARNING: 'video_id' column missing — auto-migrating schema (v2).")
        table = tbl
        migrate_schema()
        tbl = table
        existing_fields = {f.name for f in tbl.schema}

    # v3: add text_snippet
    if "text_snippet" not in existing_fields:
        print("WARNING: 'text_snippet' column missing — auto-migrating schema (v3).")
        table = tbl
        migrate_schema_v3()
        tbl = table
        existing_fields = {f.name for f in tbl.schema}

    # v4: add model_name (backfilled to LEGACY_MODEL_NAME for all old rows;
    # those rows were only ever encoded with clip-ViT-L-14).
    if "model_name" not in existing_fields:
        print("WARNING: 'model_name' column missing — auto-migrating schema (v4).")
        table = tbl
        migrate_schema_v4()
        tbl = table

    return tbl


def migrate_schema() -> None:
    """v2 migration: backfill empty video_id into every row that predates the column."""
    global table
    existing_fields = {f.name for f in table.schema}
    if "video_id" in existing_fields:
        print("Schema v2 already up-to-date; no migration needed.")
        return

    # LanceDB does not support ALTER TABLE — safest path is recreate with defaults.
    # Use a v2-only intermediate schema so we don't require text_snippet yet.
    schema_v2 = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), 768)),
        pa.field("id", pa.string()),
        pa.field("path", pa.string()),
        pa.field("thumbnail", pa.string()),
        pa.field("type", pa.string()),
        pa.field("timestamp", pa.float64()),
        pa.field("is_primary", pa.bool_()),
        pa.field("video_id", pa.string()),
    ])
    # WARN: loads entire table into RAM; fine for <1M rows on local hardware
    rows = table.search().limit(10_000_000).to_list()
    for r in rows:
        r.setdefault("video_id", "")
        # Drop text_snippet if it somehow exists to keep schema consistent
        r.pop("text_snippet", None)

    db.drop_table(TABLE_NAME)
    table = db.create_table(TABLE_NAME, schema=schema_v2)
    if rows:
        table.add(rows)
    print(f"Migration v2 complete: {len(rows)} rows backfilled with empty video_id.")


def migrate_schema_v3() -> None:
    """v3 migration: add text_snippet column; backfills "" for all existing rows."""
    global table
    existing_fields = {f.name for f in table.schema}
    if "text_snippet" in existing_fields:
        print("Schema v3 already up-to-date; no migration needed.")
        return

    # Use a v3-only intermediate schema so model_name (added in v4) is not
    # required at this stage — v4 migration will add it next.
    schema_v3 = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), 768)),
        pa.field("id", pa.string()),
        pa.field("path", pa.string()),
        pa.field("thumbnail", pa.string()),
        pa.field("type", pa.string()),
        pa.field("timestamp", pa.float64()),
        pa.field("is_primary", pa.bool_()),
        pa.field("video_id", pa.string()),
        pa.field("text_snippet", pa.string()),
    ])
    # WARN: loads entire table into RAM; fine for <1M rows on local hardware
    rows = table.search().limit(10_000_000).to_list()
    for r in rows:
        r.setdefault("text_snippet", "")
        r.pop("model_name", None)

    db.drop_table(TABLE_NAME)
    table = db.create_table(TABLE_NAME, schema=schema_v3)
    if rows:
        table.add(rows)
    print(f"Migration v3 complete: {len(rows)} rows backfilled with empty text_snippet.")


def migrate_schema_v4() -> None:
    """v4 migration: add `model_name` column to record which CLIP model encoded each row.

    Backfills LEGACY_MODEL_NAME ("clip-ViT-L-14") for every existing row — the
    only model that was ever used to populate the DB before this column existed.

    Safety: drop-and-recreate (LanceDB 0.14 lacks add-column DDL). Rows are
    loaded into RAM, then the table is dropped and recreated under the new
    schema. If the user has < ~1M rows (~few GB) this is fine on local
    hardware. The reindex_all transactional helpers (backup_and_reset_table /
    restore_from_backup) are NOT used here because this is an
    open-time migration, not a user-initiated reindex — failing partway
    through would leave the DB in a known state (legacy dir still present
    via _migrate_legacy_db_dir, since we never delete it).
    """
    global table
    existing_fields = {f.name for f in table.schema}
    if "model_name" in existing_fields:
        print("Schema v4 already up-to-date; no migration needed.")
        return

    # WARN: loads entire table into RAM; fine for <1M rows on local hardware
    rows = table.search().limit(10_000_000).to_list()
    for r in rows:
        r.setdefault("model_name", LEGACY_MODEL_NAME)

    db.drop_table(TABLE_NAME)
    table = db.create_table(TABLE_NAME, schema=schema)
    if rows:
        table.add(rows)
    print(
        f"Migration v4 complete: {len(rows)} rows backfilled with "
        f"model_name={LEGACY_MODEL_NAME!r}."
    )


table = _open_or_create_table()


# ---------------------------------------------------------------------------
# pHash — scratch implementation (numpy + PIL, no extra deps)
# ---------------------------------------------------------------------------

# Hamming distance threshold below which two frames are considered duplicates
PHASH_SIZE = 8  # produces a 64-bit hash (8×8 DCT coefficients)


def compute_phash(image: Image.Image) -> int:
    """Return a 64-bit perceptual hash for duplicate-frame detection."""
    # Resize to 32×32 first so DCT captures coarse structure, not pixel noise
    gray = image.convert("L").resize((PHASH_SIZE * 4, PHASH_SIZE * 4), Image.LANCZOS)
    pixels = np.array(gray, dtype=np.float32)

    # 2-D DCT via separable 1-D DCTs; scipy is already a transitive dep
    dct_rows = _dct(pixels, norm="ortho", axis=1)
    dct_2d = _dct(dct_rows, norm="ortho", axis=0)

    # Keep only the top-left 8×8 low-frequency block
    low_freq = dct_2d[:PHASH_SIZE, :PHASH_SIZE]
    median = np.median(low_freq)

    # Build 64-bit integer from bit positions where value > median
    # Use Python int arithmetic — numpy uint64 left_shift raises on some builds
    bits = (low_freq.flatten() > median).tolist()
    hash_val: int = sum(int(b) << i for i, b in enumerate(bits))
    return hash_val


def phash_hamming(a: int, b: int) -> int:
    """Count differing bits between two pHash integers (Hamming distance)."""
    xor = a ^ b
    count = 0
    while xor:
        count += xor & 1
        xor >>= 1
    return count


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

_MAX_ENCODE_CHUNK = 256  # OOM guard: split large batches into chunks of this size


def _preprocess_image(img: Image.Image) -> np.ndarray:
    """CLIP-ViT-L/14 image preprocessing: RGB, center-crop-resize 224, normalize, CHW.

    Matches openai/clip + sentence-transformers/clip-ViT-L-14 exactly so
    embeddings stay comparable with previously-indexed rows.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")
    # Bicubic resize matching transformers' CLIPImageProcessor default.
    img = img.resize((_CLIP_IMAGE_SIZE, _CLIP_IMAGE_SIZE), Image.BICUBIC)
    arr = np.asarray(img, dtype=np.float32) / 255.0  # HWC
    arr = (arr - _CLIP_MEAN) / _CLIP_STD
    # HWC -> CHW
    return np.transpose(arr, (2, 0, 1))


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation; supports 1-D or 2-D arrays."""
    if v.ndim == 1:
        n = float(np.linalg.norm(v))
        return v if n == 0.0 else (v / n).astype(np.float32, copy=False)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return (v / norms).astype(np.float32, copy=False)


def _run_vision(batch: np.ndarray) -> np.ndarray:
    """Run the vision ONNX session on an Nx3x224x224 float32 batch.

    Returns Nx768 float32 (unnormalized) embeddings.
    """
    vs, _ts, _tok = _ensure_sessions()
    # The vision model expects a single input named "pixel_values" in the
    # Xenova export. Probe in case a future export renames it.
    input_name = vs.get_inputs()[0].name
    # fp16 ONNX models still accept float32 inputs (ORT casts internally).
    out = vs.run(None, {input_name: batch.astype(np.float32, copy=False)})[0]
    return np.asarray(out, dtype=np.float32)


def encode_images_batch(images: List[Image.Image], batch_size: int = 64) -> List[List[float]]:
    """Encode a list of PIL images; returns list of 768-D float vectors.

    Output is L2-normalized so cosine similarity matches the old
    sentence-transformers behaviour (which also returned unit vectors after
    its own normalize step, matching what callers / LanceDB cosine expects).

    Internally chunks inputs into at most _MAX_ENCODE_CHUNK images to avoid OOM.
    """
    if not images:
        return []
    if len(images) > _MAX_ENCODE_CHUNK:
        result: List[List[float]] = []
        for i in range(0, len(images), _MAX_ENCODE_CHUNK):
            result.extend(encode_images_batch(images[i:i + _MAX_ENCODE_CHUNK], batch_size=batch_size))
        return result

    all_vecs: List[np.ndarray] = []
    for i in range(0, len(images), batch_size):
        chunk = images[i:i + batch_size]
        batch = np.stack([_preprocess_image(im) for im in chunk], axis=0)
        vecs = _run_vision(batch)
        all_vecs.append(vecs)
    out = np.concatenate(all_vecs, axis=0) if len(all_vecs) > 1 else all_vecs[0]
    out = _l2_normalize(out)
    return [row.tolist() for row in out]


def encode_image(image: Image.Image) -> List[float]:
    """Encode a single PIL Image; thin wrapper around encode_images_batch."""
    return encode_images_batch([image])[0]


def _tokenize(texts: List[str]) -> np.ndarray:
    """Tokenize a list of strings into a 1xN int64 input_ids array (padded to 77)."""
    _vs, _ts, tok = _ensure_sessions()
    encs = tok.encode_batch(texts)
    ids = np.array([e.ids for e in encs], dtype=np.int64)
    return ids


def _run_text(input_ids: np.ndarray) -> np.ndarray:
    """Run the text ONNX session; returns Nx768 float32 (unnormalized).

    Xenova's text_model export takes input_ids + attention_mask. We derive
    attention_mask from non-pad positions (pad_id=0 matches the tokenizer
    config we set in _ensure_sessions).
    """
    _vs, ts, _tok = _ensure_sessions()
    attention_mask = (input_ids != 0).astype(np.int64)
    feed: Dict[str, np.ndarray] = {}
    for inp in ts.get_inputs():
        if inp.name == "input_ids":
            feed[inp.name] = input_ids
        elif inp.name == "attention_mask":
            feed[inp.name] = attention_mask
    out = ts.run(None, feed)[0]
    return np.asarray(out, dtype=np.float32)


def encode_text(text: str) -> List[float]:
    """Encode a single text query to a 768-D L2-normalized vector."""
    ids = _tokenize([text])
    vec = _run_text(ids)[0]
    return _l2_normalize(vec).tolist()


def encode_texts_batch(texts: List[str]) -> List[List[float]]:
    """Encode a list of text strings; returns list of 768-D L2-normalized vectors."""
    if not texts:
        return []
    out_chunks: List[np.ndarray] = []
    batch_size = 64
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        ids = _tokenize(chunk)
        out_chunks.append(_run_text(ids))
    arr = np.concatenate(out_chunks, axis=0) if len(out_chunks) > 1 else out_chunks[0]
    arr = _l2_normalize(arr)
    return [row.tolist() for row in arr]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_effective_model_name() -> str:
    """Return the model name that core.get_model() WILL load for the active config.

    Distinct from get_model_name() which returns the model currently in RAM
    (may be 'unloaded' until first encode call). Used to stamp `model_name`
    on freshly added rows and as the search-time filter so vectors encoded
    by a different model are never mixed in.
    """
    cfg = config.get_current_level_params()
    return _resolve_model_name(cfg.get("model", LEGACY_MODEL_NAME))


def add_media(records: List[Dict[str, Any]]) -> None:
    """Insert a batch of media records into LanceDB."""
    # Ensure schema columns always present so inserts never fail on missing keys
    effective = get_effective_model_name()
    for r in records:
        r.setdefault("video_id", "")
        r.setdefault("text_snippet", "")
        # Always stamp the model that produced the vector. Callers may pass an
        # explicit model_name (e.g. if they encoded with a specific model
        # outside the cached global), otherwise default to current effective.
        r.setdefault("model_name", effective)
    table.add(records)


def cleanup_missing_media() -> None:
    """Remove DB rows whose source files no longer exist on disk."""
    try:
        results = table.search().limit(1_000_000).to_list()
        to_delete = [r["id"] for r in results if r.get("path") and not os.path.exists(r["path"])]

        for i in range(0, len(to_delete), 100):
            chunk = to_delete[i:i + 100]
            ids_str = ", ".join(f"'{x}'" for x in chunk)
            table.delete(f"id IN ({ids_str})")
        if to_delete:
            print(f"Cleaned up {len(to_delete)} missing media records.")
    except Exception as e:
        print("Cleanup error:", e)


def get_existing_paths() -> set:
    """Return the set of normalized file paths already indexed (used for whole-file resume)."""
    try:
        results = table.search().limit(1_000_000).select(["path"]).to_list()
        return {platform_utils.normalize_path(r["path"]) for r in results if "path" in r}
    except Exception:
        return set()


def delete_rows_by_video_id(video_id: str) -> None:
    """Remove all DB rows belonging to a specific video_id (used for crash recovery)."""
    try:
        table.delete(f"video_id = '{video_id}'")
    except Exception as e:
        print(f"Warning: could not delete rows for video_id {video_id}: {e}")


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------

# Minimum cosine similarity score to include a result.
# Overridable via SEARCH_MIN_SCORE env var. Cosine distance < (1 - min_score).
SEARCH_MIN_SCORE: float = float(os.environ.get("SEARCH_MIN_SCORE", "0.20"))


def distance_to_score(distance: float) -> float:
    """Convert LanceDB cosine distance to similarity score in [0, 1].

    LanceDB returns cosine *distance* (1 - cosine_similarity) so we invert.
    """
    return max(0.0, min(1.0, 1.0 - distance))


def expand_query(text: str) -> List[str]:
    """Return a list of query variants for the given text.

    Currently a pass-through stub returning the original query.
    TODO: replace with template-based expansion (e.g. "a photo of {text}",
    "screenshot of {text}") once we have benchmark data to validate recall lift.
    """
    return [text]


def _group_by_video(
    rows: List[Dict[str, Any]], limit: int
) -> List[Dict[str, Any]]:
    """Keep the best-scoring frame per video_id; images (video_id=='') pass through individually.

    Rows must already contain a 'score' key (float, higher = better).
    Returns at most `limit` entries, sorted by score descending.
    """
    best: Dict[str, Dict[str, Any]] = {}  # video_id -> best row (videos only)
    images: List[Dict[str, Any]] = []

    for row in rows:
        vid = row.get("video_id", "")
        if not vid:
            # Individual image — include as-is
            images.append(row)
        else:
            prev = best.get(vid)
            if prev is None or row["score"] > prev["score"]:
                best[vid] = row

    combined = images + list(best.values())
    combined.sort(key=lambda r: r["score"], reverse=True)
    return combined[:limit]


_TEXT_BOOST = 0.15  # score boost applied when query text appears in text_snippet


def search_media(
    query_vector: List[float],
    limit: int = 20,
    min_score: Optional[float] = None,
    query_text: Optional[str] = None,
    text_boost: bool = True,
) -> List[Dict[str, Any]]:
    """Search for similar media; returns best-scoring frame per video + individual images.

    Fetches up to min(limit*5, 200) candidates internally so that every video
    has at least one candidate before collapsing to top-K per video_id.

    Args:
        query_vector: Encoded query embedding.
        limit: Maximum number of results to return.
        min_score: Minimum cosine similarity (0–1). Defaults to SEARCH_MIN_SCORE.
        query_text: Raw query string used for substring boost against text_snippet.
        text_boost: If True and query_text is provided, boost rows whose text_snippet
                    contains the query string (case-insensitive) by +0.15, capped at 1.0.

    Returns:
        List of result dicts with score, timestamp, video_id, and media metadata.
        The 'vector' field is stripped to keep responses lightweight.
    """
    if min_score is None:
        min_score = SEARCH_MIN_SCORE

    # Over-fetch so each video has candidate frames before collapsing
    fetch_limit = min(limit * 5, 200)

    # Filter to rows encoded with the CURRENT effective model. Mixing vectors
    # from different models (different vector spaces, possibly different
    # dimensions in the future) is a data-corruption footgun for relevance.
    # Legacy rows were backfilled to LEGACY_MODEL_NAME during migration v4.
    effective_model = get_effective_model_name()
    safe_model = effective_model.replace("'", "''")
    raw = (
        table.search(query_vector)
        .metric("cosine")
        .where(f"model_name = '{safe_model}'")
        .limit(fetch_limit)
        .to_list()
    )

    query_lower = query_text.lower() if (text_boost and query_text) else None

    enriched = []
    for row in raw:
        score = distance_to_score(row.get("_distance", 1.0))
        if score < min_score:
            continue
        out = {k: v for k, v in row.items() if k not in ("vector", "_distance")}
        # Apply text boost if the raw query appears in the stored text_snippet
        if query_lower:
            snippet = (out.get("text_snippet") or "").lower()
            if snippet and query_lower in snippet:
                score = min(1.0, score + _TEXT_BOOST)
        out["score"] = round(score, 4)
        # Ensure video_id and timestamp keys are always present
        out.setdefault("video_id", "")
        out.setdefault("timestamp", 0.0)
        enriched.append(out)

    # Re-sort after potential boost changes
    enriched.sort(key=lambda r: r["score"], reverse=True)

    return _group_by_video(enriched, limit)


def purge_all() -> int:
    """Drop and recreate the media table. Returns count of removed rows."""
    global table
    with _model_lock:
        count = table.count_rows() if table is not None else 0
        if TABLE_NAME in db.table_names():
            db.drop_table(TABLE_NAME)
        table = db.create_table(TABLE_NAME, schema=schema)
        return count


def backup_and_reset_table(backup_name: str) -> int:
    """Rename the live media table to `backup_name`, then create a fresh empty
    media table with the same schema. Used by transactional reindex.

    Returns the row count of the table that was renamed (0 if there was none).
    Raises if `backup_name` already exists so we never silently clobber a prior
    backup that recovery still needs.
    """
    global table
    with _model_lock:
        if backup_name in db.table_names():
            raise RuntimeError(f"Backup table {backup_name!r} already exists; refusing to overwrite.")
        count = 0
        if TABLE_NAME in db.table_names():
            try:
                count = table.count_rows() if table is not None else 0
            except Exception:
                count = 0
            db.rename_table(TABLE_NAME, backup_name)
        table = db.create_table(TABLE_NAME, schema=schema)
        return count


def restore_from_backup(backup_name: str) -> None:
    """Drop the (likely-partial) live media table and rename `backup_name`
    back to it. Used by reindex recovery on failure.
    """
    global table
    with _model_lock:
        if backup_name not in db.table_names():
            raise RuntimeError(f"Backup table {backup_name!r} not found; cannot restore.")
        if TABLE_NAME in db.table_names():
            db.drop_table(TABLE_NAME)
        db.rename_table(backup_name, TABLE_NAME)
        table = db.open_table(TABLE_NAME)


def drop_backup(backup_name: str) -> None:
    """Drop a reindex backup table after a successful reindex."""
    with _model_lock:
        if backup_name in db.table_names():
            db.drop_table(backup_name)


def delete_rows_under_folder(folder_path: str) -> int:
    """Delete rows whose path is exactly the folder or is a strict descendant.

    Uses 'path = prefix OR path LIKE prefix/sep/%' instead of a bare LIKE prefix%
    to prevent false-positive matches on sibling paths with the same prefix
    (e.g. /Photos_old/x.jpg must NOT be deleted when folder_path=/Photos).
    """
    norm = platform_utils.normalize_path(folder_path)
    safe = norm.replace("'", "''")
    # normalize_path uses os.sep (backslash on Windows, forward slash on POSIX).
    # Strip trailing sep so the pattern is deterministic regardless of caller input.
    sep = os.sep
    # SQL string with backslash: LanceDB SQL passes the literal through to Arrow's
    # LIKE matcher. Backslash is not a SQL escape in this dialect, so it's safe inline.
    prefix = safe.rstrip(sep).rstrip("/")
    try:
        table.delete(f"path = '{prefix}' OR path LIKE '{prefix}{sep}%'")
    except Exception as e:
        print(f"[core] delete_rows_under_folder error: {e}")
    return 1  # LanceDB delete doesn't return count


def _count_rows_under_folder(folder_path: str) -> int:
    """Count rows whose path is under the given folder (exact or descendant)."""
    norm = platform_utils.normalize_path(folder_path)
    safe = norm.replace("'", "''")
    sep = os.sep
    prefix = safe.rstrip(sep).rstrip("/")
    try:
        rows = table.to_lance().to_table(
            filter=f"path = '{prefix}' OR path LIKE '{prefix}{sep}%'",
            columns=["id"],
        )
        return rows.num_rows
    except Exception:
        return 0


def get_all_media(limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    """Retrieve all media with basic pagination."""
    try:
        results = (
            table.search()
            .where("is_primary = true")
            .limit(limit + offset)
            .to_list()
        )
        return results[offset:]
    except Exception:
        return []
