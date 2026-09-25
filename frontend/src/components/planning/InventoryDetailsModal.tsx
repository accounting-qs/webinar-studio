"use client";

import { type GoodAvailable, type GoodAvailGeoRow } from "@/lib/api";

/** Detail view behind the Planning header's Good-Avail chips: the same fresh
 *  "ideal" inventory, split by bucket grade (good/medium/bad/no grade) x
 *  location (US+Canada, Europe, no location, rest of world). */
export default function InventoryDetailsModal({
  data,
  loading,
  error,
  onClose,
}: {
  data: GoodAvailable | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  const grades: { key: "good" | "medium" | "bad" | "none"; label: string; color: string }[] = [
    { key: "good", label: "Good", color: "text-emerald-500 dark:text-emerald-400" },
    { key: "medium", label: "Medium", color: "text-amber-500 dark:text-amber-400" },
    { key: "bad", label: "Bad", color: "text-red-500 dark:text-red-400" },
    { key: "none", label: "No grade", color: "text-zinc-500 dark:text-zinc-400" },
  ];
  const breakdown = data?.breakdown ?? null;
  const other = (r: GoodAvailGeoRow) => Math.max(0, r.total - r.us_ca - r.europe - r.no_location);
  const sum = (pick: (r: GoodAvailGeoRow) => number) =>
    breakdown ? grades.reduce((s, g) => s + pick(breakdown[g.key]), 0) : 0;
  const fmt = (n: number) => n.toLocaleString();

  const headline = [
    { label: "Good Avail", value: data?.total },
    { label: "Good US+CA", value: data?.us_ca },
    { label: "Good EU", value: data?.europe },
    { label: "Good No-loc", value: data?.no_location },
  ];

  return (
    <div
      className="fixed inset-0 z-[60] bg-black/60 backdrop-blur-sm flex items-center justify-center"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800/60 rounded-2xl shadow-2xl w-full max-w-2xl overflow-hidden">
        {/* Header */}
        <div className="px-6 py-4 border-b border-zinc-200 dark:border-zinc-800/40 flex items-center justify-between">
          <div>
            <h3 className="text-base font-bold text-zinc-900 dark:text-zinc-100">Available Inventory</h3>
            <p className="text-[11px] text-zinc-500 mt-0.5">Fresh, claimable contacts by bucket grade and location</p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-5">
          {error && (
            <div className="text-[11px] text-red-500 border border-red-500/40 rounded-lg px-3 py-2">
              {error} — these are the numbers from before the refresh, not a fresh count.
            </div>
          )}

          {/* Headline: the same four numbers as the header chips */}
          <div className="grid grid-cols-4 gap-2">
            {headline.map((h) => (
              <div key={h.label} className="rounded-lg bg-zinc-50 dark:bg-zinc-900/60 border border-zinc-200 dark:border-zinc-800/40 px-3 py-2">
                <div className="text-[10px] text-zinc-500 uppercase tracking-wider">{h.label}</div>
                <div className="text-sm font-bold font-mono text-teal-500 dark:text-teal-400">
                  {loading ? "…" : h.value != null ? fmt(h.value) : "—"}
                </div>
              </div>
            ))}
          </div>

          {/* Grade x location distribution */}
          {breakdown ? (
            <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800/40">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-zinc-50 dark:bg-zinc-900/60 text-[10px] text-zinc-500 uppercase tracking-wider">
                    <th className="px-3 py-2 text-left font-medium">Grade</th>
                    <th className="px-3 py-2 text-right font-medium">Total</th>
                    <th className="px-3 py-2 text-right font-medium">US + Canada</th>
                    <th className="px-3 py-2 text-right font-medium">Europe</th>
                    <th className="px-3 py-2 text-right font-medium">No location</th>
                    <th className="px-3 py-2 text-right font-medium">Other</th>
                  </tr>
                </thead>
                <tbody>
                  {grades.map((g) => {
                    const r = breakdown[g.key];
                    return (
                      <tr key={g.key} className="border-t border-zinc-200 dark:border-zinc-800/40">
                        <td className={`px-3 py-2 font-semibold ${g.color}`}>{g.label}</td>
                        <td className="px-3 py-2 text-right font-mono text-zinc-800 dark:text-zinc-200">{fmt(r.total)}</td>
                        <td className="px-3 py-2 text-right font-mono text-zinc-600 dark:text-zinc-300">{fmt(r.us_ca)}</td>
                        <td className="px-3 py-2 text-right font-mono text-zinc-600 dark:text-zinc-300">{fmt(r.europe)}</td>
                        <td className="px-3 py-2 text-right font-mono text-zinc-600 dark:text-zinc-300">{fmt(r.no_location)}</td>
                        <td className="px-3 py-2 text-right font-mono text-zinc-600 dark:text-zinc-300">{fmt(other(r))}</td>
                      </tr>
                    );
                  })}
                  <tr className="border-t border-zinc-200 dark:border-zinc-800/40 bg-zinc-50 dark:bg-zinc-900/60">
                    <td className="px-3 py-2 font-semibold text-zinc-900 dark:text-zinc-100">All grades</td>
                    <td className="px-3 py-2 text-right font-mono font-bold text-zinc-900 dark:text-zinc-100">{fmt(sum((r) => r.total))}</td>
                    <td className="px-3 py-2 text-right font-mono font-bold text-zinc-900 dark:text-zinc-100">{fmt(sum((r) => r.us_ca))}</td>
                    <td className="px-3 py-2 text-right font-mono font-bold text-zinc-900 dark:text-zinc-100">{fmt(sum((r) => r.europe))}</td>
                    <td className="px-3 py-2 text-right font-mono font-bold text-zinc-900 dark:text-zinc-100">{fmt(sum((r) => r.no_location))}</td>
                    <td className="px-3 py-2 text-right font-mono font-bold text-zinc-900 dark:text-zinc-100">{fmt(sum(other))}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-[11px] text-zinc-500 border border-zinc-200 dark:border-zinc-800/40 rounded-lg px-3 py-2">
              {loading
                ? "Counting inventory…"
                : "The grade breakdown hasn't been computed yet — use the header's refresh button to recount."}
            </div>
          )}

          <div className="text-[10px] text-zinc-500 space-y-1">
            <p>Counts are never-invited, unassigned, non-blocklisted contacts; the disqualified bucket is excluded and each bucket&apos;s saved Segments employee range is applied where set.</p>
            <p>The teal &quot;Good Avail&quot; header numbers = Good + Medium + No grade (Bad excluded). &quot;Other&quot; = located outside US+Canada and Europe.</p>
          </div>
        </div>
      </div>
    </div>
  );
}
