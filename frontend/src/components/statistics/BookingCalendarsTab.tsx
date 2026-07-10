"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchBookingCalendars,
  renameBookingSource,
  updateBookingCalendar,
  type BookingCalendar,
} from "@/lib/api";

const CLASS_STYLES: Record<string, string> = {
  first: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  followup: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  exclude: "bg-zinc-500/10 text-zinc-500",
};

const CLASS_OPTIONS: { value: string; label: string; hint: string }[] = [
  { value: "first", label: "first", hint: "1st sales call — counts as a booking" },
  { value: "followup", label: "followup", hint: "2nd+ meeting on an existing deal" },
  { value: "exclude", label: "exclude", hint: "Not a sales call — ignored in stats" },
];

/** Curated calendar → source mapping. Lists every booking calendar with its
 * classification + how many 1st calls it sourced, and lets the user edit both
 * the call type (first/followup/exclude) and the source label that the
 * Bookings drill-down groups by. */
export function BookingCalendarsTab() {
  const [rows, setRows] = useState<BookingCalendar[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [savingId, setSavingId] = useState<string | null>(null);

  const load = async () => {
    try {
      const data = await fetchBookingCalendars();
      setRows(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load calendars");
    }
  };

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

  // Known source labels for the dropdown: existing labels + funnel tags.
  const sourceOptions = useMemo(() => {
    const s = new Set<string>(["webinar", "outreach", "referral"]);
    (rows ?? []).forEach((r) => { if (r.source_label) s.add(r.source_label); });
    return [...s].sort((a, b) => a.localeCompare(b));
  }, [rows]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = rows ?? [];
    if (!q) return list;
    return list.filter((r) => (r.name || "").toLowerCase().includes(q));
  }, [rows, query]);

  const patchCalendar = async (
    cal: BookingCalendar,
    patch: { source_label?: string | null; calendar_class?: string },
  ) => {
    setSavingId(cal.calendar_id);
    setError(null);
    try {
      const updated = await updateBookingCalendar(cal.calendar_id, patch);
      setRows((prev) => (prev ?? []).map((r) => (r.calendar_id === cal.calendar_id ? updated : r)));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSavingId(null);
    }
  };

  const renameSource = async (from: string, to: string) => {
    setError(null);
    try {
      await renameBookingSource(from, to);
      await load(); // label changed on every calendar carrying it — refetch
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to rename source");
    }
  };

  if (error && !rows) {
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
    <div className="h-full flex flex-col p-6 gap-4">
      <div className="flex-none flex items-center justify-between gap-4">
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

      {error && <p className="flex-none text-xs text-red-500">{error}</p>}

      <div className="flex-1 min-h-0 overflow-auto rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/20">
        <table className="w-full text-xs border-separate border-spacing-0">
          <thead>
            <tr className="text-zinc-500 uppercase tracking-wider text-[10px]">
              <th className={STICKY_TH + " text-left"}>Calendar</th>
              <th className={STICKY_TH + " text-left"}>Type (editable)</th>
              <th className={STICKY_TH + " text-left"}>Auto tag</th>
              <th className={STICKY_TH + " text-right"}>Bookings</th>
              <th className={STICKY_TH + " text-left"}>Source (editable)</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr key={r.calendar_id} className="group">
                <td className={CELL + " text-zinc-800 dark:text-zinc-200"}>{r.name || <span className="text-zinc-500">—</span>}</td>
                <td className={CELL}>
                  <ClassSelect
                    value={r.calendar_class}
                    disabled={savingId === r.calendar_id}
                    onSelect={(cls) => patchCalendar(r, { calendar_class: cls })}
                  />
                </td>
                <td className={CELL + " text-zinc-500"}>{r.funnel_tag || "—"}</td>
                <td className={CELL + " text-right font-mono text-zinc-600 dark:text-zinc-400"}>{r.booking_count}</td>
                <td className={CELL}>
                  <SourceSelect
                    value={r.source_label}
                    options={sourceOptions}
                    disabled={savingId === r.calendar_id}
                    saving={savingId === r.calendar_id}
                    onSelect={(label) => patchCalendar(r, { source_label: label })}
                    onRename={renameSource}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {filtered.length === 0 && <p className="p-4 text-sm text-zinc-500">No calendars match.</p>}
      </div>
    </div>
  );
}

// Sticky header cells: the table scrolls inside its container and the header
// stays pinned. border-separate keeps the bottom border attached while stuck.
const STICKY_TH =
  "sticky top-0 z-10 px-3 py-2 font-semibold bg-zinc-50 dark:bg-zinc-900 " +
  "border-b border-zinc-200 dark:border-zinc-800/40";

const CELL = "px-3 py-2 border-b border-zinc-100 dark:border-zinc-800/30 group-last:border-b-0";

/** Shared popover shell: closes on outside click. */
function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return { open, setOpen, ref };
}

/** Call-type picker: first / followup / exclude, rendered as the class badge. */
function ClassSelect({
  value,
  disabled,
  onSelect,
}: {
  value: string | null;
  disabled: boolean;
  onSelect: (cls: string) => void;
}) {
  const { open, setOpen, ref } = usePopover();

  return (
    <div className="relative inline-block" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold disabled:opacity-50 ${CLASS_STYLES[value || ""] || "text-zinc-500 bg-zinc-500/10"}`}
      >
        {value || "—"}
        <span className="text-[8px] opacity-60">▾</span>
      </button>

      {open && (
        <div className="absolute left-0 mt-1 z-50 w-64 rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 shadow-xl py-1">
          {CLASS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => { setOpen(false); if (opt.value !== value) onSelect(opt.value); }}
              className="w-full flex items-start gap-2 px-3 py-1.5 text-left hover:bg-zinc-50 dark:hover:bg-zinc-800"
            >
              <span className={`mt-px px-1.5 py-0.5 rounded text-[10px] font-semibold ${CLASS_STYLES[opt.value]}`}>
                {opt.label}
              </span>
              <span className="flex-1 text-[10px] text-zinc-500 leading-4">{opt.hint}</span>
              {opt.value === value && <span className="text-violet-500 text-xs">✓</span>}
            </button>
          ))}
          <p className="px-3 pt-1.5 pb-1 text-[9px] text-zinc-400 border-t border-zinc-100 dark:border-zinc-800 mt-1">
            Changing the type retags this calendar&apos;s appointments and recomputes bookings.
          </p>
        </div>
      )}
    </div>
  );
}

/** Source picker: select an existing label, type to filter or create a new
 * one, clear it, or rename a label everywhere it&apos;s used. */
function SourceSelect({
  value,
  options,
  disabled,
  saving,
  onSelect,
  onRename,
}: {
  value: string | null;
  options: string[];
  disabled: boolean;
  saving: boolean;
  onSelect: (label: string | null) => void;
  onRename: (from: string, to: string) => Promise<void>;
}) {
  const { open, setOpen, ref } = usePopover();
  const [search, setSearch] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  const openPopover = () => {
    setSearch("");
    setRenaming(null);
    setOpen(true);
  };

  const choose = (label: string | null) => {
    setOpen(false);
    if (label !== (value ?? null)) onSelect(label);
  };

  const commitRename = async (from: string) => {
    const to = renameDraft.trim();
    setRenaming(null);
    if (!to || to === from) return;
    setOpen(false);
    await onRename(from, to);
  };

  const q = search.trim().toLowerCase();
  const visible = q ? options.filter((o) => o.toLowerCase().includes(q)) : options;
  const canCreate = q.length > 0 && !options.some((o) => o.toLowerCase() === q);

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => (open ? setOpen(false) : openPopover())}
        disabled={disabled}
        className="w-44 flex items-center justify-between gap-2 px-2 py-1 text-xs rounded border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 disabled:opacity-50 hover:border-zinc-300 dark:hover:border-zinc-600"
      >
        <span className={value ? "" : "text-zinc-400"}>{value || "—"}</span>
        {saving ? (
          <span className="w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
        ) : (
          <span className="text-zinc-400 text-[9px]">▾</span>
        )}
      </button>

      {open && (
        <div className="absolute left-0 mt-1 z-50 w-64 rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 shadow-xl">
          <div className="p-2 border-b border-zinc-100 dark:border-zinc-800">
            <input
              autoFocus
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && search.trim()) choose(search.trim());
                if (e.key === "Escape") setOpen(false);
              }}
              placeholder="Search or create…"
              className="w-full px-2 py-1 text-xs rounded border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-950 text-zinc-800 dark:text-zinc-200"
            />
          </div>

          <div className="max-h-56 overflow-y-auto py-1">
            {visible.map((opt) =>
              renaming === opt ? (
                <div key={opt} className="flex items-center gap-1 px-2 py-1">
                  <input
                    autoFocus
                    value={renameDraft}
                    onChange={(e) => setRenameDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitRename(opt);
                      if (e.key === "Escape") setRenaming(null);
                    }}
                    className="flex-1 px-2 py-1 text-xs rounded border border-violet-400 bg-white dark:bg-zinc-950 text-zinc-800 dark:text-zinc-200"
                  />
                  <button
                    onClick={() => commitRename(opt)}
                    className="px-1.5 py-1 text-xs text-violet-500 hover:text-violet-600"
                    title="Rename everywhere"
                  >
                    ✓
                  </button>
                  <button
                    onClick={() => setRenaming(null)}
                    className="px-1.5 py-1 text-xs text-zinc-400 hover:text-zinc-600"
                    title="Cancel"
                  >
                    ✕
                  </button>
                </div>
              ) : (
                <div
                  key={opt}
                  className="group/opt flex items-center gap-1 px-1 hover:bg-zinc-50 dark:hover:bg-zinc-800"
                >
                  <button
                    onClick={() => choose(opt)}
                    className="flex-1 flex items-center gap-2 px-2 py-1.5 text-left text-xs text-zinc-800 dark:text-zinc-200"
                  >
                    <span className="flex-1 truncate">{opt}</span>
                    {opt === value && <span className="text-violet-500">✓</span>}
                  </button>
                  <button
                    onClick={() => { setRenaming(opt); setRenameDraft(opt); }}
                    className="opacity-0 group-hover/opt:opacity-100 px-1.5 py-1 text-[10px] text-zinc-400 hover:text-violet-500"
                    title={`Rename "${opt}" on every calendar`}
                  >
                    ✎
                  </button>
                </div>
              ),
            )}
            {visible.length === 0 && !canCreate && (
              <p className="px-3 py-2 text-xs text-zinc-500">No sources match.</p>
            )}
            {canCreate && (
              <button
                onClick={() => choose(search.trim())}
                className="w-full px-3 py-1.5 text-left text-xs text-violet-500 hover:bg-zinc-50 dark:hover:bg-zinc-800"
              >
                Create &ldquo;{search.trim()}&rdquo;
              </button>
            )}
          </div>

          <div className="border-t border-zinc-100 dark:border-zinc-800">
            <button
              onClick={() => choose(null)}
              className="w-full px-3 py-1.5 text-left text-xs text-zinc-500 hover:bg-zinc-50 dark:hover:bg-zinc-800"
            >
              — No source
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
