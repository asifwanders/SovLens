"use client";

import { useState, useEffect, useRef, useCallback, useSyncExternalStore } from "react";
import SplashScreen from "@/components/SplashScreen";
import MediaGrid from "@/components/MediaGrid";
import { MediaItem } from "@/lib/types";
import MediaModal from "@/components/MediaModal";
import { RefreshCw, FolderPlus, FileImage, Image as ImageIcon, Upload } from "lucide-react";
import { getCurrentWebview } from "@tauri-apps/api/webview";

const API = "http://127.0.0.1:14793";
const SESSION_KEY = "sovlens.firstLoadDone";
const SYNC_MIN_MS = 600;

// useSyncExternalStore-friendly subscriber: sessionStorage doesn't fire events,
// so we provide a no-op subscribe + a manual bump via a module-level emitter.
let _splashBump = 0;
const _splashListeners = new Set<() => void>();
function _bumpSplash() {
  _splashBump++;
  _splashListeners.forEach((cb) => cb());
}
function _subscribeSplash(cb: () => void) {
  _splashListeners.add(cb);
  return () => _splashListeners.delete(cb);
}

export default function Home() {
  // ── A. Loading: only on first app open ──────────────────────────────────
  // useSyncExternalStore handles SSR/client snapshot mismatch correctly:
  // server snapshot = false (no splash), client snapshot = sessionStorage value.
  const showSplash = useSyncExternalStore(
    _subscribeSplash,
    () => sessionStorage.getItem(SESSION_KEY) !== "1",
    () => false,
  );

  const handleSplashComplete = useCallback(() => {
    sessionStorage.setItem(SESSION_KEY, "1");
    _bumpSplash();
  }, []);

  // ── Media state ──────────────────────────────────────────────────────────
  const [media, setMedia] = useState<MediaItem[]>([]);
  const [hasMore, setHasMore] = useState(true);
  const [offset, setOffset] = useState(0);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);

  // ── B. Sync animation ────────────────────────────────────────────────────
  const [syncing, setSyncing] = useState(false);
  const [isIngesting, setIsIngesting] = useState(false);
  const isIngestingRef = useRef(false);
  // Track backend's media_generation counter — bumps on every add/delete.
  // Lets the home grid refresh after a folder is removed (or anything else
  // mutates the table) without waiting for an ingest-end edge transition.
  const mediaGenRef = useRef<number>(-1);
  // Job progress is rendered globally by JobBanner (mounted in
  // app/layout.tsx). Home page only needs ingestion-end detection to
  // know when to refetch /media.

  // ── A. Drag-drop state ───────────────────────────────────────────────────
  const [dragOver, setDragOver] = useState(false);

  // ── Status banner ────────────────────────────────────────────────────────
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const statusTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showStatus = useCallback((msg: string) => {
    if (statusTimer.current) clearTimeout(statusTimer.current);
    setStatusMsg(msg);
    statusTimer.current = setTimeout(() => setStatusMsg(null), 4000);
  }, []);

  // ── Known paths set for dedup ────────────────────────────────────────────
  const knownPathsRef = useRef<Set<string>>(new Set());

  const refreshKnownPaths = useCallback(async () => {
    try {
      const res = await fetch(`${API}/media?limit=10000&offset=0`);
      if (!res.ok) return;
      const data = await res.json() as { items?: MediaItem[] };
      const paths = new Set<string>(
        (data.items ?? []).map((item) => item.path).filter(Boolean)
      );
      knownPathsRef.current = paths;
    } catch {
      // best-effort; backend dedup still runs
    }
  }, []);

  // ── Fetch paginated media ────────────────────────────────────────────────
  const fetchMedia = useCallback(async (currentOffset: number) => {
    try {
      const res = await fetch(`${API}/media?limit=20&offset=${currentOffset}`);
      if (!res.ok) throw new Error(`Backend error: ${res.status}`);
      const data = await res.json() as { items?: MediaItem[] };
      if (data.items && data.items.length > 0) {
        if (currentOffset === 0) {
          setMedia(data.items);
        } else {
          setMedia((prev) => {
            const existingIds = new Set(prev.map((item) => item.id));
            const newItems = data.items!.filter((item) => !existingIds.has(item.id));
            return [...prev, ...newItems];
          });
        }
        setOffset(currentOffset + 20);
      } else {
        setHasMore(false);
      }
    } catch (e) {
      console.error("Failed to fetch media", e);
    }
  }, []);

  // ── Status polling ────────────────────────────────────────────────────────
  useEffect(() => {
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API}/status`);
        const data = await res.json() as {
          is_ingesting: boolean;
          media_generation?: number;
        };

        // Refresh on ingest-end edge OR on media_generation bump (covers
        // folder deletes which don't go through the ingest path).
        const gen = data.media_generation ?? 0;
        const ingestJustEnded = isIngestingRef.current && !data.is_ingesting;
        const generationChanged = mediaGenRef.current !== -1 && mediaGenRef.current !== gen;
        if (ingestJustEnded || generationChanged) {
          setOffset(0);
          setHasMore(true);
          fetchMedia(0);
          refreshKnownPaths();
        }
        mediaGenRef.current = gen;

        isIngestingRef.current = data.is_ingesting;
        setIsIngesting(data.is_ingesting);
      } catch {
        // ignore polling errors
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [fetchMedia, refreshKnownPaths]);

  // ── Initial load + Tauri drag-drop ───────────────────────────────────────
  useEffect(() => {
    if (!showSplash) {
      // Defer past render commit to satisfy react-hooks/set-state-in-effect
      queueMicrotask(() => {
        fetchMedia(0);
        refreshKnownPaths();
      });
    }

    let unlisten: (() => void) | undefined;

    async function setupDragDrop() {
      try {
        unlisten = await getCurrentWebview().onDragDropEvent(async (event) => {
          const payload = event.payload;

          if (payload.type === "enter") {
            setDragOver(true);
            return;
          }

          if (payload.type === "over") {
            setDragOver(true);
            return;
          }

          if (payload.type === "leave") {
            setDragOver(false);
            return;
          }

          if (payload.type === "drop") {
            setDragOver(false);
            const paths = payload.paths ?? [];
            if (paths.length === 0) return;

            const known = knownPathsRef.current;
            const newPaths = paths.filter((p) => !known.has(p));
            const skipped = paths.length - newPaths.length;

            if (newPaths.length === 0) {
              showStatus(`Skipped ${skipped} already-added file${skipped !== 1 ? "s" : ""}.`);
              return;
            }

            // Per-path fetch + ok check. allSettled only reflects whether the
            // request itself rejected — a 4xx/5xx still resolves, so we must
            // inspect res.ok inside the map to know real backend success.
            const results = await Promise.allSettled(
              newPaths.map(async (p) => {
                const res = await fetch(`${API}/add_file`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ filepath: p }),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                return p;
              })
            );

            const succeeded: string[] = [];
            let failed = 0;
            results.forEach((r) => {
              if (r.status === "fulfilled") {
                succeeded.push(r.value);
              } else {
                failed++;
              }
            });

            // Only mark genuinely-added paths as known so retries are possible.
            succeeded.forEach((p) => known.add(p));

            const addedPart = `Added ${succeeded.length} file${succeeded.length !== 1 ? "s" : ""}`;
            let msg: string;
            if (failed > 0) {
              msg = `${addedPart}, failed ${failed} (check Settings → Logs)`;
              if (skipped > 0) {
                msg += `, skipped ${skipped} duplicate${skipped !== 1 ? "s" : ""}`;
              }
              msg += ".";
            } else if (skipped > 0) {
              msg = `${addedPart}, skipped ${skipped} duplicate${skipped !== 1 ? "s" : ""}.`;
            } else {
              msg = `${addedPart}.`;
            }
            showStatus(msg);

            setOffset(0);
            setHasMore(true);
            fetchMedia(0);
          }
        });
      } catch {
        console.log("Tauri drag-drop listener unavailable (browser mode).");
      }
    }

    setupDragDrop();

    return () => {
      if (unlisten) unlisten();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSplash]);

  // ── Add folder ────────────────────────────────────────────────────────────
  const handleAddFolder = async () => {
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selectedPath = await open({ directory: true, multiple: false });
      if (selectedPath && typeof selectedPath === "string") {
        showStatus("Adding folder…");
        const res = await fetch(`${API}/add_folder`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ folder_path: selectedPath }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        showStatus("Folder added");
        setOffset(0);
        setHasMore(true);
        fetchMedia(0);
      }
    } catch (e) {
      console.error("add folder failed", e);
      showStatus(`Add folder failed: ${String(e)}`);
    }
  };

  // ── Add media files ───────────────────────────────────────────────────────
  const handleAddMedia = async () => {
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const result = await open({
        multiple: true,
        filters: [
          { name: "Images", extensions: ["jpg", "jpeg", "png", "gif", "webp", "heic", "heif"] },
          { name: "Videos", extensions: ["mp4", "mov", "mkv", "webm", "avi"] },
          { name: "All Files", extensions: ["*"] },
        ],
      });
      if (!result) return;
      const paths = Array.isArray(result) ? result : [result];
      if (paths.length === 0) return;
      const res = await fetch(`${API}/add_files`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filepaths: paths }),
      });
      const data = await res.json() as { accepted: number; rejected: { path: string; reason: string }[] };
      showStatus(
        `Adding ${data.accepted} file${data.accepted === 1 ? "" : "s"}${data.rejected.length > 0 ? `, ${data.rejected.length} skipped` : ""}`
      );
      setTimeout(() => {
        setOffset(0);
        setHasMore(true);
        fetchMedia(0);
      }, 2000);
    } catch (e) {
      console.error("add media failed", e);
      showStatus(`Could not open file picker: ${String(e)}`);
    }
  };

  // ── B. Sync all ───────────────────────────────────────────────────────────
  const handleSyncAll = async () => {
    if (syncing) return;
    setSyncing(true);
    const start = Date.now();
    try {
      await fetch(`${API}/sync_all`, { method: "POST" });
      setOffset(0);
      setHasMore(true);
      fetchMedia(0);
    } catch (e) {
      console.error(e);
    } finally {
      const elapsed = Date.now() - start;
      const remaining = SYNC_MIN_MS - elapsed;
      if (remaining > 0) {
        setTimeout(() => setSyncing(false), remaining);
      } else {
        setSyncing(false);
      }
    }
  };

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="h-full w-full relative">

      {/* C. Splash — full-screen z-[100] covers sidebar, shown only once per session */}
      {showSplash && (
        <div className="fixed inset-0 z-[100]">
          <SplashScreen onComplete={handleSplashComplete} />
        </div>
      )}

      {/* A. Drag-drop overlay */}
      {dragOver && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-accent/20 backdrop-blur-sm border-4 border-dashed border-accent pointer-events-none">
          <div className="text-center">
            <Upload className="w-16 h-16 text-accent mx-auto mb-3" />
            <p className="text-2xl font-semibold text-accent">Drop to add</p>
            <p className="text-sm text-foreground/60 mt-1">Images and videos will be indexed</p>
          </div>
        </div>
      )}

      {/* Global ingestion banner now lives in app/layout.tsx (JobBanner)
          so it shows on every route. Don't render a per-page copy. */}

      {!showSplash && (
        <div className="p-8 w-full h-full animate-in fade-in duration-1000">
          <div className="flex justify-between items-center mb-4">
            <h1 className="text-xl font-bold">All Media</h1>
            <div className="flex gap-2">
              <button
                onClick={handleAddFolder}
                disabled={isIngesting}
                className="flex items-center gap-2 px-3 py-2 rounded-lg bg-accent/10 hover:bg-accent/20 text-accent border border-accent/30 transition-colors text-sm font-medium disabled:opacity-50"
              >
                <FolderPlus className="w-4 h-4" />
                Add Folder
              </button>

              <button
                onClick={handleAddMedia}
                disabled={isIngesting}
                className="flex items-center gap-2 px-3 py-2 rounded-lg bg-accent/10 hover:bg-accent/20 text-accent border border-accent/30 transition-colors text-sm font-medium disabled:opacity-50"
              >
                <FileImage className="w-4 h-4" />
                Add Media
              </button>

              {/* B. Sync button with spin animation */}
              <button
                onClick={handleSyncAll}
                disabled={syncing || isIngesting}
                title="Sync All Folders"
                className={[
                  "flex items-center justify-center w-10 h-10 rounded-lg transition-colors border border-panel-border",
                  syncing || isIngesting
                    ? "bg-accent/30 text-accent cursor-not-allowed"
                    : "bg-black/5 dark:bg-white/5 text-foreground hover:bg-black/10 dark:hover:bg-white/10",
                ].join(" ")}
              >
                <RefreshCw className={["w-4 h-4", syncing ? "animate-spin" : ""].join(" ")} />
              </button>
            </div>
          </div>

          {/* Status banner */}
          {statusMsg && (
            <div className="mb-4 px-4 py-2 rounded-lg bg-accent/10 border border-accent/30 text-sm text-accent font-medium animate-in fade-in duration-300">
              {statusMsg}
            </div>
          )}

          <div className="w-full h-[calc(100%-80px)]">
            {media.length > 0 ? (
              <MediaGrid
                items={media}
                hasMore={hasMore}
                onLoadMore={() => fetchMedia(offset)}
                onItemClick={(item) => {
                  const idx = media.findIndex((m) => m.id === item.id);
                  if (idx !== -1) setSelectedIndex(idx);
                }}
              />
            ) : (
              <div className="w-full h-full glass-panel rounded-2xl p-8 flex items-center justify-center">
                <div className="text-center">
                  <div className="w-16 h-16 rounded-full bg-black/5 dark:bg-white/5 flex items-center justify-center mx-auto mb-4">
                    <ImageIcon className="w-8 h-8 text-muted-text" />
                  </div>
                  <h3 className="text-xl font-semibold mb-2">No media found</h3>
                  <p className="text-muted-text max-w-sm mx-auto">
                    Drag and drop images or videos here, or click &ldquo;Add Folder&rdquo; to start indexing your media.
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {selectedIndex !== null && (
        <MediaModal
          item={media[selectedIndex]}
          onClose={() => setSelectedIndex(null)}
          onPrev={() => setSelectedIndex(Math.max(0, selectedIndex - 1))}
          onNext={() => setSelectedIndex(Math.min(media.length - 1, selectedIndex + 1))}
          hasPrev={selectedIndex > 0}
          hasNext={selectedIndex < media.length - 1}
        />
      )}
    </div>
  );
}
