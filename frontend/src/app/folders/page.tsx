"use client";
import { useState, useEffect, useCallback } from "react";
import { Folder, Plus, Trash2, AlertTriangle } from "lucide-react";

interface FolderInfo {
  path: string;
  normalized_path: string;
  exists: boolean;
  indexed_count: number;
}

interface Toast {
  id: number;
  message: string;
  type: "error" | "info";
}

let toastId = 0;

export default function FoldersPage() {
  const [folders, setFolders] = useState<FolderInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmDelete, setConfirmDelete] = useState<string[] | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = (message: string, type: Toast["type"] = "info") => {
    const id = ++toastId;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 4000);
  };

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("http://127.0.0.1:14793/folders");
      if (res.ok) {
        const data = (await res.json()) as { items: FolderInfo[] };
        setFolders(data.items);
      }
    } catch (e) {
      console.error("fetch folders failed", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    queueMicrotask(() => { void refresh(); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleAddFolder = async () => {
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const folder = await open({ directory: true, multiple: false });
      if (!folder || typeof folder !== "string") return;
      const res = await fetch("http://127.0.0.1:14793/add_folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_path: folder }),
      });
      if (res.status === 409) {
        addToast("Ingest already in progress. Please wait.", "error");
        return;
      }
      if (res.ok) {
        await refresh();
      } else {
        addToast("Failed to add folder.", "error");
      }
    } catch (e) {
      console.error("add folder failed", e);
      addToast("Failed to open folder picker.", "error");
    }
  };

  const toggleSelect = (path: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const handleDelete = async (paths: string[]) => {
    try {
      const res = await fetch("http://127.0.0.1:14793/folders", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths }),
      });
      if (res.status === 409) {
        addToast("Ingest in progress. Wait then retry.", "error");
      } else if (res.ok) {
        setSelected((prev) => {
          const next = new Set(prev);
          paths.forEach((p) => next.delete(p));
          return next;
        });
        await refresh();
      } else {
        addToast("Failed to remove folder(s).", "error");
      }
    } catch (e) {
      console.error("delete folders failed", e);
      addToast("Failed to remove folder(s).", "error");
    }
  };

  const basename = (p: string) => p.split(/[\\/]/).filter(Boolean).pop() ?? p;

  return (
    <div className="h-full w-full p-8 overflow-y-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-xl font-bold">Folders</h1>
          <div className="flex gap-2">
            {selected.size > 0 && (
              <button
                onClick={() => setConfirmDelete([...selected])}
                className="flex items-center gap-2 px-3 py-2 rounded-lg bg-red-600/10 hover:bg-red-600/20 text-red-600 border border-red-600/30 transition-colors text-sm font-medium"
              >
                <Trash2 className="w-4 h-4" />
                Remove Selected ({selected.size})
              </button>
            )}
            <button
              onClick={() => void handleAddFolder()}
              className="flex items-center gap-2 px-3 py-2 rounded-lg bg-accent/10 hover:bg-accent/20 text-accent border border-accent/30 transition-colors text-sm font-medium"
            >
              <Plus className="w-4 h-4" />
              Add Folder
            </button>
          </div>
        </div>

        {/* Content */}
        {loading ? (
          <p className="text-foreground/50">Loading…</p>
        ) : folders.length === 0 ? (
          <div className="text-center py-16 text-foreground/60">
            No folders added yet. Click &ldquo;Add Folder&rdquo; to start indexing.
          </div>
        ) : (
          // Auto-fill grid — matches MediaGrid (see comment there) so
          // folder tiles cap at ~180 px regardless of monitor width.
          <div
            className="grid gap-2.5"
            style={{ gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))" }}
          >
            {folders.map((f) => (
              <div
                key={f.normalized_path}
                className="relative group aspect-square rounded-xl border border-panel-border bg-foreground/[0.02] hover:bg-foreground/[0.05] transition-colors p-2 flex flex-col"
              >
                {/* Top-left checkbox */}
                <div className="absolute top-1.5 left-1.5">
                  <input
                    type="checkbox"
                    checked={selected.has(f.path)}
                    onChange={() => toggleSelect(f.path)}
                    className="w-3.5 h-3.5 accent-accent cursor-pointer"
                  />
                </div>

                {/* Top-right bin */}
                <button
                  onClick={() => setConfirmDelete([f.path])}
                  className="absolute top-1.5 right-1.5 p-1 rounded-md text-red-500 hover:bg-red-500/10 transition-colors"
                  title="Remove folder"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>

                {/* Folder icon + info */}
                <div className="flex-1 flex flex-col items-center justify-center">
                  <Folder className="w-8 h-8 text-accent mb-1" />
                  <div className="font-medium text-xs truncate w-full text-center">
                    {basename(f.path)}
                  </div>
                  <div
                    className="text-[10px] text-foreground/50 truncate w-full text-center mt-0.5"
                    title={f.path}
                  >
                    {f.path}
                  </div>
                  <div className="flex items-center gap-1 mt-1.5 flex-wrap justify-center">
                    <span className="text-[10px] px-1.5 py-0.5 rounded-md bg-accent/10 text-accent font-medium">
                      {f.indexed_count} items
                    </span>
                    {!f.exists && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-md bg-yellow-500/10 text-yellow-600 flex items-center gap-1">
                        <AlertTriangle className="w-2.5 h-2.5" /> Missing
                      </span>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

      {/* Confirm delete modal */}
      {confirmDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
          onClick={() => setConfirmDelete(null)}
        >
          <div
            className="bg-background rounded-2xl border border-panel-border p-6 max-w-md mx-4"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-lg font-semibold">
              Remove{" "}
              {confirmDelete.length === 1
                ? "folder"
                : `${confirmDelete.length} folders`}
              ?
            </h2>
            <p className="text-sm text-foreground/70 mt-2">
              {confirmDelete.length === 1
                ? `"${basename(confirmDelete[0])}" and all its indexed items will be removed from SovLens. Original files on disk are NOT deleted.`
                : `These ${confirmDelete.length} folders and their indexed items will be removed from SovLens. Original files on disk are NOT deleted.`}
            </p>
            <div className="flex justify-end gap-2 mt-5">
              <button
                onClick={() => setConfirmDelete(null)}
                className="px-4 py-2 rounded-lg hover:bg-foreground/5 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  void handleDelete(confirmDelete);
                  setConfirmDelete(null);
                }}
                className="px-4 py-2 rounded-lg bg-red-600 text-white hover:bg-red-700 transition-colors"
              >
                Remove
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Toast notifications */}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 pointer-events-none">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`px-4 py-2 rounded-lg text-sm shadow-lg ${
              t.type === "error"
                ? "bg-red-600 text-white"
                : "bg-foreground/90 text-background"
            }`}
          >
            {t.message}
          </div>
        ))}
      </div>
    </div>
  );
}
