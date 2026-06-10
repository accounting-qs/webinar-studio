"use client";

import { useEffect, useState } from "react";
import { fetchListDistribution, type ListDistributionResponse } from "@/lib/api";

/** What opens the list-name distribution modal. */
export type ListDistTarget = {
  scope: "assignment" | "webinar";
  assignment?: string | null;
  webinarId?: string | null;
  webinarNumber?: number;
  /** Human label for the header (list description or "Webinar N"). */
  label: string;
};

/** Modal showing how the contacts in an assigned list — or across all assigned
 * lists of a webinar — break down by their source list name
 * (contacts.lead_list_name). Each row is a list name, its contact count, and
 * its percentage share of the scope. */
export function ListDistributionModal({ target, onClose }: { target: ListDistTarget; onClose: () => void }) {
  const [data, setData] = useState<ListDistributionResponse | null>(null);
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
        const res = await fetchListDistribution({
          assignment: target.scope === "assignment" ? target.assignment ?? undefined : undefined,
          webinarId: target.scope === "webinar" ? target.webinarId ?? undefined : undefined,
          webinarNumber: target.scope === "webinar" ? target.webinarNumber ?? undefined : undefined,
        });
        if (!cancelled) setData(res);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [target.scope, target.assignment, target.webinarId, target.webinarNumber]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-2xl w-full max-h-[85vh] overflow-y-auto"
      >
        <div className="flex items-start justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800 sticky top-0 bg-white dark:bg-zinc-900 z-10">
          <div>
            <div className="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold mb-0.5">
              {target.scope === "webinar" ? "Webinar · all assigned lists" : "Assigned list"}
            </div>
            <div className="text-base font-bold text-zinc-900 dark:text-zinc-100">{target.label}</div>
            <div className="text-[11px] text-zinc-500 mt-0.5">
              List-name distribution
              {data ? ` · ${data.total} contact${data.total === 1 ? "" : "s"} · ${data.items.length} list${data.items.length === 1 ? "" : "s"}` : ""}
            </div>
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
          <p className="text-[11px] text-zinc-500 dark:text-zinc-400 leading-relaxed">
            Each contact carries the source list name it was uploaded under. This shows what
            share of the contacts {target.scope === "webinar" ? "across all assigned lists for this webinar" : "in this assigned list"} came from each list name.
          </p>

          {loading && (
            <div className="flex items-center gap-3 py-8 justify-center">
              <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
              <span className="text-sm text-zinc-500">Loading distribution…</span>
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-400">{error}</div>
          )}

          {data && !loading && (
            data.items.length === 0 ? (
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-500">
                No contacts found for this {target.scope === "webinar" ? "webinar" : "list"}.
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[10px] uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
                    <th className="py-1.5 pr-2 font-semibold">List name</th>
                    <th className="py-1.5 px-2 font-semibold text-right w-20">Contacts</th>
                    <th className="py-1.5 pl-2 font-semibold text-right w-32">Share</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((it, i) => (
                    <tr key={i} className="border-b border-zinc-100 dark:border-zinc-800/40">
                      <td className="py-1.5 pr-2 text-zinc-800 dark:text-zinc-200">
                        {it.list_name ?? <span className="italic text-zinc-400">— no list name</span>}
                      </td>
                      <td className="py-1.5 px-2 text-right tabular-nums text-zinc-700 dark:text-zinc-300">
                        {it.count.toLocaleString()}
                      </td>
                      <td className="py-1.5 pl-2 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <div className="flex-1 h-1.5 bg-zinc-100 dark:bg-zinc-800 rounded-full overflow-hidden max-w-[80px]">
                            <div
                              className="h-full bg-violet-500 rounded-full"
                              style={{ width: `${it.pct}%` }}
                            />
                          </div>
                          <span className="tabular-nums text-zinc-600 dark:text-zinc-400 w-12 text-right">
                            {it.pct.toFixed(1)}%
                          </span>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </div>
    </div>
  );
}
