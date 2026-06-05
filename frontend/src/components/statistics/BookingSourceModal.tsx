"use client";

import { useEffect, useState } from "react";
import { fetchStatisticsContacts, type ContactDrilldownResponse } from "@/lib/api";
import { ContactsDrilldownTable } from "./ContactsDrilldownTable";

/** What a clicked metric cell hands to the modal. */
export type DrillTarget = {
  metric: string;
  group: string;
  label: string;
  webinarId?: string | null;
  webinarNumber?: number;
  assignment?: string | null;
  listLabel?: string | null;
};

/** Large modal opened by clicking a drillable metric number on the Statistics
 * dashboard. Shows the booking-source breakdown + the contacts / opportunities
 * behind that number. */
export function BookingSourceModal({ target, onClose }: { target: DrillTarget; onClose: () => void }) {
  const [data, setData] = useState<ContactDrilldownResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Escape to close + lock body scroll while open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    (async () => {
      try {
        const res = await fetchStatisticsContacts({
          webinarId: target.webinarId ?? undefined,
          webinarNumber: target.webinarNumber ?? undefined,
          metric: target.metric,
          assignment: target.assignment ?? undefined,
        });
        if (!cancelled) setData(res);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [target.metric, target.webinarId, target.webinarNumber, target.assignment]);

  const unitLabel = data?.unit === "opportunity" ? "opportunities" : "contacts";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-5xl w-full max-h-[85vh] overflow-y-auto"
      >
        <div className="flex items-start justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800 sticky top-0 bg-white dark:bg-zinc-900 z-10">
          <div>
            <div className="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold mb-0.5">
              {target.group}{target.listLabel ? ` · ${target.listLabel}` : ""}
            </div>
            <div className="text-base font-bold text-zinc-900 dark:text-zinc-100">{target.label}</div>
            {data && (
              <div className="text-[11px] text-zinc-500 mt-0.5">
                {data.total} {unitLabel}
                {data.items.length < data.total ? ` · showing first ${data.items.length}` : ""}
              </div>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
            aria-label="Close"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="px-6 py-5 space-y-4">
          {loading && (
            <div className="flex items-center gap-3 py-8 justify-center">
              <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
              <span className="text-sm text-zinc-500">Loading {unitLabel}…</span>
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-400">{error}</div>
          )}

          {data && !loading && (
            <>
              {!data.available && (
                <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-500">
                  {data.reason ?? "Drill-down unavailable for this metric."}
                </div>
              )}
              <ContactsDrilldownTable data={data} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
