"use client";

import { useState, useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import MediaGrid from "@/components/MediaGrid";
import { MediaItem } from "@/lib/types";
import MediaModal from "@/components/MediaModal";
import { Search, X, ArrowRight, Clock } from "lucide-react";

const HISTORY_KEY = "sovlens.search.history";
const MAX_HISTORY = 5;

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState<string | null>(null);
  const [media, setMedia] = useState<MediaItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [isSearching, setIsSearching] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [history, setHistory] = useState<string[]>(() => {
    if (typeof window === "undefined") return [];
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch {
      return [];
    }
  });
  const [showHistory, setShowHistory] = useState(false);

  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const hasResults = submittedQuery !== null;

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  function saveToHistory(q: string) {
    const trimmed = q.trim();
    if (!trimmed) return;
    setHistory((prev) => {
      const next = [trimmed, ...prev.filter((h) => h !== trimmed)].slice(
        0,
        MAX_HISTORY
      );
      localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
      return next;
    });
  }

  function removeFromHistory(q: string) {
    setHistory((prev) => {
      const next = prev.filter((h) => h !== q);
      localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
      return next;
    });
  }

  const handleSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    const q = query.trim();
    if (!q) return;

    saveToHistory(q);
    setSubmittedQuery(q);
    setShowHistory(false);
    setIsSearching(true);

    try {
      const res = await fetch("http://127.0.0.1:14793/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, limit: 40 }),
      });
      if (!res.ok) throw new Error(`Backend error: ${res.status}`);
      const data = await res.json();
      setMedia(data.items || []);
      setHasMore(false);
    } catch (err) {
      console.error(err);
    } finally {
      setIsSearching(false);
    }
  };

  const handleHistorySelect = (h: string) => {
    setQuery(h);
    // Submit after state update via a micro-task
    setTimeout(() => {
      const q = h.trim();
      if (!q) return;
      saveToHistory(q);
      setSubmittedQuery(q);
      setShowHistory(false);
      setIsSearching(true);
      fetch("http://127.0.0.1:14793/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, limit: 40 }),
      })
        .then((res) => {
          if (!res.ok) throw new Error(`Backend error: ${res.status}`);
          return res.json();
        })
        .then((data) => {
          setMedia(data.items || []);
          setHasMore(false);
        })
        .catch(console.error)
        .finally(() => setIsSearching(false));
    }, 0);
  };

  return (
    <div
      ref={containerRef}
      className="h-full w-full p-8 overflow-y-auto relative"
    >
      <motion.div
        layout
        transition={{ type: "spring", stiffness: 200, damping: 25 }}
        className={
          hasResults
            ? "max-w-3xl mx-auto"
            : "max-w-2xl mx-auto flex flex-col items-center justify-center min-h-[60vh]"
        }
      >
        {!hasResults && (
          <h1 className="text-3xl font-semibold mb-6 text-center">
            Search your media
          </h1>
        )}

        <form onSubmit={handleSubmit} className="relative w-full">
          <div className="relative">
            <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-5 h-5 text-foreground/40" />
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onFocus={() => setShowHistory(true)}
              onBlur={() => setTimeout(() => setShowHistory(false), 150)}
              placeholder="e.g. dolphin, sunset, person in red shirt"
              className="w-full pl-12 pr-12 py-4 rounded-xl bg-foreground/5 border border-panel-border focus:outline-none focus:border-accent text-base"
            />
            {query && (
              <button
                type="button"
                onClick={() => {
                  // Only clear input — preserve results so user can edit + re-search
                  // without losing context. Pressing Enter on empty is a no-op.
                  setQuery("");
                  inputRef.current?.focus();
                }}
                className="absolute right-12 top-1/2 -translate-y-1/2 text-foreground/40 hover:text-foreground"
                aria-label="Clear query"
              >
                <X className="w-4 h-4" />
              </button>
            )}
            <button
              type="submit"
              disabled={!query.trim() || isSearching}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-2 rounded-lg bg-accent text-white hover:bg-accent/90 disabled:opacity-50 transition-colors"
              aria-label="Submit search"
            >
              <ArrowRight className="w-4 h-4" />
            </button>
          </div>

          {/* History dropdown */}
          <AnimatePresence>
            {showHistory && history.length > 0 && (
              <motion.div
                initial={{ opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.12 }}
                className="absolute left-0 right-0 top-full mt-2 rounded-xl border border-panel-border bg-background shadow-lg overflow-hidden z-10"
              >
                <div className="px-3 py-2 text-xs uppercase tracking-wider text-foreground/50 border-b border-panel-border">
                  Recent
                </div>
                {history.map((h) => (
                  <div key={h} className="flex items-center group">
                    <button
                      type="button"
                      onMouseDown={(e) => {
                        e.preventDefault();
                        handleHistorySelect(h);
                      }}
                      className="flex-1 text-left px-4 py-2.5 text-sm hover:bg-foreground/5 flex items-center gap-2"
                    >
                      <Clock className="w-3.5 h-3.5 text-foreground/40 shrink-0" />
                      <span>{h}</span>
                    </button>
                    <button
                      type="button"
                      onMouseDown={(e) => {
                        e.preventDefault();
                        removeFromHistory(h);
                      }}
                      className="px-3 py-2.5 text-foreground/40 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                      title="Remove from history"
                      aria-label={`Remove "${h}" from history`}
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                ))}
              </motion.div>
            )}
          </AnimatePresence>
        </form>

        {hasResults && media.length > 0 && (
          <p className="mt-3 text-sm text-foreground/50 text-center">
            Showing {media.length} results — click any thumbnail to jump to the
            matched moment.
          </p>
        )}
      </motion.div>

      {/* Results */}
      {hasResults && (
        <div className="max-w-7xl mx-auto mt-8">
          {isSearching ? (
            <div className="w-full h-[40vh] flex items-center justify-center">
              <p className="text-foreground/50 text-lg">Searching…</p>
            </div>
          ) : media.length > 0 ? (
            <MediaGrid
              items={media}
              hasMore={hasMore}
              onLoadMore={() => {}}
              onItemClick={(item) => {
                const idx = media.findIndex((m) => m.id === item.id);
                if (idx !== -1) setSelectedIndex(idx);
              }}
            />
          ) : (
            <div className="w-full h-[40vh] glass-panel rounded-2xl p-8 flex items-center justify-center">
              <p className="text-foreground/50 text-xl font-medium">
                No results found for &ldquo;{submittedQuery}&rdquo;
              </p>
            </div>
          )}
        </div>
      )}

      {selectedIndex !== null && (
        <MediaModal
          item={media[selectedIndex]}
          onClose={() => setSelectedIndex(null)}
          onPrev={() => setSelectedIndex(Math.max(0, selectedIndex - 1))}
          onNext={() =>
            setSelectedIndex(Math.min(media.length - 1, selectedIndex + 1))
          }
          hasPrev={selectedIndex > 0}
          hasNext={selectedIndex < media.length - 1}
        />
      )}
    </div>
  );
}
