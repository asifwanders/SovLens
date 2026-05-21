"use client";

import { useRef, useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, ChevronLeft, ChevronRight, Copy, Check, FolderOpen } from "lucide-react";
import { MediaItem } from "@/lib/types";
import { createPortal } from "react-dom";
import { useHls } from "@/lib/useHls";
import { getRevealLabel } from "@/lib/platform";
import { useCopyFeedback } from "@/lib/useCopyFeedback";

interface MediaModalProps {
  item: MediaItem | null;
  onClose: () => void;
  onNext: () => void;
  onPrev: () => void;
  hasNext: boolean;
  hasPrev: boolean;
}

export default function MediaModal({ item, onClose, onNext, onPrev, hasNext, hasPrev }: MediaModalProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [isBuffering, setIsBuffering] = useState(false);
  const [revealLabel, setRevealLabel] = useState("Show in Files");
  const { copied, trigger } = useCopyFeedback();

  useEffect(() => {
    getRevealLabel().then(setRevealLabel);
  }, []);

  const isVideo = item !== null && (item.type === "video" || item.type === "audio_segment");

  // Build HLS playlist URL for video/audio_segment items
  const hlsUrl = isVideo && item !== null
    ? `http://127.0.0.1:14793/hls/${encodeURIComponent(item.video_id ?? item.id)}/playlist.m3u8?path=${encodeURIComponent(item.path)}`
    : null;

  // Direct mp4 fallback used by hls.js on fatal error and by the last-resort path
  const fallbackUrl = isVideo && item !== null
    ? `http://127.0.0.1:14793/video?path=${encodeURIComponent(item.path)}`
    : undefined;

  // Attach HLS source (no-op for images; hook guards on null src)
  useHls(videoRef, hlsUrl, fallbackUrl);

  // Seek to timestamp once metadata is available. Setting currentTime before
  // loadedmetadata is unreliable because the browser may not know the duration yet.
  // audio_segment items point to the source video path and use the same seek logic.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !item || (item.type !== "video" && item.type !== "audio_segment")) return;
    const ts = item.timestamp ?? 0;
    if (ts <= 0) return;

    const handler = () => {
      video.currentTime = ts;
    };
    video.addEventListener("loadedmetadata", handler);
    return () => video.removeEventListener("loadedmetadata", handler);
  }, [item]);

  if (!item) return null;

  const srcUrl = isVideo
    ? undefined // src is controlled by useHls
    : `http://127.0.0.1:14793/image?path=${encodeURIComponent(item.path)}`;

  const handleCopy = async () => {
    if (!item) return;
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
    if (!item) return;
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("reveal_in_explorer", { path: item.path });
    } catch (e) {
      console.error("reveal_in_explorer failed", e);
    }
  };

  const modalContent = (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-md"
        onClick={onClose}
      >
        {hasPrev && (
          <button
            onClick={(e) => { e.stopPropagation(); onPrev(); }}
            className="absolute left-6 top-1/2 -translate-y-1/2 text-white p-4 bg-black/40 hover:bg-black/60 rounded-full transition-colors z-50"
          >
            <ChevronLeft className="w-8 h-8" />
          </button>
        )}

        {hasNext && (
          <button
            onClick={(e) => { e.stopPropagation(); onNext(); }}
            className="absolute right-6 top-1/2 -translate-y-1/2 text-white p-4 bg-black/40 hover:bg-black/60 rounded-full transition-colors z-50"
          >
            <ChevronRight className="w-8 h-8" />
          </button>
        )}

        <motion.div
          initial={{ scale: 0.95, y: 10 }}
          animate={{ scale: 1, y: 0 }}
          exit={{ scale: 0.95, y: 10 }}
          transition={{ type: "spring", damping: 25, stiffness: 300 }}
          className="relative max-w-5xl w-full max-h-[90vh] flex flex-col rounded-2xl overflow-hidden bg-black/40 backdrop-blur-xl border border-white/10 shadow-2xl"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="flex items-center justify-between px-5 py-3 border-b border-white/10 shrink-0">
            <span className="text-white/80 text-base font-medium truncate max-w-[80%]">{item.path.split(/[\\/]/).pop()}</span>
            <button
              onClick={onClose}
              className="text-white/70 hover:text-white p-1.5 hover:bg-white/10 rounded-full transition-colors ml-2 shrink-0"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Scrollable content area */}
          <div className="flex-1 overflow-auto flex flex-col items-center justify-center p-4 min-h-0">
            {item.type === "image" ? (
              <div className="flex flex-col items-center gap-3 w-full">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={srcUrl}
                  alt="Enlarged media"
                  className="max-h-full max-w-full object-contain rounded-xl shadow-2xl border border-white/20"
                />
                {item.text_snippet && (
                  <div className="w-full bg-foreground/5 border border-panel-border rounded-lg px-4 py-3">
                    <div className="text-xs uppercase tracking-wider text-foreground/50 mb-1">Detected text</div>
                    <p className="text-sm leading-snug">{item.text_snippet}</p>
                  </div>
                )}
              </div>
            ) : (
              <div className="relative w-full flex flex-col gap-3">
                <div className="relative w-full">
                  <video
                    key={item.id}
                    ref={videoRef}
                    controls
                    autoPlay
                    playsInline
                    onWaiting={() => setIsBuffering(true)}
                    onPlaying={() => setIsBuffering(false)}
                    onCanPlay={() => setIsBuffering(false)}
                    className="max-h-[60vh] w-full mx-auto rounded-xl bg-black shadow-2xl border border-white/20"
                  />
                  {/* Buffering spinner overlay while HLS first segment loads */}
                  {isBuffering && (
                    <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-black/40 backdrop-blur-sm">
                      <div className="w-12 h-12 rounded-full border-4 border-white/20 border-t-white animate-spin" />
                    </div>
                  )}
                </div>
                {item.text_snippet && (
                  <div className="w-full bg-foreground/5 border border-panel-border rounded-lg px-4 py-3">
                    <div className="text-xs uppercase tracking-wider text-foreground/50 mb-1">
                      {item.type === "audio_segment" ? "Transcript" : "Detected text"}
                    </div>
                    <p className={`text-sm leading-snug${item.type === "audio_segment" ? " italic" : ""}`}>{item.text_snippet}</p>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Action button bar — always visible at bottom */}
          <div className="shrink-0 flex items-center justify-center gap-4 border-t border-white/10 px-6 py-3 bg-black/20">
            <button
              onClick={handleCopy}
              className={`flex items-center gap-2 transition-all duration-200 ease-out rounded-md px-2 py-1 ${
                copied
                  ? "text-accent bg-accent/20 scale-110"
                  : "text-white hover:text-accent scale-100"
              }`}
            >
              {copied ? <Check className="w-5 h-5" /> : <Copy className="w-5 h-5" />}
              <span className="text-sm font-medium">{copied ? "Copied!" : "Copy Media"}</span>
            </button>
            <div className="w-px h-5 bg-white/30"></div>
            <button onClick={handleOpenExplorer} className="flex items-center gap-2 text-white hover:text-accent transition-colors">
              <FolderOpen className="w-5 h-5" />
              <span className="text-sm font-medium">{revealLabel}</span>
            </button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );

  if (typeof document === "undefined") return null;
  return createPortal(modalContent, document.body);
}
