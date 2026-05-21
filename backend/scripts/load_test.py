"""SovLens load test harness.

Validates the ingestion + search pipeline on a synthetic video corpus.
Measures performance and verifies ANN index threshold behaviour.

Usage:
    python scripts/load_test.py --videos 3 --duration 5

    # Keep files after run (recommended on Windows to avoid AV-scanner races):
    python scripts/load_test.py --videos 3 --keep

    # Check Tauri dev server is reachable:
    python scripts/load_test.py --videos 3 --check-frontend

Python 3.9 compatible — no match statements, no X | Y unions.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Ensure the backend directory is on sys.path so we can import core etc.
# ---------------------------------------------------------------------------

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import platform_utils  # noqa: E402 — must come after sys.path patch

# ---------------------------------------------------------------------------
# Cross-platform defaults
# ---------------------------------------------------------------------------

DEFAULT_CORPUS = os.path.join(platform_utils.get_temp_dir(), "sovlens_test_corpus")
DEFAULT_DB = os.path.join(platform_utils.get_temp_dir(), "sovlens_test_db")

# ---------------------------------------------------------------------------
# 1. Synthetic corpus generator
# ---------------------------------------------------------------------------

# (source_name, extra_args) tuples — cycled across the generated videos
_LAVFI_SOURCES: List[Tuple[str, List[str]]] = [
    ("testsrc2=size=640x480:rate=30", []),
    ("mandelbrot=size=640x480:rate=30", []),
    ("rgbtestsrc=size=640x480:rate=30", []),
    ("haldclutsrc=size=640x480:rate=30", []),
    ("smptebars=size=640x480:rate=30", []),
    ("pal75bars=size=640x480:rate=30", []),
    ("color=c=red:size=640x480:rate=30", []),
    ("color=c=blue:size=640x480:rate=30", []),
]

# Duration range (seconds) for generated videos — cycled
_DURATIONS: List[int] = [3, 4, 5, 6, 7, 8]


def _get_ffmpeg() -> str:
    """Return path to the ffmpeg binary via imageio_ffmpeg."""
    try:
        return platform_utils.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def generate_corpus(out_dir: str, count: int, seconds: int = 5) -> List[str]:
    """Generate *count* synthetic mp4 files in *out_dir*.

    Cycles through multiple lavfi sources so videos are visually distinct.
    Skips files that already exist (idempotent). Returns list of created paths.

    Args:
        out_dir: Directory to write mp4 files into.
        count: Number of videos to generate.
        seconds: Default duration when not cycling durations.

    Returns:
        List of absolute paths to all mp4 files in out_dir after generation.
    """
    os.makedirs(out_dir, exist_ok=True)
    ffmpeg = _get_ffmpeg()
    paths: List[str] = []

    print(f"\n[generate] Creating {count} synthetic videos in {out_dir}")

    for i in range(count):
        out_path = os.path.join(out_dir, f"test_video_{i:04d}.mp4")
        if os.path.exists(out_path):
            print(f"  skip (exists): {os.path.basename(out_path)}")
            paths.append(out_path)
            continue

        source_name, extra_args = _LAVFI_SOURCES[i % len(_LAVFI_SOURCES)]
        duration = _DURATIONS[i % len(_DURATIONS)]

        cmd = [
            ffmpeg,
            "-f", "lavfi",
            "-i", source_name,
            "-t", str(duration),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-y",
            out_path,
        ] + extra_args

        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if result.returncode == 0 and os.path.exists(out_path):
            print(f"  created: {os.path.basename(out_path)} ({source_name[:20]}... {duration}s)")
            paths.append(out_path)
        else:
            print(f"  FAILED: {os.path.basename(out_path)} (ffmpeg returned {result.returncode})")

    print(f"[generate] Done: {len(paths)}/{count} videos ready.")
    return paths


# ---------------------------------------------------------------------------
# 2. Isolated ingestion run
# ---------------------------------------------------------------------------

def run_ingest(corpus_dir: str, db_path: str) -> Dict:
    """Ingest all videos in corpus_dir into an isolated LanceDB at db_path.

    Uses monkey-patching to redirect core.py's DB_PATH / db / table globals
    and ingestion.py's _PROGRESS_PATH before importing those modules, so the
    user's real lancedb_data is never touched.

    Args:
        corpus_dir: Folder containing synthetic mp4 files.
        db_path: Path to an isolated LanceDB directory (created if absent).

    Returns:
        Dict with keys: total_videos, total_frames, elapsed_sec, frames_per_sec.
    """
    import importlib

    os.makedirs(db_path, exist_ok=True)
    progress_path = os.path.join(db_path, "progress.json")

    print(f"\n[ingest] Redirecting DB -> {db_path}")

    # --- Monkey-patch core BEFORE importing ingestion ---
    # core.py connects at module level; we must override DB_PATH and reconnect.
    import core as _core
    import lancedb

    _core.DB_PATH = db_path
    _core.db = lancedb.connect(db_path)
    # Open or create the table in the newly-patched _core.db
    if _core.TABLE_NAME not in _core.db.table_names():
        _core.table = _core.db.create_table(_core.TABLE_NAME, schema=_core.schema)
    else:
        _core.table = _core.db.open_table(_core.TABLE_NAME)

    # Also patch ingestion's _PROGRESS_PATH so checkpoint json goes to test db
    import ingestion as _ingestion
    _ingestion._PROGRESS_PATH = progress_path

    # --- Run ingestion ---
    print(f"[ingest] Processing folder: {corpus_dir}")
    t0 = time.perf_counter()

    # Capture stdout dots by calling process_folder directly
    video_files = _ingestion.get_media_files(corpus_dir)
    total_videos = len(video_files)
    print(f"[ingest] Found {total_videos} media files. Ingesting", end="", flush=True)

    # We need frame count — patch add_media to count inserts
    _frame_count: List[int] = [0]
    _original_add_media = _core.add_media

    def _counting_add_media(records: List[Dict]) -> None:
        _frame_count[0] += len(records)
        _original_add_media(records)
        print(".", end="", flush=True)

    _core.add_media = _counting_add_media

    try:
        _ingestion.process_folder(corpus_dir)
    finally:
        _core.add_media = _original_add_media

    elapsed = time.perf_counter() - t0
    total_frames = _frame_count[0]
    fps = total_frames / elapsed if elapsed > 0 else 0.0

    print()  # newline after dots
    result = {
        "total_videos": total_videos,
        "total_frames": total_frames,
        "elapsed_sec": round(elapsed, 2),
        "frames_per_sec": round(fps, 2),
    }
    print(f"[ingest] {total_videos} videos, {total_frames} frames in {elapsed:.2f}s ({fps:.2f} fps)")
    return result


# ---------------------------------------------------------------------------
# 3. Search latency test
# ---------------------------------------------------------------------------

_DEFAULT_QUERIES: List[str] = [
    "abstract pattern",
    "colorful gradient",
    "mandelbrot fractal",
    "test signal",
    "red color",
    "blue color",
]


def run_search_test(
    queries: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict:
    """Run each query against the DB and report latency statistics.

    Args:
        queries: List of text queries to run.
        db_path: Unused (DB already patched via run_ingest or caller). Kept for
                 API symmetry so the function is importable standalone.

    Returns:
        Dict with p50_ms, p95_ms, max_ms, query_count.
    """
    import core as _core

    if queries is None:
        queries = _DEFAULT_QUERIES

    print(f"\n[search] Running {len(queries)} queries ...")
    latencies: List[float] = []

    for q in queries:
        t0 = time.perf_counter()
        vec = _core.encode_text(q)
        results = _core.search_media(vec, limit=10, min_score=0.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed_ms)
        print(f"  '{q}' -> {len(results)} hits in {elapsed_ms:.1f}ms")

    latencies.sort()
    n = len(latencies)
    p50 = latencies[n // 2]
    p95 = latencies[min(int(n * 0.95), n - 1)]
    max_ms = latencies[-1]

    result = {
        "query_count": n,
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "max_ms": round(max_ms, 1),
    }
    print(f"[search] p50={p50:.1f}ms  p95={p95:.1f}ms  max={max_ms:.1f}ms")
    return result


# ---------------------------------------------------------------------------
# 4. ANN index trigger test
# ---------------------------------------------------------------------------

def test_index_trigger(db_path: str, stub_threshold: int = 50) -> Dict:
    """Verify should_build_index() and (optionally) build the index on small data.

    Temporarily lowers ANN_INDEX_THRESHOLD to *stub_threshold* so we can
    exercise the full build path without needing 50k real frames.

    Args:
        db_path: Path to the test LanceDB (for logging; DB already patched).
        stub_threshold: Override threshold — set below current row count to
                        force index build during smoke tests.

    Returns:
        Dict with row_count, above_real_threshold, index_built, message.
    """
    import core as _core
    import index_build as _ib

    row_count = _core.table.count_rows()
    above_real = row_count >= 50_000

    print(f"\n[index] Row count: {row_count}  (real threshold: 50,000 -> above={above_real})")

    # Verify real threshold behaviour
    natural_result = _ib.should_build_index()
    assert natural_result == above_real, (
        f"should_build_index() returned {natural_result}, expected {above_real}"
    )

    # Stub threshold to test build path
    _ib.ANN_INDEX_THRESHOLD = stub_threshold
    triggered = _ib.should_build_index()
    print(f"[index] Stubbed threshold to {stub_threshold} -> should_build={triggered}")

    index_built = False
    build_info: Dict = {}
    # IVF_PQ requires at least MIN_PARTITIONS (8) rows to train centroids.
    min_rows_for_index = _ib.MIN_PARTITIONS
    if triggered and row_count >= min_rows_for_index:
        print("[index] Building ANN index on test data ...")
        try:
            build_info = _ib.build_ann_index(num_sub_vectors=16)  # 16 divides 768, fast for tiny data
            index_built = True
            print(f"[index] {build_info.get('message', 'done')}")
        except Exception as exc:
            build_info = {"message": f"Index build failed (expected for tiny corpus): {exc}"}
            print(f"[index] WARNING: {build_info['message']}")
    elif triggered and row_count < min_rows_for_index:
        build_info = {
            "message": (
                f"Skipped: IVF_PQ needs >= {min_rows_for_index} rows to train centroids "
                f"(have {row_count}). Use --videos 10+ to exercise full index build."
            )
        }
        print(f"[index] {build_info['message']}")
    elif row_count == 0:
        print("[index] Table is empty — skipping index build.")
    else:
        print(f"[index] Row count {row_count} < stub threshold {stub_threshold} — not triggered.")

    # Restore threshold
    _ib.ANN_INDEX_THRESHOLD = 50_000

    return {
        "row_count": row_count,
        "above_real_threshold": above_real,
        "stubbed_triggered": triggered,
        "index_built": index_built,
        "build_info": build_info,
    }


# ---------------------------------------------------------------------------
# 5. Platform diagnostics
# ---------------------------------------------------------------------------

def _whisper_available() -> bool:
    """Return True if whisper is importable."""
    try:
        import whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _heic_supported() -> Optional[bool]:
    """Return True/False if pillow-heif is available, else None."""
    try:
        import pillow_heif  # noqa: F401
        return True
    except ImportError:
        return None


def print_platform_diagnostics() -> Dict:
    """Print a platform diagnostic block and return a dict of the values."""
    if platform_utils.IS_WINDOWS:
        platform_label = "Windows"
    elif platform_utils.IS_MACOS:
        platform_label = f"macOS ({sys.platform})"
    else:
        platform_label = f"Linux ({sys.platform})"

    py_version = sys.version.split()[0]
    torch_device = platform_utils.detect_torch_device()
    hw_encoder = platform_utils.detect_hwaccel_encoder()
    hw_decoder = platform_utils.detect_hwaccel_decoder()
    plays_vp9 = platform_utils.webview_plays_vp9()
    plays_hevc = platform_utils.webview_plays_hevc()
    whisper_ok = _whisper_available()
    heic = _heic_supported()

    try:
        ffmpeg_path = platform_utils.get_ffmpeg_exe()
    except Exception:
        ffmpeg_path = shutil.which("ffmpeg") or "not found"

    print("\n" + "=" * 60)
    print("  PLATFORM DIAGNOSTICS")
    print("=" * 60)
    print(f"  Platform         : {platform_label}")
    print(f"  Python           : {py_version}")
    print(f"  Torch device     : {torch_device}")
    print(f"  HW encoder       : {hw_encoder}")
    print(f"  HW decoder       : {hw_decoder}")
    print(f"  WebView VP9      : {plays_vp9}")
    print(f"  WebView HEVC     : {plays_hevc}")
    print(f"  Whisper avail    : {whisper_ok}")
    if heic is not None:
        print(f"  HEIC supported   : {heic}")
    print(f"  ffmpeg           : {ffmpeg_path}")
    print("=" * 60)

    diag: Dict = {
        "platform": platform_label,
        "python": py_version,
        "torch_device": torch_device,
        "hw_encoder": hw_encoder,
        "hw_decoder": hw_decoder,
        "webview_vp9": plays_vp9,
        "webview_hevc": plays_hevc,
        "whisper_available": whisper_ok,
        "ffmpeg": ffmpeg_path,
    }
    if heic is not None:
        diag["heic_supported"] = heic
    return diag


# ---------------------------------------------------------------------------
# 6. Encoder smoke test
# ---------------------------------------------------------------------------

def test_encoder() -> Dict:
    """Transcode the first available test video using the detected hwaccel encoder.

    Returns:
        Dict with success, encoder_args, elapsed_sec, error (if any).
    """
    print("\n[encoder] Running encoder smoke test ...")

    encoder_args = platform_utils.detect_hwaccel_encoder()
    print(f"[encoder] Using encoder args: {encoder_args}")

    # Find a source video — look in DEFAULT_CORPUS and temp dir
    source: Optional[str] = None
    for candidate_dir in (DEFAULT_CORPUS,):
        if os.path.isdir(candidate_dir):
            for fname in sorted(os.listdir(candidate_dir)):
                if fname.endswith(".mp4"):
                    source = os.path.join(candidate_dir, fname)
                    break
        if source:
            break

    if not source:
        msg = "No source video found for encoder smoke test (corpus not yet generated?)"
        print(f"[encoder] SKIP: {msg}")
        return {"success": False, "encoder_args": encoder_args, "elapsed_sec": 0.0, "error": msg}

    ffmpeg = _get_ffmpeg()
    tmp_out = os.path.join(platform_utils.get_temp_dir(), "sovlens_encoder_smoke.mp4")

    cmd = [
        ffmpeg,
        "-y",
        "-i", source,
        "-t", "2",
    ] + encoder_args + [
        "-pix_fmt", "yuv420p",
        tmp_out,
    ]

    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        elapsed = time.perf_counter() - t0
        success = result.returncode == 0 and os.path.exists(tmp_out)
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        print(f"[encoder] FAILED: {exc}")
        return {"success": False, "encoder_args": encoder_args, "elapsed_sec": round(elapsed, 2), "error": str(exc)}
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass

    if success:
        print(f"[encoder] OK — transcode completed in {elapsed:.2f}s")
    else:
        stderr_tail = (result.stderr or "")[-300:]
        print(f"[encoder] FAILED (rc={result.returncode}): ...{stderr_tail}")

    return {
        "success": success,
        "encoder_args": encoder_args,
        "elapsed_sec": round(elapsed, 2),
        "error": None if success else f"ffmpeg rc={result.returncode}",
    }


# ---------------------------------------------------------------------------
# 7. Path normalization sanity check
# ---------------------------------------------------------------------------

def test_path_normalization(test_path: str) -> Dict:
    """Assert normalize_path() is idempotent and print before/after.

    Args:
        test_path: A filesystem path to normalize (typically the corpus dir).

    Returns:
        Dict with original, normalized, idempotent.
    """
    print("\n[normalize] Path normalization sanity check ...")

    normalized_once = platform_utils.normalize_path(test_path)
    normalized_twice = platform_utils.normalize_path(normalized_once)
    idempotent = normalized_once == normalized_twice

    print(f"  original   : {test_path}")
    print(f"  normalized : {normalized_once}")
    print(f"  idempotent : {idempotent}")

    assert idempotent, (
        f"normalize_path is NOT idempotent!\n"
        f"  once : {normalized_once}\n"
        f"  twice: {normalized_twice}"
    )
    print("[normalize] OK")

    return {
        "original": test_path,
        "normalized": normalized_once,
        "idempotent": idempotent,
    }


# ---------------------------------------------------------------------------
# 8. Frontend reachability test (optional)
# ---------------------------------------------------------------------------

def test_frontend_reachability(url: str = "http://localhost:3000") -> Dict:
    """Attempt to reach the frontend dev server and report HTTP status.

    Uses urllib (stdlib only) so no new dependencies.

    Args:
        url: URL to check (default: http://localhost:3000).

    Returns:
        Dict with url, reachable, status_code, error.
    """
    print(f"\n[frontend] Checking reachability of {url} ...")
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.urlopen(url, timeout=5)
        status = req.status
        req.close()
        print(f"[frontend] OK — HTTP {status}")
        return {"url": url, "reachable": True, "status_code": status, "error": None}
    except urllib.error.HTTPError as exc:
        print(f"[frontend] HTTP error {exc.code}")
        return {"url": url, "reachable": True, "status_code": exc.code, "error": str(exc)}
    except Exception as exc:
        print(f"[frontend] NOT reachable: {exc}")
        return {"url": url, "reachable": False, "status_code": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# 9. Summary printer + JSON writer
# ---------------------------------------------------------------------------

def _print_summary(
    gen_result: Dict,
    ingest_result: Dict,
    search_result: Dict,
    index_result: Dict,
) -> None:
    """Print a formatted summary table."""
    print("\n" + "=" * 60)
    print("  SOVLENS LOAD TEST SUMMARY")
    print("=" * 60)

    print("\n  GENERATION")
    print(f"    Videos created  : {gen_result.get('created', 'n/a')}")
    print(f"    Generate time   : {gen_result.get('elapsed_sec', 'n/a')}s")

    print("\n  INGESTION")
    print(f"    Total videos    : {ingest_result.get('total_videos', 0)}")
    print(f"    Total frames    : {ingest_result.get('total_frames', 0)}")
    print(f"    Elapsed         : {ingest_result.get('elapsed_sec', 0)}s")
    print(f"    Throughput      : {ingest_result.get('frames_per_sec', 0)} frames/s")

    print("\n  SEARCH LATENCY")
    print(f"    Queries run     : {search_result.get('query_count', 0)}")
    print(f"    p50             : {search_result.get('p50_ms', 0)} ms")
    print(f"    p95             : {search_result.get('p95_ms', 0)} ms")
    print(f"    max             : {search_result.get('max_ms', 0)} ms")

    print("\n  ANN INDEX")
    print(f"    Row count       : {index_result.get('row_count', 0)}")
    print(f"    Above 50k       : {index_result.get('above_real_threshold', False)}")
    print(f"    Stubbed trigger : {index_result.get('stubbed_triggered', False)}")
    print(f"    Index built     : {index_result.get('index_built', False)}")
    if index_result.get("build_info"):
        print(f"    Build msg       : {index_result['build_info'].get('message', '')}")

    print("\n" + "=" * 60)


def _write_json_summary(db_path: str, summary: Dict) -> str:
    """Write summary dict to <db_path>/load_test_summary.json and return path."""
    os.makedirs(db_path, exist_ok=True)
    out_path = os.path.join(db_path, "load_test_summary.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    print(f"\n[summary] JSON written -> {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# 10. Main CLI
# ---------------------------------------------------------------------------

def main() -> int:
    _win_keep_note = (
        " On Windows, AV scanners may interfere with rapid file delete/recreate;"
        " --keep is safer for repeated runs."
    )

    parser = argparse.ArgumentParser(
        description="SovLens ingestion + search load test harness."
    )
    parser.add_argument("--videos", type=int, default=10,
                        help="Number of synthetic videos to generate (default: 10)")
    parser.add_argument("--duration", type=int, default=5,
                        help="Default video duration in seconds (default: 5)")
    parser.add_argument(
        "--corpus-dir",
        default=DEFAULT_CORPUS,
        help=f"Directory for synthetic videos (default: {DEFAULT_CORPUS})",
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB,
        help=f"Isolated LanceDB path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help=(
            "Retain corpus and DB after the test (default: clean up)."
            + (_win_keep_note if platform_utils.IS_WINDOWS else "")
        ),
    )
    parser.add_argument("--skip-generate", action="store_true",
                        help="Skip generation; assume corpus-dir already populated")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="Skip ingestion; assume DB already populated")
    parser.add_argument("--ann-stub-threshold", type=int, default=50,
                        help="Row count threshold override for ANN trigger test (default: 50)")
    parser.add_argument(
        "--check-frontend",
        action="store_true",
        help="Also check that the Tauri dev frontend (http://localhost:3000) is reachable.",
    )
    parser.add_argument(
        "--frontend-url",
        default="http://localhost:3000",
        help="Frontend URL to check when --check-frontend is set (default: http://localhost:3000)",
    )

    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Phase 0: Platform diagnostics
    # -----------------------------------------------------------------------
    diag_result = print_platform_diagnostics()

    gen_result: Dict = {"created": 0, "elapsed_sec": 0.0}
    ingest_result: Dict = {}
    search_result: Dict = {}
    index_result: Dict = {}
    encoder_result: Dict = {}
    normalize_result: Dict = {}
    frontend_result: Dict = {}

    _summary_written_path: Optional[str] = None

    try:
        # Phase 1: Generate corpus
        if not args.skip_generate:
            t0 = time.perf_counter()
            paths = generate_corpus(args.corpus_dir, args.videos, args.duration)
            gen_elapsed = time.perf_counter() - t0
            gen_result = {"created": len(paths), "elapsed_sec": round(gen_elapsed, 2)}
        else:
            print(f"[generate] Skipped — using existing corpus at {args.corpus_dir}")

        # Phase 1b: Encoder smoke test (after corpus exists)
        encoder_result = test_encoder()

        # Phase 1c: Path normalization
        normalize_result = test_path_normalization(args.corpus_dir)

        # Phase 2: Ingest
        if not args.skip_ingest:
            ingest_result = run_ingest(args.corpus_dir, args.db_path)
        else:
            # Still need to patch core so search / index phases work
            import core as _core
            import lancedb
            _core.DB_PATH = args.db_path
            _core.db = lancedb.connect(args.db_path)
            if _core.TABLE_NAME not in _core.db.table_names():
                _core.table = _core.db.create_table(_core.TABLE_NAME, schema=_core.schema)
            else:
                _core.table = _core.db.open_table(_core.TABLE_NAME)
            ingest_result = {"total_videos": 0, "total_frames": 0,
                             "elapsed_sec": 0, "frames_per_sec": 0}
            print(f"[ingest] Skipped — using existing DB at {args.db_path}")

        # Phase 3: Search latency
        search_result = run_search_test()

        # Phase 4: ANN index trigger
        index_result = test_index_trigger(args.db_path, stub_threshold=args.ann_stub_threshold)

        # Phase 5: Frontend reachability (optional)
        if args.check_frontend:
            frontend_result = test_frontend_reachability(args.frontend_url)

    except Exception as exc:
        print(f"\n[ERROR] Phase failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    finally:
        # Write JSON summary before cleanup so the file path is meaningful
        _full_summary: Dict = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "platform": diag_result,
            "generation": gen_result,
            "encoder_smoke": encoder_result,
            "path_normalization": normalize_result,
            "ingestion": ingest_result,
            "search": search_result,
            "ann_index": index_result,
        }
        if frontend_result:
            _full_summary["frontend"] = frontend_result

        # Only write if db_path exists (or can be created)
        _db_path_for_summary = args.db_path if hasattr(args, "db_path") else DEFAULT_DB
        try:
            _summary_written_path = _write_json_summary(_db_path_for_summary, _full_summary)
        except Exception as _exc:
            print(f"[summary] WARNING: could not write JSON summary: {_exc}", file=sys.stderr)

        if not args.keep:
            print("\n[cleanup] Removing test corpus and DB ...")
            for path in (args.corpus_dir, args.db_path):
                if os.path.exists(path):
                    shutil.rmtree(path, ignore_errors=True)
                    print(f"  removed: {path}")

    _print_summary(gen_result, ingest_result, search_result, index_result)

    if _summary_written_path and not args.keep:
        print(f"  NOTE: JSON summary was written before cleanup — file may no longer exist.")
    elif _summary_written_path:
        print(f"\n  JSON summary: {_summary_written_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
