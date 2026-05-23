"use client";

import { useEffect, useRef } from "react";
import { Copy, Check, FolderOpen } from "lucide-react";
import { MediaItem } from "@/lib/types";
import { formatTimestamp } from "@/lib/format";
import { useCopyFeedback } from "@/lib/useCopyFeedback";

interface MediaGridProps {
  items: MediaItem[];
  onLoadMore: () => void;
  hasMore: boolean;
  onItemClick: (item: MediaItem) => void;
}

interface MediaTileProps {
  item: MediaItem;
  onItemClick: (item: MediaItem) => void;
}

function MediaTile({ item, onItemClick }: MediaTileProps) {
  const { copied, trigger } = useCopyFeedback();

  const srcUrl = `http://127.0.0.1:14793/image?path=${encodeURIComponent(item.thumbnail || item.path)}`;
  const showTimestamp = (item.type === "video" || item.type === "audio_segment") && typeof item.timestamp === "number" && item.timestamp > 0;
  const showScore = typeof item.score === "number";
  const showSnippet = !!item.text_snippet;

  const handleCopy = async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("copy_media_to_clipboard", { path: item.path });
      trigger();
    } catch (e) {
      console.error("copy_media_to_clipboard failed", e);
      // Last-resort: fall back to copying path as text
      try {
        const { writeText } = await import("@tauri-apps/plugin-clipboard-manager");
        await writeText(item.path);
        trigger();
      } catch (e2) {
        try {
          await navigator.clipboard.writeText(item.path);
          trigger();
        } catch (e3) { console.error("clipboard fallback failed", e2, e3); }
      }
    }
  };

  const handleOpenExplorer = async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("reveal_in_explorer", { path: item.path });
    } catch (e) {
      console.error("reveal_in_explorer failed", e);
    }
  };

  return (
    <div
      key={item.id}
      onClick={() => onItemClick(item)}
      className="relative group aspect-square rounded-xl overflow-hidden bg-black/5 dark:bg-white/5 border border-panel-border cursor-pointer shadow-sm hover:shadow-md transition-shadow"
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={srcUrl}
        alt="Media thumbnail"
        className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
      />

      {item.type === "video" && (
        <div className="absolute top-2 right-2 bg-black/50 text-white text-xs px-2 py-1 rounded-md backdrop-blur-md">
          Video
        </div>
      )}

      {item.type === "audio_segment" && (
        <div className="absolute top-2 right-2 bg-blue-600/70 text-white text-xs px-2 py-1 rounded-md backdrop-blur-md">
          Audio
        </div>
      )}

      {showSnippet && (
        // Hover-only so OCR / transcript text doesn't permanently
        // disfigure the thumbnail. tooltip via `title` still gives
        // keyboard / mobile users access.
        <div
          className="absolute bottom-0 left-0 right-0 bg-black/65 backdrop-blur-sm px-2 py-1 opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none"
          title={item.text_snippet ?? undefined}
        >
          <p className={`text-white text-[10px] leading-tight line-clamp-2${item.type === "audio_segment" ? " italic" : ""}`}>
            {(item.text_snippet as string).slice(0, 80)}
          </p>
        </div>
      )}

      {showScore && (
        <div className="absolute top-2 left-2 bg-black/60 text-white text-xs px-2 py-0.5 rounded-md backdrop-blur-md font-medium">
          {Math.round((item.score as number) * 100)}%
        </div>
      )}

      {showTimestamp && (
        <div className="absolute bottom-2 right-2 bg-black/60 text-white text-xs px-2 py-0.5 rounded-md backdrop-blur-md font-mono">
          {formatTimestamp(item.timestamp as number)}
        </div>
      )}

      {/* Hover Actions */}
      <div className="absolute inset-0 bg-black/0 group-hover:bg-black/20 transition-colors duration-300 flex items-center justify-center opacity-0 group-hover:opacity-100">
        <div className="flex gap-2" onClick={(e) => e.stopPropagation()}>
          <button
            className={`p-2 backdrop-blur-md rounded-lg transition-all duration-200 ease-out ${
              copied
                ? "bg-accent/20 text-accent scale-110"
                : "bg-white/20 hover:bg-white/40 text-white scale-100"
            }`}
            title="Copy Media"
            onClick={(e) => { e.stopPropagation(); handleCopy(); }}
          >
            {copied ? <Check className="w-5 h-5" /> : <Copy className="w-5 h-5" />}
          </button>
          <button
            className="p-2 bg-white/20 hover:bg-white/40 backdrop-blur-md rounded-lg text-white transition-colors"
            title="Show in Files"
            onClick={(e) => { e.stopPropagation(); handleOpenExplorer(); }}
          >
            <FolderOpen className="w-5 h-5" />
          </button>
        </div>
      </div>
    </div>
  );
}

export default function MediaGrid({ items, onLoadMore, hasMore, onItemClick }: MediaGridProps) {
  const observerTarget = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && hasMore) {
          onLoadMore();
        }
      },
      { threshold: 0.1 }
    );

    if (observerTarget.current) {
      observer.observe(observerTarget.current);
    }

    return () => observer.disconnect();
  }, [hasMore, onLoadMore]);

  return (
    <div className="w-full h-full pb-20">
      {/*
        Auto-fill grid caps tile size regardless of monitor width.
        Tailwind's fixed breakpoint columns (grid-cols-3...9) were
        producing huge tiles on 27" 1440p+ at fullscreen because the
        column count plateaued at 9 while the available width kept
        growing — each cell stretched to ~280-320 px. minmax(180px, 1fr)
        keeps tiles between 180 px (lower bound) and 1fr (only one row
        at narrow widths) and adds more columns as space allows.
      */}
      <div
        className="grid gap-2.5"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))" }}
      >
        {items.map((item) => (
          <MediaTile key={item.id} item={item} onItemClick={onItemClick} />
        ))}
      </div>

      {/* Infinite Scroll Trigger */}
      {hasMore && (
        <div ref={observerTarget} className="w-full h-20 flex items-center justify-center mt-8">
          <div className="w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin"></div>
        </div>
      )}
    </div>
  );
}
