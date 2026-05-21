"use client";

import { useState, useEffect, useCallback } from "react";
import { ConfigResponse, LevelInfo } from "@/lib/types";

const API_BASE = "http://localhost:14793";

export default function SettingsPage() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [selectedLevel, setSelectedLevel] = useState<string>("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [showReindexModal, setShowReindexModal] = useState(false);
  const [reindexMessage, setReindexMessage] = useState<string | null>(null);
  const [isIngesting, setIsIngesting] = useState(false);
  const [reindexInFlight, setReindexInFlight] = useState(false);

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

  useEffect(() => {
    queueMicrotask(() => { void fetchConfig(); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      setToast(`Settings saved. New media will be indexed at ${label} level.`);
      setTimeout(() => setToast(null), 4000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save settings.");
      // Revert
      if (config) setSelectedLevel(config.level);
    } finally {
      setIsSaving(false);
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
      </div>

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
