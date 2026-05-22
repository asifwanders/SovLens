"use client";

import { motion, AnimatePresence } from "framer-motion";
import { useState, useEffect, useRef } from "react";
import { createPortal } from "react-dom";

const API = "http://127.0.0.1:14793";
const POLL_MS = 1500;
const MIN_VISIBLE_MS = 800; // never flash too fast
// Backend startup grace. The bundled PyInstaller backend is now onedir
// (not onefile), so there is no per-launch %TEMP% extract — the bootloader
// just dlopens DLLs from its own _internal/ folder. Cold launch is ~3-5 s
// on mac, ~5-10 s on Windows (Defender may still scan-on-first-read each
// DLL once). 60 s is generous; we used to need 180 s only because onefile
// had to expand a 1 GB blob before the first byte of code ran.
const BACKEND_GRACE_MS = 60_000;

type ClipStatus = {
  name: string;
  loaded: boolean;
  downloaded: boolean;
};
type ClipProgress = {
  bytes_now: number;
  bytes_total: number;
  percent: number;
};

export default function SplashScreen({ onComplete }: { onComplete: () => void }) {
  const [isVisible, setIsVisible] = useState(true);
  const [mounted, setMounted] = useState(false);
  const [status, setStatus] = useState<"booting" | "downloading" | "ready" | "error">("booting");
  const [statusMsg, setStatusMsg] = useState<string>("Starting backend…");
  const [clipProg, setClipProg] = useState<ClipProgress | null>(null);
  // Lazy-init in effect to keep render pure (Date.now() is impure).
  const mountedAt = useRef<number>(0);
  const warmupFired = useRef(false);

  useEffect(() => {
    mountedAt.current = Date.now();
    // Defer past render commit to satisfy react-hooks/set-state-in-effect.
    queueMicrotask(() => setMounted(true));
    let cancelled = false;

    const dismiss = () => {
      const elapsed = Date.now() - mountedAt.current;
      const wait = Math.max(0, MIN_VISIBLE_MS - elapsed);
      setTimeout(() => {
        if (cancelled) return;
        setIsVisible(false);
        setTimeout(onComplete, 800);
      }, wait);
    };

    const poll = async () => {
      if (cancelled) return;
      try {
        const res = await fetch(`${API}/models/status`, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as { clip: ClipStatus };
        const clip = data.clip;

        if (clip.downloaded) {
          setStatus("ready");
          setStatusMsg("Ready");
          dismiss();
          return;
        }

        // Backend up, model not yet downloaded — fire warmup once.
        if (!warmupFired.current) {
          warmupFired.current = true;
          fetch(`${API}/models/warmup`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model: "clip" }),
          }).catch(() => {
            /* polled status will catch any backend error */
          });
        }
        setStatus("downloading");
        // Fetch byte progress (best-effort; backend may not have it yet on first ping)
        try {
          const pr = await fetch(`${API}/models/progress`, { cache: "no-store" });
          if (pr.ok) {
            const pj = (await pr.json()) as { clip: ClipProgress };
            setClipProg(pj.clip);
            const mb = (b: number) => (b / (1024 * 1024)).toFixed(0);
            const total = pj.clip.bytes_total;
            if (total > 0) {
              setStatusMsg(`Downloading visual search model — ${mb(pj.clip.bytes_now)} / ${mb(total)} MB`);
            } else {
              setStatusMsg("Downloading visual search model (~890 MB)…");
            }
          } else {
            setStatusMsg("Downloading visual search model (~890 MB)…");
          }
        } catch {
          setStatusMsg("Downloading visual search model (~890 MB)…");
        }
      } catch {
        // Backend not yet reachable. Show booting until grace expires.
        const elapsed = Date.now() - mountedAt.current;
        if (elapsed > BACKEND_GRACE_MS) {
          setStatus("error");
          setStatusMsg("Backend not responding. Check Settings → Logs.");
        }
      } finally {
        if (!cancelled) {
          setTimeout(poll, POLL_MS);
        }
      }
    };

    poll();

    return () => {
      cancelled = true;
    };
  }, [onComplete]);

  if (!mounted) return null;

  return createPortal(
    <AnimatePresence>
      {isVisible && (
        <motion.div
          initial={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.8, ease: "easeInOut" }}
          className="fixed inset-0 z-[9999] flex items-center justify-center bg-background"
        >
          <motion.div
            initial={{ scale: 0.9, opacity: 0, y: 20 }}
            animate={{ scale: 1, opacity: 1, y: 0 }}
            exit={{ scale: 1.1, opacity: 0, y: -20 }}
            transition={{ duration: 0.8, ease: "easeOut" }}
            className="flex flex-col items-center max-w-md px-6"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/logo-wordmark.png" alt="SovLens" className="h-24 mb-4" />
            <p className="text-muted-text tracking-widest uppercase text-sm font-medium mb-10">
              by sovstac
            </p>

            {/* Progress / status block */}
            <div className="w-full">
              {(status === "booting" || status === "downloading") && (
                <div className="h-1 w-full bg-black/10 dark:bg-white/10 rounded-full overflow-hidden mb-3">
                  {status === "downloading" && clipProg && clipProg.bytes_total > 0 ? (
                    <div
                      className="h-full bg-accent transition-[width] duration-500"
                      style={{ width: `${clipProg.percent}%` }}
                    />
                  ) : (
                    <motion.div
                      className="h-full w-1/3 bg-accent"
                      animate={{ x: ["-100%", "300%"] }}
                      transition={{
                        duration: 1.4,
                        repeat: Infinity,
                        ease: "easeInOut",
                      }}
                    />
                  )}
                </div>
              )}
              <p className="text-center text-sm text-muted-text">{statusMsg}</p>
              {status === "downloading" && (
                <p className="text-center text-xs text-muted-text/70 mt-2">
                  First-launch only. Future launches start instantly.
                </p>
              )}
              {status === "error" && (
                <button
                  onClick={() => {
                    setIsVisible(false);
                    setTimeout(onComplete, 400);
                  }}
                  className="mt-4 mx-auto block text-xs text-accent hover:underline"
                >
                  Continue anyway
                </button>
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
