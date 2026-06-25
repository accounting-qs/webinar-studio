"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchStatisticsRecomputeStatus,
  recomputeStatistics,
  type StatisticsRecomputeStatus,
} from "@/lib/api";

/** Compact "Updated …" label + "Recompute now" button, shared by the
 * Statistics and Segments tabs. Triggers a full background snapshot rebuild,
 * polls progress while it runs, and calls `onDone` when a run finishes so the
 * parent can refetch the now-fresh data. */
export function RecomputeControl({ onDone }: { onDone?: () => void }) {
  const [status, setStatus] = useState<StatisticsRecomputeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const wasRunning = useRef(false);
  const onDoneRef = useRef(onDone);
  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  // Fetch status; on the running→idle transition, clear busy and notify the
  // parent so it can refetch the freshly-rebuilt data.
  const refreshStatus = useCallback(async () => {
    try {
      const s = await fetchStatisticsRecomputeStatus();
      setStatus(s);
      if (s.running) {
        wasRunning.current = true;
      } else if (wasRunning.current) {
        wasRunning.current = false;
        setBusy(false);
        onDoneRef.current?.();
      }
    } catch {
      // transient — keep the last known status
    }
  }, []);

  // Initial status read (inline async so setState lands in a callback, after
  // the await — not synchronously in the effect body).
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const s = await fetchStatisticsRecomputeStatus();
        if (active) {
          setStatus(s);
          if (s.running) wasRunning.current = true;
        }
      } catch {
        /* ignore */
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  // While a run is in flight (manual click or a sync-triggered rebuild), poll.
  useEffect(() => {
    if (!status?.running) return;
    const id = setInterval(refreshStatus, 2500);
    return () => clearInterval(id);
  }, [status?.running, refreshStatus]);

  const handleRecompute = useCallback(async () => {
    setBusy(true);
    wasRunning.current = true;
    try {
      const s = await recomputeStatistics();
      setStatus(s);
    } catch {
      setBusy(false);
      wasRunning.current = false;
    }
  }, []);

  const running = status?.running ?? false;
  const label = running
    ? status && status.total > 0
      ? `Recomputing… ${status.done}/${status.total}`
      : "Recomputing…"
    : busy
      ? "Starting…"
      : "Recompute now";

  return (
    <div className="flex items-center gap-2">
      <span
        className="text-[11px] text-zinc-500 whitespace-nowrap"
        title={
          status?.last_computed_at
            ? `Snapshots last built: ${status.last_computed_at}`
            : "No stored statistics yet"
        }
      >
        {running ? "Updating…" : `Updated ${relTime(status?.last_computed_at ?? null)}`}
      </span>
      <button
        onClick={handleRecompute}
        disabled={busy || running}
        className="px-3 py-1.5 text-xs rounded-lg bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-200 disabled:opacity-50"
        title="Rebuild all stored statistics from the latest synced data"
      >
        {label}
      </button>
    </div>
  );
}

function relTime(iso: string | null): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "never";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}
