"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchStatisticsSegments,
  type SegmentFunnelResponse,
  type SegmentFunnelRow,
  type SegmentFunnelWebinar,
} from "@/lib/api";
import { RecomputeControl } from "./RecomputeControl";

/* ── Formatting ─────────────────────────────────────────────────────────── */

function fmtInt(n: number): string {
  return n.toLocaleString();
}

/** Ratio (0–1) → percent string. Small rates (<10%) get 2 decimals so values
 * like Reg% (0.49%) and Att% of invites (0.07%) stay legible; larger rates get
 * 1 decimal, matching the reference layout. */
function fmtPct(ratio: number | null): string {
  if (ratio === null) return "—";
  const v = ratio * 100;
  return `${v < 10 ? v.toFixed(2) : v.toFixed(1)}%`;
}

function fmtPer1k(ratio: number | null): string {
  if (ratio === null) return "—";
  return ratio.toFixed(2);
}

function safeDiv(a: number, b: number): number | null {
  return b > 0 ? a / b : null;
}

function safePer1k(a: number, b: number): number | null {
  return b > 0 ? a / (b / 1000) : null;
}

/* ── Derived funnel cells (recomputed from summed raw counts) ────────────── */

type FunnelCells = {
  invites: number;
  regs: number;
  regPct: number | null;
  attendees10m: number;
  attOfInv: number | null;
  attOfReg: number | null;
  bookings: number;
  bookOfAtt: number | null;
  bookPer1kInv: number | null;
};

function deriveCells(r: SegmentFunnelRow): FunnelCells {
  return {
    invites: r.invites,
    regs: r.regs,
    regPct: safeDiv(r.regs, r.invites),
    attendees10m: r.attendees10m,
    attOfInv: safeDiv(r.attendees10m, r.invites),
    attOfReg: safeDiv(r.attendees10m, r.regs),
    bookings: r.bookings,
    bookOfAtt: safeDiv(r.bookings, r.attendees10m),
    bookPer1kInv: safePer1k(r.bookings, r.invites),
  };
}

/* ── Tab ────────────────────────────────────────────────────────────────── */

export function SegmentsTab() {
  const [data, setData] = useState<SegmentFunnelResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The committed webinar selection driving the currently displayed data.
  // Empty until the first load resolves, then initialized to "all".
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const load = useCallback(async (ids: string[] | null, isRefresh: boolean) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const d = await fetchStatisticsSegments(ids);
      setData(d);
      // On first load (selection still empty) adopt the full set as selected.
      setSelected((prev) =>
        prev.size === 0 ? new Set(d.includedWebinarIds) : prev,
      );
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load(null, false);
  }, [load]);

  const allIds = useMemo(
    () => (data ? data.webinars.map((w) => w.webinarId) : []),
    [data],
  );

  const applySelection = useCallback(
    (ids: Set<string>) => {
      setSelected(ids);
      // All selected → pass null so the backend includes everything.
      const isAll = ids.size === allIds.length;
      load(isAll ? null : Array.from(ids), true);
    },
    [allIds.length, load],
  );

  const refresh = useCallback(() => {
    const isAll = selected.size === allIds.length || selected.size === 0;
    load(isAll ? null : Array.from(selected), true);
  }, [selected, allIds.length, load]);

  if (loading) {
    return <div className="px-6 py-5 text-xs text-zinc-500">Loading…</div>;
  }
  if (error) {
    return (
      <div className="px-6 py-5">
        <div className="px-3 py-2 rounded-md bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
          {error}
        </div>
      </div>
    );
  }
  if (!data) return null;

  const rangeLabel = webinarRangeLabel(data.webinars, selected);

  return (
    <div className="flex flex-col h-full">
      <div className="flex-none px-6 pt-5 pb-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              Bookings Funnel by Segment{rangeLabel ? ` — ${rangeLabel}` : ""}
            </h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              High-level funnel rolled up by bucket across the selected webinars.
              Percentages are computed from summed totals, not averaged per-webinar rates.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <RecomputeControl onDone={refresh} />
            <WebinarMultiSelect
              options={data.webinars}
              selectedIds={selected}
              onApply={applySelection}
            />
            <button
              onClick={refresh}
              disabled={refreshing}
              className="px-3 py-1.5 text-xs rounded-lg bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-200 disabled:opacity-50"
            >
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>
      </div>

      {data.webinars.length === 0 ? (
        <div className="mx-6 mt-6 mb-6 text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
          No passed webinars with statistics yet.
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-auto px-6 pb-6">
          {data.pendingWebinarIds.length > 0 && (
            <div className="mt-2 mb-3 px-3 py-2 rounded-md bg-amber-500/10 border border-amber-500/30 text-amber-600 dark:text-amber-400 text-xs">
              {data.pendingWebinarIds.length} of {data.includedWebinarIds.length}{" "}
              selected webinar{data.includedWebinarIds.length === 1 ? "" : "s"} not computed yet
              {" "}— their numbers are excluded from the totals below. Click{" "}
              <span className="font-semibold">Recompute now</span> to build them.
            </div>
          )}
          <FunnelTable
            segments={data.segments}
            totals={data.totals}
            includedCount={data.includedWebinarIds.length - data.pendingWebinarIds.length}
          />
        </div>
      )}
    </div>
  );
}

/** "(W136–W145)" for the selected webinars, or "" when none/unknown. */
function webinarRangeLabel(
  webinars: SegmentFunnelWebinar[],
  selected: Set<string>,
): string {
  const nums = webinars
    .filter((w) => selected.size === 0 || selected.has(w.webinarId))
    .map((w) => w.number)
    .filter((n) => typeof n === "number");
  if (nums.length === 0) return "";
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  return min === max ? `(W${min})` : `(W${min}–W${max})`;
}

/* ── Funnel table ───────────────────────────────────────────────────────── */

const COL = "px-3 py-2 text-right tabular-nums whitespace-nowrap";
const HEAD =
  "px-3 py-2 text-right font-semibold text-zinc-500 dark:text-zinc-500 whitespace-nowrap";

function FunnelTable({
  segments,
  totals,
  includedCount,
}: {
  segments: SegmentFunnelRow[];
  totals: SegmentFunnelRow;
  includedCount: number;
}) {
  // Named bucket rows lead the column-leader comparison; the "Other (no bucket)"
  // row (bucketId === null) and the Total row are excluded so a catch-all bucket
  // doesn't win every column.
  const namedCells = useMemo(
    () => segments.filter((s) => s.bucketId !== null).map(deriveCells),
    [segments],
  );

  const maxes = useMemo(() => {
    const keys: (keyof FunnelCells)[] = [
      "invites", "regs", "regPct", "attendees10m",
      "attOfInv", "attOfReg", "bookings", "bookOfAtt", "bookPer1kInv",
    ];
    const m = {} as Record<keyof FunnelCells, number>;
    for (const k of keys) {
      let best = -Infinity;
      for (const c of namedCells) {
        const v = c[k];
        if (v !== null && v > best) best = v;
      }
      m[k] = best;
    }
    return m;
  }, [namedCells]);

  if (segments.length === 0) {
    return (
      <div className="mt-4 text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
        No segment data for the selected webinars.
      </div>
    );
  }

  return (
    <div className="mt-2 border border-zinc-200 dark:border-zinc-800 rounded-lg overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead className="bg-zinc-50 dark:bg-zinc-900 text-[11px] uppercase tracking-wider">
            <tr>
              <th className="px-3 py-2 text-left font-semibold text-zinc-500 dark:text-zinc-500 min-w-[220px]">
                Segment
              </th>
              <th className={HEAD}>Invites</th>
              <th className={HEAD}>Regs</th>
              <th className={HEAD}>Reg%</th>
              <th className={HEAD}>Attendees (10m+)</th>
              <th className={HEAD} title="10-min+ attendees ÷ invites">Att% (of inv)</th>
              <th className={HEAD} title="10-min+ attendees ÷ registrations">Att% (of reg)</th>
              <th className={HEAD}>Bookings</th>
              <th className={HEAD} title="Bookings ÷ 10-min+ attendees">Book% (of att)</th>
              <th className={HEAD} title="Bookings per 1,000 invites">Book/1k inv</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
            {segments.map((s, i) => (
              <SegmentRow
                key={s.bucketId ?? `other-${i}`}
                row={s}
                maxes={maxes}
                isOther={s.bucketId === null}
              />
            ))}
          </tbody>
          <tfoot>
            <TotalsRow totals={totals} includedCount={includedCount} />
          </tfoot>
        </table>
      </div>
    </div>
  );
}

function leaderCls(value: number | null, max: number): string {
  return value !== null && value === max && Number.isFinite(max)
    ? "font-bold text-zinc-900 dark:text-zinc-100"
    : "text-zinc-700 dark:text-zinc-300";
}

function SegmentRow({
  row,
  maxes,
  isOther,
}: {
  row: SegmentFunnelRow;
  maxes: Record<keyof FunnelCells, number>;
  isOther: boolean;
}) {
  const c = deriveCells(row);
  return (
    <tr className="bg-white dark:bg-zinc-950 hover:bg-zinc-50 dark:hover:bg-zinc-900/60">
      <td
        className={`px-3 py-2 text-left ${
          isOther
            ? "text-zinc-500 italic"
            : "text-zinc-800 dark:text-zinc-200 font-medium"
        }`}
        title={row.bucketName ?? ""}
      >
        {row.bucketName ?? "—"}
      </td>
      <td className={`${COL} ${leaderCls(c.invites, maxes.invites)}`}>{fmtInt(c.invites)}</td>
      <td className={`${COL} ${leaderCls(c.regs, maxes.regs)}`}>{fmtInt(c.regs)}</td>
      <td className={`${COL} ${leaderCls(c.regPct, maxes.regPct)}`}>{fmtPct(c.regPct)}</td>
      <td className={`${COL} ${leaderCls(c.attendees10m, maxes.attendees10m)}`}>{fmtInt(c.attendees10m)}</td>
      <td className={`${COL} ${leaderCls(c.attOfInv, maxes.attOfInv)}`}>{fmtPct(c.attOfInv)}</td>
      <td className={`${COL} ${leaderCls(c.attOfReg, maxes.attOfReg)}`}>{fmtPct(c.attOfReg)}</td>
      <td className={`${COL} ${leaderCls(c.bookings, maxes.bookings)}`}>{fmtInt(c.bookings)}</td>
      <td className={`${COL} ${leaderCls(c.bookOfAtt, maxes.bookOfAtt)}`}>{fmtPct(c.bookOfAtt)}</td>
      <td className={`${COL} ${leaderCls(c.bookPer1kInv, maxes.bookPer1kInv)}`}>{fmtPer1k(c.bookPer1kInv)}</td>
    </tr>
  );
}

function TotalsRow({
  totals,
  includedCount,
}: {
  totals: SegmentFunnelRow;
  includedCount: number;
}) {
  const c = deriveCells(totals);
  const cls = "px-3 py-2 text-right tabular-nums font-bold text-zinc-900 dark:text-zinc-100 whitespace-nowrap";
  return (
    <tr className="bg-zinc-100 dark:bg-zinc-900 border-t-2 border-zinc-300 dark:border-zinc-700">
      <td className="px-3 py-2 text-left font-bold text-zinc-900 dark:text-zinc-100">
        Total
        <span className="ml-2 font-normal text-[11px] text-zinc-500">
          {includedCount} webinar{includedCount === 1 ? "" : "s"}
        </span>
      </td>
      <td className={cls}>{fmtInt(c.invites)}</td>
      <td className={cls}>{fmtInt(c.regs)}</td>
      <td className={cls}>{fmtPct(c.regPct)}</td>
      <td className={cls}>{fmtInt(c.attendees10m)}</td>
      <td className={cls}>{fmtPct(c.attOfInv)}</td>
      <td className={cls}>{fmtPct(c.attOfReg)}</td>
      <td className={cls}>{fmtInt(c.bookings)}</td>
      <td className={cls}>{fmtPct(c.bookOfAtt)}</td>
      <td className={cls}>{fmtPer1k(c.bookPer1kInv)}</td>
    </tr>
  );
}

/* ── Webinar multi-select (popover with checkboxes) ─────────────────────── */

function WebinarMultiSelect({
  options,
  selectedIds,
  onApply,
}: {
  options: SegmentFunnelWebinar[];
  selectedIds: Set<string>;
  onApply: (ids: Set<string>) => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Set<string>>(selectedIds);
  const ref = useRef<HTMLDivElement>(null);

  // Re-seed the draft from the committed selection when opening, then toggle.
  const toggleOpen = () => {
    if (!open) setDraft(new Set(selectedIds));
    setOpen((o) => !o);
  };

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const allSelected = selectedIds.size === options.length;
  const label = allSelected
    ? `All webinars (${options.length})`
    : `${selectedIds.size} of ${options.length} webinars`;

  const toggle = (id: string) =>
    setDraft((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const dirty =
    draft.size !== selectedIds.size ||
    Array.from(draft).some((id) => !selectedIds.has(id));

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={toggleOpen}
        className="px-3 py-1.5 text-xs rounded-lg bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-200 inline-flex items-center gap-1.5"
      >
        <span className="font-semibold">Webinars:</span> {label}
        <span className="text-zinc-400 dark:text-zinc-500">▾</span>
      </button>

      {open && (
        <div className="absolute right-0 mt-1 z-50 w-72 rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 shadow-xl">
          <div className="flex items-center justify-between px-3 py-2 border-b border-zinc-200 dark:border-zinc-800">
            <button
              onClick={() => setDraft(new Set(options.map((o) => o.webinarId)))}
              className="text-[11px] text-violet-500 hover:underline"
            >
              Select all
            </button>
            <button
              onClick={() => setDraft(new Set())}
              className="text-[11px] text-zinc-500 hover:underline"
            >
              Clear
            </button>
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {options.map((w) => {
              const checked = draft.has(w.webinarId);
              return (
                <label
                  key={w.webinarId}
                  className="flex items-center gap-2 px-3 py-1.5 text-xs cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-800/60"
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(w.webinarId)}
                    className="accent-violet-500"
                  />
                  <span className="text-zinc-700 dark:text-zinc-200 truncate" title={w.title ?? w.label}>
                    {w.label}
                  </span>
                </label>
              );
            })}
          </div>
          <div className="flex items-center justify-between px-3 py-2 border-t border-zinc-200 dark:border-zinc-800">
            <span className="text-[11px] text-zinc-500">{draft.size} selected</span>
            <button
              onClick={() => {
                onApply(new Set(draft));
                setOpen(false);
              }}
              disabled={draft.size === 0 || !dirty}
              className="px-3 py-1 text-xs rounded-md bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Apply
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
