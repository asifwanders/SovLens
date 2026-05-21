"""ANN index build utility for the SovLens LanceDB media table.

Brute-force kNN is accurate and fast enough up to ~50–100k rows.
Beyond that, an IVF_PQ ANN index cuts query latency significantly.
Call build_ann_index() after large ingestion batches or on demand.
"""

import math
import threading

import core

# Row count above which an ANN index provides meaningful speedup.
ANN_INDEX_THRESHOLD = 50_000

# Clamp num_partitions to this range to avoid degenerate IVF configs.
MIN_PARTITIONS = 8
MAX_PARTITIONS = 256

_index_build_lock = threading.Lock()


def _choose_num_partitions(num_rows: int) -> int:
    """Return a reasonable IVF partition count: sqrt(num_rows), clamped to [8, 256].

    sqrt heuristic balances cluster granularity against per-partition overhead.
    """
    raw = int(math.sqrt(max(num_rows, 1)))
    return max(MIN_PARTITIONS, min(MAX_PARTITIONS, raw))


def build_ann_index(num_sub_vectors: int = 96) -> dict:
    """Build (or rebuild) an IVF_PQ ANN index on the media table's vector column.

    Safe to call multiple times — LanceDB replaces the existing index.

    Args:
        num_sub_vectors: PQ compression sub-vectors (must divide 768). 96 gives
            good recall at 8× compression; reduce to 64 for smaller tables.

    Returns:
        Dict with 'num_rows' and 'num_partitions' for logging/response.
    """
    if not _index_build_lock.acquire(blocking=False):
        return {"num_rows": 0, "num_partitions": 0, "message": "Index build already in progress; skipped."}

    try:
        tbl = core.table
        num_rows: int = tbl.count_rows()

        if num_rows == 0:
            return {"num_rows": 0, "num_partitions": 0, "message": "Table is empty; skipped."}

        num_partitions = _choose_num_partitions(num_rows)

        # LanceDB create_index rebuilds in-place; no explicit drop needed.
        tbl.create_index(
            metric="cosine",
            vector_column_name="vector",
            num_partitions=num_partitions,
            num_sub_vectors=num_sub_vectors,
            replace=True,
        )

        return {
            "num_rows": num_rows,
            "num_partitions": num_partitions,
            "message": f"ANN index built: {num_partitions} partitions over {num_rows} rows.",
        }
    finally:
        _index_build_lock.release()


def should_build_index() -> bool:
    """Return True when the table row count exceeds the ANN index threshold."""
    try:
        return core.table.count_rows() >= ANN_INDEX_THRESHOLD
    except Exception:
        return False
