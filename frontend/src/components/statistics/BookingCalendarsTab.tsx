"use client";

import { useEffect, useMemo, useState } from "react";
import {
  fetchBookingCalendars,
  updateBookingCalendarSource,
  type BookingCalendar,
} from "@/lib/api";

const CLASS_STYLES: Record<string, string> = {
  first: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  followup: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  exclude: "bg-zinc-500/10 text-zinc-500",
};

/** Curated calendar → source mapping. Lists every booking calendar with its
 * auto-classification + how many 1st calls it sourced, and lets the user edit
 * the source label that the Bookings drill-down groups by. */
export function BookingCalendarsTab() {
  const [rows, setRows] = useState<BookingCalendar[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [savingId, setSavingId] = useState<string | null>(null);
  // Local edits keyed by calendar_id, so typing doesn't fight the fetched state.
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await fetchBookingCalendars();
        if (!cancelled) setRows(data);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load calendars");
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Suggestions for the source-label datalist: existing labels + funnel tags.
  const suggestions = useMemo(() => {
    const s = new Set<string>(["webinar", "outreach", "referral"]);
    (rows ?? []).forEach((r) => { if (r.source_label) s.add(r.source_label); });
    return [...s].sort();
  }, [rows]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = rows ?? [];
    if (!q) return list;
    return list.filter((r) => (r.name || "").toLowerCase().includes(q));
  }, [rows, query]);

  const commit = async (cal: BookingCalendar) => {
    const draft = drafts[cal.calendar_id];
    if (draft === undefined) return;
    const next = draft.trim() || null;
    if (next === (cal.source_label ?? null)) return; // no change
    setSavingId(cal.calendar_id);
    try {
      const updated = await updateBookingCalendarSource(cal.calendar_id, next);
      setRows((prev) => (prev ?? []).map((r) => (r.calendar_id === cal.calendar_id ? updated : r)));
      setDrafts((d) => { const n = { ...d }; delete n[cal.calendar_id]; return n; });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSavingId(null);
    }
  };

  if (error) {
    return <div className="p-6 text-sm text-red-500">{error}</div>;
  }
  if (!rows) {
    return (
      <div className="flex items-center gap-3 p-6">
        <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
        <span className="text-sm text-zinc-500">Loading calendars…</span>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-lg font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Booking Calendars</h1>
          <p className="text-[11px] text-zinc-500 mt-0.5">
            The <span className="font-semibold">Source</span> you set here is what the Bookings drill-down groups by.
            Seeded from our auto-classification; your edits stick across syncs.
          </p>
        </div>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search calendars…"
          className="px-3 py-1.5 text-xs rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 w-56"
        />
      </div>

      <datalist id="source-label-suggestions">
        {suggestions.map((s) => <option key={s} value={s} />)}
      </datalist>

      <div className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/20 overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-zinc-50 dark:bg-zinc-900/50 border-b border-zinc-200 dark:border-zinc-800/40 text-zinc-500 uppercase tracking-wider text-[10px]">
              <th className="px-3 py-2 text-left font-semibold">Calendar</th>
              <th className="px-3 py-2 text-left font-semibold">Type</th>
              <th className="px-3 py-2 text-left font-semibold">Auto tag</th>
              <th className="px-3 py-2 text-right font-semibold">Bookings</th>
              <th className="px-3 py-2 text-left font-semibold">Source (editable)</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr key={r.calendar_id} className="border-b border-zinc-100 dark:border-zinc-800/30 last:border-0">
                <td className="px-3 py-2 text-zinc-800 dark:text-zinc-200">{r.name || <span className="text-zinc-500">—</span>}</td>
                <td className="px-3 py-2">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${CLASS_STYLES[r.calendar_class || ""] || "text-zinc-500"}`}>
                    {r.calendar_class || "—"}
                  </span>
                </td>
                <td className="px-3 py-2 text-zinc-500">{r.funnel_tag || "—"}</td>
                <td className="px-3 py-2 text-right font-mono text-zinc-600 dark:text-zinc-400">{r.booking_count}</td>
                <td className="px-3 py-2">
                  <input
                    list="source-label-suggestions"
                    value={drafts[r.calendar_id] ?? r.source_label ?? ""}
                    onChange={(e) => setDrafts((d) => ({ ...d, [r.calendar_id]: e.target.value }))}
                    onBlur={() => commit(r)}
                    onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                    disabled={savingId === r.calendar_id}
                    placeholder="—"
                    className="px-2 py-1 text-xs rounded border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 w-44 disabled:opacity-50"
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {filtered.length === 0 && <p className="text-sm text-zinc-500">No calendars match.</p>}
    </div>
  );
}
