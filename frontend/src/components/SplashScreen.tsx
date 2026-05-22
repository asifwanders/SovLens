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

// Mirror of backend/config.py LEVEL_PARAMS. Lives here too so the splash
// can decide which models to wait for without dragging in the rest of the
// settings page. The names must match the keys of /models/status.
const LEVEL_REQUIRED_MODELS: Record<string, string[]> = {
  low: ["clip"],
  medium: ["clip"],
  high: ["clip", "whisper", "ocr"],
  extreme: ["clip", "whisper", "ocr", "yolo"],
};

const MODEL_LABELS: Record<string, string> = {
  clip: "visual search (CLIP)",
  whisper: "voice transcription (Whisper)",
  ocr: "text in images (OCR)",
  yolo: "object detection (YOLO)",
};

type ModelStatus = {
  name?: string;
  loaded?: boolean;
  downloaded?: boolean;
  available?: boolean;
};
type ModelsStatusResponse = {
  clip: ModelStatus;
  whisper: ModelStatus;
  yolo: ModelStatus;
  ocr: ModelStatus;
};
type ModelProgress = {
  bytes_now: number;
  bytes_total: number;
  percent: number;
};
type ModelsProgressResponse = {
  clip: ModelProgress;
  whisper: ModelProgress;
  yolo: ModelProgress;
  ocr: ModelProgress;
};

export default function SplashScreen({ onComplete }: { onComplete: () => void }) {
  const [isVisible, setIsVisible] = useState(true);
  const [mounted, setMounted] = useState(false);
  const [status, setStatus] = useState<"booting" | "downloading" | "ready" | "error">("booting");
  const [statusMsg, setStatusMsg] = useState<string>("Starting backend…");
  // Aggregate progress across all required models (sum of bytes_now /
  // sum of bytes_total). Single bar is less confusing than per-model
  // bars when the user just wants "is it ready yet?"
  const [aggPct, setAggPct] = useState<number | null>(null);
  const [aggBytesText, setAggBytesText] = useState<string | null>(null);
  // Lazy-init in effect to keep render pure (Date.now() is impure).
  const mountedAt = useRef<number>(0);
  // Track which warmups we have already fired so we don't spam /models/warmup.
  const warmedRef = useRef<Set<string>>(new Set());
  // Active level — fetched once from /config on first successful poll.
  const requiredRef = useRef<string[] | null>(null);

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

    const fetchRequired = async (): Promise<string[]> => {
      if (requiredRef.current) return requiredRef.current;
      try {
        const res = await fetch(`${API}/config`, { cache: "no-store" });
        if (res.ok) {
          const data = (await res.json()) as { level?: string };
          const lvl = (data.level || "medium").toLowerCase();
          const reqs = LEVEL_REQUIRED_MODELS[lvl] ?? ["clip"];
          requiredRef.current = reqs;
          return reqs;
        }
      } catch {
        /* backend not up yet */
      }
      // Fall back to CLIP-only until /config responds.
      return LEVEL_REQUIRED_MODELS["medium"];
    };

    const mb = (b: number) => (b / (1024 * 1024)).toFixed(0);

    const poll = async () => {
      if (cancelled) return;
      try {
        const required = await fetchRequired();
        const res = await fetch(`${API}/models/status`, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as ModelsStatusResponse;

        // For each required model that isn't downloaded yet, fire warmup
        // once. This is what actually kicks off the HF download — without
        // it the splash would block forever waiting for a download that
        // nobody triggered.
        for (const key of required) {
          const ms = data[key as keyof ModelsStatusResponse];
          if (!ms?.downloaded && !warmedRef.current.has(key)) {
            warmedRef.current.add(key);
            void fetch(`${API}/models/warmup`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ model: key }),
            }).catch(() => {
              /* polled status will catch any backend error */
            });
          }
        }

        // All required models present → dismiss the splash.
        const allReady = required.every((k) => {
          const ms = data[k as keyof ModelsStatusResponse];
          return ms?.downloaded === true;
        });
        if (allReady) {
          setStatus("ready");
          setStatusMsg("Ready");
          dismiss();
          return;
        }

        setStatus("downloading");
        // Aggregate progress across all required models that are still pending.
        try {
          const pr = await fetch(`${API}/models/progress`, { cache: "no-store" });
          if (pr.ok) {
            const pj = (await pr.json()) as ModelsProgressResponse;
            let totNow = 0;
            let totAll = 0;
            const pending: string[] = [];
            for (const key of required) {
              const ms = data[key as keyof ModelsStatusResponse];
              if (ms?.downloaded) continue;
              pending.push(MODEL_LABELS[key] ?? key);
              const p = pj[key as keyof ModelsProgressResponse];
              if (p) {
                totNow += p.bytes_now;
                totAll += p.bytes_total;
              }
            }
            if (totAll > 0) {
              setAggPct(Math.min(100, Math.round((100 * totNow) / totAll)));
              setAggBytesText(`${mb(totNow)} / ${mb(totAll)} MB`);
            } else {
              setAggPct(null);
              setAggBytesText(null);
            }
            const label = pending.slice(0, 2).join(", ") + (pending.length > 2 ? ", …" : "");
            setStatusMsg(`Downloading ${label}`);
          } else {
            setStatusMsg("Preparing models…");
          }
        } catch {
          setStatusMsg("Preparing models…");
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
                  {status === "downloading" && aggPct !== null ? (
                    <div
                      className="h-full bg-accent transition-[width] duration-500"
                      style={{ width: `${aggPct}%` }}
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
              {status === "downloading" && aggBytesText && (
                <p className="text-center text-xs text-muted-text/70 mt-2">
                  {aggBytesText}
                </p>
              )}
              {status === "downloading" && (
                <p className="text-center text-xs text-muted-text/70 mt-1">
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
