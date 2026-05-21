import os
import threading
import torch
import lancedb
from sentence_transformers import SentenceTransformer
import pyarrow as pa
import numpy as np
from typing import List, Dict, Any, Optional
from PIL import Image
from scipy.fft import dct as _dct
import platform_utils
import config

# Determine the best available device for hardware acceleration
device = platform_utils.detect_torch_device()

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

_model: Optional[SentenceTransformer] = None
_model_name: Optional[str] = None
_model_lock = threading.Lock()


def _resolve_model_name(requested: str) -> str:
    """Map a config model name to a supported 768d model.

    Falls back to clip-ViT-L-14 with a warning if the requested model does not
    produce 768-dimensional vectors (e.g. clip-ViT-B-32 outputs 512d).
    """
    if requested in _SUPPORTED_768D:
        return requested
    print(
        f"WARN: model '{requested}' not 768d-compatible with current schema; "
        "using clip-ViT-L-14 instead. Re-index required if model is changed."
    )
    return "clip-ViT-L-14"


def get_model() -> SentenceTransformer:
    """Lazy-load and cache the CLIP model for the current config level.

    Thread-safe: only one thread loads the model; others wait at the lock.
    If the config level changes the model name, the old model is replaced on
    the next call.
    """
    global _model, _model_name
    with _model_lock:
        cfg = config.get_current_level_params()
        wanted = _resolve_model_name(cfg.get("model", "clip-ViT-L-14"))
        if _model is None or _model_name != wanted:
            print(f"Loading CLIP model: {wanted} on device: {device}")
            _model = SentenceTransformer(wanted, device=device)
            _model_name = wanted
        return _model


def get_model_name() -> str:
    """Return the name of the currently-loaded model, or 'unloaded'."""
    return _model_name or "unloaded"


def resolve_model_name(requested: str) -> str:
    """Public wrapper around _resolve_model_name for use in status endpoints."""
    return _resolve_model_name(requested)


def reset_model() -> None:
    """Drop the cached model so the next get_model() call reloads.

    Called by /config/level after a level change so the new model (if any) is
    picked up lazily on the next encode request.
    """
    global _model, _model_name
    with _model_lock:
        _model = None
        _model_name = None

# Set up LanceDB in the backend directory
DB_PATH = os.path.join(os.path.dirname(__file__), "lancedb_data")
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

    # WARN: loads entire table into RAM; fine for <1M rows on local hardware
    rows = table.search().limit(10_000_000).to_list()
    for r in rows:
        r.setdefault("text_snippet", "")

    db.drop_table(TABLE_NAME)
    table = db.create_table(TABLE_NAME, schema=schema)
    if rows:
        table.add(rows)
    print(f"Migration v3 complete: {len(rows)} rows backfilled with empty text_snippet.")


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


def encode_images_batch(images: List[Image.Image], batch_size: int = 64) -> List[List[float]]:
    """Encode a list of PIL images; returns list of 768-D float vectors.

    Internally chunks inputs into at most _MAX_ENCODE_CHUNK images to avoid OOM.
    """
    if len(images) > _MAX_ENCODE_CHUNK:
        result: List[List[float]] = []
        for i in range(0, len(images), _MAX_ENCODE_CHUNK):
            result.extend(encode_images_batch(images[i:i + _MAX_ENCODE_CHUNK], batch_size=batch_size))
        return result

    # fp16 autocast halves VRAM and speeds inference on CUDA without quality loss for retrieval
    if device == "cuda":
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16):
            vecs = get_model().encode(
                images,
                batch_size=batch_size,
                convert_to_numpy=True,
                normalize_embeddings=False,
            )
    else:
        with torch.inference_mode():
            vecs = get_model().encode(
                images,
                batch_size=batch_size,
                convert_to_numpy=True,
                normalize_embeddings=False,
            )
    return [v.tolist() for v in vecs]


def encode_image(image: Image.Image) -> List[float]:
    """Encode a single PIL Image; thin wrapper around encode_images_batch."""
    return encode_images_batch([image])[0]


def encode_text(text: str) -> List[float]:
    """Encode text query to vector using CLIP."""
    with torch.inference_mode():
        return get_model().encode(text).tolist()


def encode_texts_batch(texts: List[str]) -> List[List[float]]:
    """Encode a list of text strings in one batch; returns list of 768-D float vectors."""
    with torch.inference_mode():
        vecs = get_model().encode(texts, convert_to_numpy=True, batch_size=64)
    return vecs.tolist()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def add_media(records: List[Dict[str, Any]]) -> None:
    """Insert a batch of media records into LanceDB."""
    # Ensure schema columns always present so inserts never fail on missing keys
    for r in records:
        r.setdefault("video_id", "")
        r.setdefault("text_snippet", "")
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

    raw = (
        table.search(query_vector)
        .metric("cosine")
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
