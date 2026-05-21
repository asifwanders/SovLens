"use client";

import { useEffect } from "react";
import Hls from "hls.js";

/**
 * Attaches an HLS source to a <video> element.
 * - On Safari / WKWebView (macOS Tauri): uses native HLS via HTMLVideoElement.src.
 * - On Chromium / WebView2 (Windows Tauri): uses hls.js.
 * - If a fatal hls.js error occurs, falls back to the direct /video?path=… mp4 URL.
 */
export function useHls(
  videoRef: React.RefObject<HTMLVideoElement | null>,
  src: string | null,
  fallbackSrc?: string
): void {
  useEffect(() => {
    if (!videoRef.current || !src) return;
    const video = videoRef.current;

    // Safari / WKWebView supports HLS natively — canPlayType check must come first
    // because Hls.isSupported() returns false on those environments.
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = src;
      return;
    }

    if (Hls.isSupported()) {
      const hls = new Hls({ enableWorker: true });
      hls.loadSource(src);
      hls.attachMedia(video);

      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (data.fatal) {
          console.warn("[hls.js] Fatal error, falling back to direct mp4:", data);
          hls.destroy();
          if (fallbackSrc) {
            video.src = fallbackSrc;
          }
        }
      });

      return () => hls.destroy();
    }

    // Last-resort fallback: browser doesn't support MSE or native HLS
    video.src = fallbackSrc ?? src;
  }, [src, fallbackSrc, videoRef]);
}
