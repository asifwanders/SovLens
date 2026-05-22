"use client";

// Global ingestion / reindex / sync progress banner.
//
// Mounted from app/layout.tsx so it renders on EVERY route (All Media,
// Search, Folders, Settings, …) without per-page duplication. Polls
// /status every 1.5 s for the current job snapshot:
//   - data.is_ingesting   -> show banner at all?
//   - data.jobs[]         -> per-job (type, description, done, total, current)
//
// Behaviour:
//   - No job, not ingesting -> renders nothing
//   - is_ingesting but no jobs[] entry yet (race during background_task
//     spawn) -> generic "AI is analyzing media…" spinner
//   - Job with total>0 -> progress bar + "done / total" + current filename
//
// Z-index 9999 so it sits above modals/dropdowns. Top-center fixed.

import { useEffect, useState } from "react";

const API = "http://127.0.0.1:14793";

type JobInfo = {
  id: string;
  type: string;
  description: string;
  total: number;
  done: number;
  current: string;
};

type StatusResponse = {
  is_ingesting: boolean;
  jobs?: JobInfo[];
};

export default function JobBanner() {
  const [isIngesting, setIsIngesting] = useState(false);
  const [activeJob, setActiveJob] = useState<JobInfo | null>(null);

  useEffect(() => {
    let cancelled = false;

    const tick = async () => {
      try {
        const res = await fetch(`${API}/status`, { cache: "no-store" });
        if (!res.ok) return;
        const data = (await res.json()) as StatusResponse;
        if (cancelled) return;
        setIsIngesting(data.is_ingesting);
        const jobs = data.jobs ?? [];
        if (jobs.length === 0) {
          setActiveJob(null);
        } else {
          // Prefer the reindex if running (most informative); else the
          // job with the largest total.
          const reindex = jobs.find((j) => j.type === "reindex_all");
          setActiveJob(
            reindex ?? jobs.reduce((a, b) => (b.total > a.total ? b : a), jobs[0]),
          );
        }
      } catch {
        // backend may be temporarily offline (e.g. during sidecar restart
        // after /admin/wipe_data) — keep last-known state.
      }
    };

    void tick();
    const interval = setInterval(() => {
      void tick();
    }, 1500);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  if (!isIngesting && !activeJob) return null;

  const pct =
    activeJob && activeJob.total > 0
      ? Math.min(100, Math.round((100 * activeJob.done) / activeJob.total))
      : null;

  const filename = activeJob?.current
    ? activeJob.current.split(/[\\/]/).pop()
    : null;

  return (
    <div className="fixed top-4 left-1/2 -translate-x-1/2 z-[9999] flex flex-col items-stretch gap-2 bg-black/85 dark:bg-white/10 backdrop-blur-xl text-white px-5 py-3 rounded-2xl shadow-2xl border border-white/10 min-w-[340px] max-w-[520px] animate-in slide-in-from-top-4">
      <div className="flex items-center gap-3">
        <div className="w-5 h-5 border-2 border-[#00b9a0] border-t-transparent rounded-full animate-spin shrink-0" />
        <span className="text-sm font-medium flex-1 truncate">
          {activeJob?.description ?? "AI is analyzing media…"}
          {activeJob && activeJob.total > 0
            ? ` — ${activeJob.done} / ${activeJob.total}`
            : ""}
        </span>
      </div>
      {pct !== null && (
        <div className="h-1 w-full bg-white/15 rounded-full overflow-hidden">
          <div
            className="h-full bg-[#00b9a0] transition-[width] duration-500"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      {filename && (
        <div className="text-[10px] text-white/60 w-full truncate">
          {filename}
        </div>
      )}
    </div>
  );
}
