"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-shell";
import { getVersion } from "@tauri-apps/api/app";
import { check } from "@tauri-apps/plugin-updater";
import { ConfigResponse, LevelInfo } from "@/lib/types";

// ---------------------------------------------------------------------------
// AI model list
// ---------------------------------------------------------------------------
interface ModelMeta {
  key: string;
  label: string;
  size: string;
  required: boolean;
}

const MODELS: ModelMeta[] = [
  { key: "clip",    label: "Visual search (CLIP)",          size: "~890 MB", required: true  },
  { key: "whisper", label: "Voice transcription (Whisper)", size: "~150 MB", required: false },
  { key: "ocr",     label: "Text in images (EasyOCR)",      size: "~150 MB", required: false },
  { key: "yolo",    label: "Object detection (YOLOv8)",     size: "~6 MB",   required: false },
];

interface ModelStatus {
  downloaded: boolean;
  loaded: boolean;
  available?: boolean;
}
interface ModelsStatusResponse {
  clip:    ModelStatus;
  whisper: ModelStatus;
  yolo:    ModelStatus;
  ocr:     ModelStatus;
}
interface ModelProgress {
  bytes_now: number;
  bytes_total: number;
  percent: number;
}
interface ModelsProgressResponse {
  clip:    ModelProgress;
  whisper: ModelProgress;
  yolo:    ModelProgress;
  ocr:     ModelProgress;
}

const API_BASE = "http://127.0.0.1:14793";

// Mirror of backend/config.py LEVEL_PARAMS — which models each level needs.
// Used to auto-trigger downloads when the user picks a higher level.
const LEVEL_REQUIRED_MODELS: Record<string, string[]> = {
  low: ["clip"],
  medium: ["clip"],
  high: ["clip", "whisper", "ocr"],
  extreme: ["clip", "whisper", "ocr", "yolo"],
};

export default function SettingsPage() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [selectedLevel, setSelectedLevel] = useState<string>("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [showReindexModal, setShowReindexModal] = useState(false);
  const [reindexMessage, setReindexMessage] = useState<string | null>(null);
  const [showWipeModal, setShowWipeModal] = useState(false);
  const [wipeMessage, setWipeMessage] = useState<string | null>(null);
  const [wipeInFlight, setWipeInFlight] = useState(false);
  const [isIngesting, setIsIngesting] = useState(false);
  const [reindexInFlight, setReindexInFlight] = useState(false);

  // AI model status. `warming` is a Set so multiple models can show
  // a "Downloading…" state in parallel (e.g. switching to Extreme).
  const [modelStatus, setModelStatus] = useState<ModelsStatusResponse | null>(null);
  const [modelProgress, setModelProgress] = useState<ModelsProgressResponse | null>(null);
  const [warming, setWarming] = useState<Set<string>>(new Set());
  const warmingRef = useRef<Set<string>>(new Set());

  // Updater state
  const [appVersion, setAppVersion] = useState<string>("...");
  const [updateAvailable, setUpdateAvailable] = useState<{ version: string; body?: string | null } | null>(null);
  const [updateChecking, setUpdateChecking] = useState(false);
  const [updateInstalling, setUpdateInstalling] = useState(false);
  const [updateMessage, setUpdateMessage] = useState<string | null>(null);
  const [lastChecked, setLastChecked] = useState<string | null>(null);

  const fetchConfig = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/config`);
      if (!res.ok) throw new Error(`Backend error: ${res.status}`);
      const data: ConfigResponse = await res.json();
      setConfig(data);
      setSelectedLevel(data.level);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load settings.");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const fetchModelStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/models/status`);
      if (!res.ok) return;
      const data = (await res.json()) as ModelsStatusResponse;
      setModelStatus(data);
      // Drop warming flags for any model that has finished downloading.
      if (warmingRef.current.size > 0) {
        const next = new Set(warmingRef.current);
        for (const key of warmingRef.current) {
          const k = key as keyof ModelsStatusResponse;
          if (data[k]?.downloaded) next.delete(key);
        }
        if (next.size !== warmingRef.current.size) {
          warmingRef.current = next;
          setWarming(next);
        }
      }
    } catch {
      // backend not ready yet — ignore
    }
  }, []);

  const checkForUpdates = useCallback(async (silent = false) => {
    setUpdateChecking(true);
    if (!silent) setUpdateMessage(null);
    try {
      const update = await check();
      setLastChecked(new Date().toLocaleTimeString());
      if (update?.available) {
        setUpdateAvailable({ version: update.version, body: update.body });
        if (silent) setToast(`v${update.version} is available. See Updates section.`);
      } else {
        setUpdateAvailable(null);
        if (!silent) setUpdateMessage("You are on the latest version.");
      }
    } catch (e) {
      if (!silent) setUpdateMessage(`Update check failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setUpdateChecking(false);
    }
  }, []);

  const handleInstallUpdate = async () => {
    if (!updateAvailable) return;
    setUpdateInstalling(true);
    setUpdateMessage("Downloading and installing update…");
    try {
      const update = await check();
      if (update?.available) {
        await update.downloadAndInstall();
        // Tauri will relaunch the app automatically after install
      }
    } catch (e) {
      setUpdateMessage(`Install failed: ${e instanceof Error ? e.message : String(e)}`);
      setUpdateInstalling(false);
    }
  };

  useEffect(() => {
    queueMicrotask(() => { void fetchConfig(); });
    queueMicrotask(() => { void fetchModelStatus(); });
    getVersion().then(setAppVersion).catch(() => setAppVersion("unknown"));
    // Auto-check silently on mount — defer past render commit so the
    // setUpdateChecking(true) inside doesn't trip react-hooks/set-state-in-effect.
    queueMicrotask(() => { void checkForUpdates(true); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Always poll /models/status while the page is mounted. Cheap call, no
  // model I/O. Catches downloads triggered by ingestion / other paths too.
  useEffect(() => {
    const interval = setInterval(() => { void fetchModelStatus(); }, 3000);
    return () => clearInterval(interval);
  }, [fetchModelStatus]);

  // Poll /models/progress every 1.5s — used for download progress bars.
  // Endpoint just walks the HF cache dir; cheap.
  useEffect(() => {
    const tick = async () => {
      try {
        const res = await fetch(`${API_BASE}/models/progress`);
        if (!res.ok) return;
        const data = (await res.json()) as ModelsProgressResponse;
        setModelProgress(data);
      } catch {
        /* backend not ready */
      }
    };
    void tick();
    const interval = setInterval(() => { void tick(); }, 1500);
    return () => clearInterval(interval);
  }, []);

  const warmup = useCallback(async (key: string) => {
    if (warmingRef.current.has(key)) return;
    const next = new Set(warmingRef.current);
    next.add(key);
    warmingRef.current = next;
    setWarming(next);
    try {
      await fetch(`${API_BASE}/models/warmup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: key }),
      });
    } catch {
      // warmup endpoint may not respond immediately — polling will detect completion
    }
  }, []);

  // Poll /status while a re-index is in flight
  useEffect(() => {
    if (!reindexInFlight) return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/status`);
        if (!res.ok) return;
        const data = await res.json();
        setIsIngesting(data.is_ingesting as boolean);
        if (!(data.is_ingesting as boolean)) {
          setReindexInFlight(false);
        }
      } catch {
        // backend may be temporarily busy; keep polling
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [reindexInFlight]);

  const handleSelect = async (key: string) => {
    if (isSaving || key === selectedLevel) return;
    setSelectedLevel(key);
    setIsSaving(true);
    try {
      const res = await fetch(`${API_BASE}/config/level`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: key }),
      });
      if (!res.ok) throw new Error(`Save failed: ${res.status}`);
      const level = config?.available_levels.find((l: LevelInfo) => l.key === key);
      const label = level?.label ?? key;

      // Auto-trigger background download for any newly required models
      // that aren't on disk yet. The UI will show "Downloading…" per model
      // via the polling loop above.
      const required = LEVEL_REQUIRED_MODELS[key] ?? [];
      const toDownload = required.filter((m) => {
        const k = m as keyof ModelsStatusResponse;
        return !modelStatus?.[k]?.downloaded;
      });
      for (const m of toDownload) void warmup(m);

      const extra = toDownload.length > 0
        ? ` Downloading ${toDownload.length} model${toDownload.length === 1 ? "" : "s"}…`
        : "";
      setToast(`Settings saved. New media will be indexed at ${label} level.${extra}`);
      setTimeout(() => setToast(null), 5000);
      void fetchModelStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save settings.");
      // Revert
      if (config) setSelectedLevel(config.level);
    } finally {
      setIsSaving(false);
    }
  };

  const handleOpenLogs = async () => {
    try {
      await invoke("open_logs_folder");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not open logs folder.");
    }
  };

  const handleReportBug = async () => {
    try {
      const logs = await invoke<string>("read_recent_logs", { maxBytes: 4000 });
      const version = await getVersion().catch(() => "unknown");
      const body = `## Description\n(Describe what you were doing)\n\n## Expected\n...\n\n## Actual\n...\n\n## Environment\n- OS: macOS / Windows\n- SovLens version: ${version}\n- Hardware: (e.g., M2 Pro, RTX 4060)\n\n## Recent logs\n\`\`\`\n${logs || "(no logs found)"}\n\`\`\``;
      const url = `https://github.com/asifwanders/SovLens/issues/new?title=Bug%20report&body=${encodeURIComponent(body)}`;
      await open(url);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not open bug report.");
    }
  };

  const handleReindex = () => {
    setShowReindexModal(true);
  };

  const confirmReindex = async () => {
    setShowReindexModal(false);
    try {
      const res = await fetch(`${API_BASE}/reindex_all`, { method: "POST" });
      if (!res.ok) {
        setReindexMessage(`Re-index failed: ${res.status}`);
        setTimeout(() => setReindexMessage(null), 6000);
      } else {
        setIsIngesting(true);
        setReindexInFlight(true);
        setReindexMessage(
          "Re-indexing started. This will take a while. Check the home page indicator."
        );
      }
    } catch {
      setReindexMessage("Could not reach backend — is SovLens running?");
      setTimeout(() => setReindexMessage(null), 6000);
    }
  };

  const confirmWipe = async () => {
    setShowWipeModal(false);
    setWipeInFlight(true);
    setWipeMessage("Wiping data and stopping backend… Please quit and reopen SovLens.");
    try {
      await fetch(`${API_BASE}/admin/wipe_data`, { method: "POST" });
    } catch {
      // Backend will exit immediately after responding — the fetch may
      // surface a network error which is fine; treat it as success.
    }
  };

  return (
    <div className="h-full w-full p-8 overflow-y-auto">
      <div className="max-w-2xl mx-auto">
        <h1 className="text-xl font-bold mb-6">Settings</h1>

        {/* Toast */}
        {toast && (
          <div className="mb-6 px-4 py-3 rounded-xl bg-accent/15 border border-accent/30 text-accent text-sm">
            {toast}
          </div>
        )}

        {/* Error banner */}
        {error && (
          <div className="mb-6 px-4 py-3 rounded-xl bg-red-500/10 border border-red-500/30 text-red-500 text-sm flex items-center justify-between">
            <span>{error}</span>
            <button
              onClick={fetchConfig}
              className="ml-4 underline hover:no-underline text-xs"
            >
              Retry
            </button>
          </div>
        )}

        {/* Analysis Level section */}
        <section>
          <h2 className="text-lg font-medium mb-1">Analysis Level</h2>
          <p className="text-sm text-foreground/70 mb-4">
            Choose how thoroughly SovLens analyzes your media. Higher levels find more (like a tiny
            object in a video) but take longer and use more disk/GPU.
          </p>

          {isLoading ? (
            <div className="space-y-3">
              {[1, 2, 3, 4].map((i) => (
                <div
                  key={i}
                  className="block rounded-xl border border-panel-border p-4 animate-pulse"
                >
                  <div className="h-4 bg-foreground/10 rounded w-1/4 mb-2" />
                  <div className="h-3 bg-foreground/10 rounded w-3/4 mb-1" />
                  <div className="h-3 bg-foreground/10 rounded w-1/2" />
                </div>
              ))}
            </div>
          ) : config ? (
            <div className="space-y-3">
              {config.available_levels.map((level: LevelInfo) => (
                <label
                  key={level.key}
                  className={[
                    "block rounded-xl border p-4 cursor-pointer transition-colors",
                    "hover:bg-black/5 dark:hover:bg-white/5",
                    isSaving ? "opacity-60 pointer-events-none" : "",
                    selectedLevel === level.key
                      ? "border-accent bg-accent/10 dark:bg-accent/15"
                      : "border-panel-border",
                  ].join(" ")}
                >
                  <div className="flex items-start gap-3">
                    <input
                      type="radio"
                      name="level"
                      value={level.key}
                      checked={selectedLevel === level.key}
                      onChange={() => handleSelect(level.key)}
                      disabled={isSaving}
                      className="mt-1 accent-accent"
                    />
                    <div className="flex-1">
                      <div className="font-medium">{level.label}</div>
                      <p className="text-sm text-foreground/70 mt-1">{level.description}</p>
                      <div className="text-xs text-foreground/50 mt-2 font-mono">
                        {level.speed_estimate}
                      </div>
                    </div>
                  </div>
                </label>
              ))}
            </div>
          ) : null}
        </section>

        {/* AI models section */}
        <div className="mt-8 pt-6 border-t border-panel-border">
          <h2 className="text-lg font-medium mb-3">AI models</h2>
          <p className="text-sm text-foreground/70 mb-4">
            Models download automatically when first needed. You can pre-download them now.
          </p>
          <div className="space-y-2">
            {MODELS.map((m) => {
              const status = modelStatus ? modelStatus[m.key as keyof ModelsStatusResponse] : null;
              const isDownloaded = status?.downloaded ?? false;
              const isWarming = warming.has(m.key);
              return (
                <div
                  key={m.key}
                  className="flex items-center justify-between rounded-lg border border-panel-border px-4 py-3"
                >
                  <div>
                    <div className="font-medium text-sm">{m.label}</div>
                    <div className="text-xs text-foreground/50">
                      {m.size} &mdash; {m.required ? "Required" : "Optional"}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {isDownloaded ? (
                      <span className="text-xs text-accent flex items-center gap-1">
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                        </svg>
                        Downloaded
                      </span>
                    ) : isWarming ? (
                      (() => {
                        const prog = modelProgress?.[m.key as keyof ModelsProgressResponse];
                        const pct = prog?.percent ?? 0;
                        const mb = (b: number) => (b / (1024 * 1024)).toFixed(0);
                        const sizeStr = prog && prog.bytes_total > 0
                          ? `${mb(prog.bytes_now)} / ${mb(prog.bytes_total)} MB`
                          : "Downloading…";
                        return (
                          <div className="flex items-center gap-2">
                            <span className="text-xs text-foreground/60 tabular-nums">{sizeStr}</span>
                            <div className="w-24 h-1 bg-foreground/10 rounded overflow-hidden">
                              <div
                                className="h-full bg-accent transition-[width] duration-500"
                                style={{ width: `${pct}%` }}
                              />
                            </div>
                            <span className="text-xs text-foreground/60 tabular-nums w-8 text-right">{pct}%</span>
                          </div>
                        );
                      })()
                    ) : (
                      <button
                        onClick={() => warmup(m.key)}
                        disabled={warming.has(m.key)}
                        className="text-xs px-2 py-1 rounded border border-accent/30 bg-accent/10 hover:bg-accent/20 text-accent disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                      >
                        Download
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Updates section */}
        <div className="mt-8 pt-6 border-t border-panel-border">
          <h2 className="text-lg font-medium mb-1">Updates</h2>
          <p className="text-sm text-foreground/70 mb-3">
            Current version:{" "}
            <span className="font-mono text-foreground/90">v{appVersion}</span>
            {lastChecked && (
              <span className="ml-3 text-foreground/40">Last checked: {lastChecked}</span>
            )}
          </p>

          {updateAvailable ? (
            <div className="mb-3 px-4 py-3 rounded-xl bg-accent/10 border border-accent/30 text-sm">
              <p className="font-medium text-accent mb-1">
                v{updateAvailable.version} is available — restart to install.
              </p>
              {updateAvailable.body && (
                <p className="text-foreground/70 text-xs mt-1 whitespace-pre-wrap line-clamp-4">
                  {updateAvailable.body}
                </p>
              )}
              {updateMessage && (
                <p className="text-foreground/60 text-xs mt-2">{updateMessage}</p>
              )}
              <button
                onClick={handleInstallUpdate}
                disabled={updateInstalling}
                className="mt-3 px-4 py-2 rounded-lg bg-accent text-white hover:bg-accent/90 transition-colors text-sm disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {updateInstalling ? "Installing…" : "Download & Install"}
              </button>
            </div>
          ) : (
            updateMessage && (
              <p className="text-sm text-foreground/60 mb-3">{updateMessage}</p>
            )
          )}

          <button
            onClick={() => checkForUpdates(false)}
            disabled={updateChecking || updateInstalling}
            className="px-4 py-2 rounded-lg border transition-colors text-sm bg-accent/10 hover:bg-accent/20 text-accent border-accent/30 disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {updateChecking ? "Checking…" : "Check for Updates"}
          </button>
        </div>

        {/* Help & diagnostics section */}
        <div className="mt-8 pt-6 border-t border-panel-border">
          <h2 className="text-lg font-medium mb-2">Help &amp; diagnostics</h2>
          <p className="text-sm text-foreground/70 mb-3">
            Crash logs are saved locally to your app data folder. Nothing is sent automatically.
            To report a bug, click below — we&apos;ll open a pre-filled GitHub issue you can review and submit.
          </p>
          <div className="flex gap-2">
            <button
              onClick={handleOpenLogs}
              className="px-4 py-2 rounded-lg border transition-colors text-sm bg-accent/10 hover:bg-accent/20 text-accent border-accent/30"
            >
              Open Logs Folder
            </button>
            <button
              onClick={handleReportBug}
              className="px-4 py-2 rounded-lg border transition-colors text-sm bg-accent/10 hover:bg-accent/20 text-accent border-accent/30"
            >
              Report Bug
            </button>
          </div>
        </div>

        {/* Re-index section */}
        {!isLoading && (
          <div className="mt-8 pt-6 border-t border-panel-border">
            <h2 className="text-lg font-medium mb-2">Re-index existing media</h2>
            <p className="text-sm text-foreground/70 mb-3">
              Changing levels only affects new media. To re-process your existing library at the new
              level, re-index it here. This can take hours for large libraries.
            </p>
            {reindexMessage && (
              <p className="text-sm text-foreground/70 mb-3 px-3 py-2 rounded-lg bg-foreground/5 border border-panel-border">
                {reindexMessage}
              </p>
            )}
            <button
              className={[
                "px-4 py-2 rounded-lg border transition-colors text-sm",
                isIngesting
                  ? "bg-foreground/5 text-foreground/40 border-panel-border cursor-not-allowed"
                  : "bg-accent/10 hover:bg-accent/20 text-accent border-accent/30",
              ].join(" ")}
              onClick={isIngesting ? undefined : handleReindex}
              disabled={isIngesting}
            >
              {isIngesting ? "Re-indexing…" : "Re-Index All"}
            </button>
          </div>
        )}
        {/* Danger zone */}
        <div className="mt-8 pt-6 border-t border-red-500/30">
          <h2 className="text-lg font-medium mb-2 text-red-500">Danger zone</h2>
          <p className="text-sm text-foreground/70 mb-3">
            Permanently delete all SovLens data: search index, logs, transcoded video cache,
            object-detection crops, folder list, and progress. AI model weights in your shared
            cache (HuggingFace, Whisper, EasyOCR) are not touched. The app will quit; reopen
            it for a fresh start. This cannot be undone.
          </p>
          {wipeMessage && (
            <p className="text-sm text-red-500/90 mb-3 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/30">
              {wipeMessage}
            </p>
          )}
          <button
            onClick={() => setShowWipeModal(true)}
            disabled={wipeInFlight}
            className="px-4 py-2 rounded-lg border transition-colors text-sm bg-red-500/10 hover:bg-red-500/20 text-red-500 border-red-500/30 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {wipeInFlight ? "Wiping…" : "Reset All Data"}
          </button>
        </div>
      </div>

      {/* Reset-data confirmation modal */}
      {showWipeModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
          <div className="glass-panel rounded-2xl p-6 max-w-sm w-full mx-4 shadow-xl">
            <h3 className="text-lg font-semibold mb-2 text-red-500">Delete all SovLens data?</h3>
            <p className="text-sm text-foreground/70 mb-6">
              This deletes the search index, logs, and cache. You&apos;ll need to re-scan your
              folders next time you open SovLens. This cannot be undone.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setShowWipeModal(false)}
                className="px-4 py-2 rounded-lg border border-panel-border hover:bg-foreground/5 transition-colors text-sm"
              >
                Cancel
              </button>
              <button
                onClick={confirmWipe}
                className="px-4 py-2 rounded-lg bg-red-500 text-white hover:bg-red-500/90 transition-colors text-sm"
              >
                Delete everything
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Re-index confirmation modal */}
      {showReindexModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
          <div className="glass-panel rounded-2xl p-6 max-w-sm w-full mx-4 shadow-xl">
            <h3 className="text-lg font-semibold mb-2">Are you sure?</h3>
            <p className="text-sm text-foreground/70 mb-6">
              Re-indexing your entire library can take hours. Your library will still be accessible
              during the process.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setShowReindexModal(false)}
                className="px-4 py-2 rounded-lg border border-panel-border hover:bg-foreground/5 transition-colors text-sm"
              >
                Cancel
              </button>
              <button
                onClick={confirmReindex}
                className="px-4 py-2 rounded-lg bg-accent text-white hover:bg-accent/90 transition-colors text-sm"
              >
                Re-Index All
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
