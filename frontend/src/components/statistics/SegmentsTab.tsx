"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchStatisticsSegments,
  updateBucketQuality,
  type BucketQuality,
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

  // Set/clear a bucket's quality mark. Optimistically patch the row in place so
  // the change is instant, then persist; on failure surface the error and
  // reload to resync with the server.
  const setSegmentQuality = useCallback(
    async (bucketId: string, quality: BucketQuality | null) => {
      setData((prev) =>
        prev
          ? {
              ...prev,
              segments: prev.segments.map((s) =>
                s.bucketId === bucketId ? { ...s, quality } : s,
              ),
            }
          : prev,
      );
      try {
        await updateBucketQuality(bucketId, quality);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        refresh();
      }
    },
    [refresh],
  );

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
        <div className="flex-1 min-h-0 flex flex-col px-6 pb-6">
          {data.pendingWebinarIds.length > 0 && (
            <div className="shrink-0 mt-2 mb-3 px-3 py-2 rounded-md bg-amber-500/10 border border-amber-500/30 text-amber-600 dark:text-amber-400 text-xs">
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
            onSetQuality={setSegmentQuality}
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

type CellKey = keyof FunnelCells;
type SortKey = "segment" | CellKey;
type SortDir = "asc" | "desc";

/** Single source of truth for the numeric columns — drives the header, the body
 * cells, the totals row, and the sort keys so they can never drift apart. */
const NUMERIC_COLUMNS: {
  key: CellKey;
  label: string;
  title?: string;
  fmt: (c: FunnelCells) => string;
}[] = [
  { key: "invites", label: "Invites", fmt: (c) => fmtInt(c.invites) },
  { key: "regs", label: "Regs", fmt: (c) => fmtInt(c.regs) },
  { key: "regPct", label: "Reg%", fmt: (c) => fmtPct(c.regPct) },
  { key: "attendees10m", label: "Attendees (10m+)", fmt: (c) => fmtInt(c.attendees10m) },
  { key: "attOfInv", label: "Att% (of inv)", title: "10-min+ attendees ÷ invites", fmt: (c) => fmtPct(c.attOfInv) },
  { key: "attOfReg", label: "Att% (of reg)", title: "10-min+ attendees ÷ registrations", fmt: (c) => fmtPct(c.attOfReg) },
  { key: "bookings", label: "Bookings", fmt: (c) => fmtInt(c.bookings) },
  { key: "bookOfAtt", label: "Book% (of att)", title: "Bookings ÷ 10-min+ attendees", fmt: (c) => fmtPct(c.bookOfAtt) },
  { key: "bookPer1kInv", label: "Book/1k inv", title: "Bookings per 1,000 invites", fmt: (c) => fmtPer1k(c.bookPer1kInv) },
];

function SortArrow({ active, dir }: { active: boolean; dir: SortDir }) {
  if (!active) {
    return <span className="text-zinc-400 dark:text-zinc-600 text-[10px]">↕</span>;
  }
  return <span className="text-violet-500 text-[10px]">{dir === "asc" ? "↑" : "↓"}</span>;
}

function FunnelTable({
  segments,
  totals,
  includedCount,
  onSetQuality,
}: {
  segments: SegmentFunnelRow[];
  totals: SegmentFunnelRow;
  includedCount: number;
  onSetQuality: (bucketId: string, quality: BucketQuality | null) => void;
}) {
  // Default to invites desc — matches the server's initial ordering.
  const [sortKey, setSortKey] = useState<SortKey>("invites");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // Names default A→Z; numbers default high→low (most interesting first).
      setSortDir(key === "segment" ? "asc" : "desc");
    }
  };

  // Named bucket rows lead the column-leader comparison; the "Other (no bucket)"
  // row (bucketId === null) and the Total row are excluded so a catch-all bucket
  // doesn't win every column.
  const namedCells = useMemo(
    () => segments.filter((s) => s.bucketId !== null).map(deriveCells),
    [segments],
  );

  // Per-column min/max across the named bucket rows — drives both the
  // column-leader emphasis and the red→green heatmap. "Other"/Total are
  // excluded so a catch-all bucket doesn't skew the scale.
  const colStats = useMemo(() => {
    const s = {} as Record<CellKey, { min: number; max: number }>;
    for (const col of NUMERIC_COLUMNS) {
      let min = Infinity;
      let max = -Infinity;
      for (const c of namedCells) {
        const v = c[col.key];
        if (v !== null) {
          if (v < min) min = v;
          if (v > max) max = v;
        }
      }
      s[col.key] = { min, max };
    }
    return s;
  }, [namedCells]);

  // Sort the named bucket rows by the active column; the "Other (no bucket)" row
  // stays pinned at the bottom (it's a catch-all, like the Total row).
  const sortedNamed = useMemo(() => {
    const decorated = segments
      .filter((s) => s.bucketId !== null)
      .map((row) => ({ row, cells: deriveCells(row) }));
    decorated.sort((a, b) => {
      let cmp: number;
      if (sortKey === "segment") {
        cmp = (a.row.bucketName ?? "").localeCompare(b.row.bucketName ?? "");
      } else {
        const av = a.cells[sortKey];
        const bv = b.cells[sortKey];
        // Nulls (e.g. a % with a zero denominator) always sort last.
        if (av === null && bv === null) return 0;
        if (av === null) return 1;
        if (bv === null) return -1;
        cmp = av - bv;
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return decorated.map((d) => d.row);
  }, [segments, sortKey, sortDir]);

  const otherRows = useMemo(
    () => segments.filter((s) => s.bucketId === null),
    [segments],
  );

  if (segments.length === 0) {
    return (
      <div className="mt-4 text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
        No segment data for the selected webinars.
      </div>
    );
  }

  // Sticky header: the bordered box below is the vertical scroll container, and
  // each <th> pins to its top so the column labels stay visible while the rows
  // scroll inside the table (not the page). Solid bg + inset bottom-border keep
  // the header opaque and divided as rows pass under it.
  const headBase =
    "sticky top-0 z-10 bg-zinc-50 dark:bg-zinc-900 px-3 py-2 font-semibold text-zinc-500 dark:text-zinc-500 whitespace-nowrap cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800 shadow-[inset_0_-1px_0_#e4e4e7] dark:shadow-[inset_0_-1px_0_#27272a]";

  return (
    <div className="mt-2 flex-1 min-h-0 overflow-auto border border-zinc-200 dark:border-zinc-800 rounded-lg">
      <table className="w-full text-xs border-collapse">
        <thead className="text-[11px] uppercase tracking-wider">
          <tr>
            <th
              onClick={() => handleSort("segment")}
              className={`${headBase} text-left min-w-[220px]`}
            >
              <span className="inline-flex items-center gap-1">
                Segment
                <SortArrow active={sortKey === "segment"} dir={sortDir} />
              </span>
            </th>
            {NUMERIC_COLUMNS.map((col) => (
              <th
                key={col.key}
                onClick={() => handleSort(col.key)}
                title={col.title}
                className={`${headBase} text-right`}
              >
                <span className="inline-flex items-center justify-end gap-1">
                  {col.label}
                  <SortArrow active={sortKey === col.key} dir={sortDir} />
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {sortedNamed.map((s) => (
            <SegmentRow key={s.bucketId} row={s} colStats={colStats} isOther={false} onSetQuality={onSetQuality} />
          ))}
          {otherRows.map((s, i) => (
            <SegmentRow key={`other-${i}`} row={s} colStats={colStats} isOther />
          ))}
        </tbody>
        <tfoot>
          <TotalsRow totals={totals} includedCount={includedCount} />
        </tfoot>
      </table>
    </div>
  );
}

function leaderCls(value: number | null, max: number): string {
  return value !== null && value === max && Number.isFinite(max)
    ? "font-bold text-zinc-900 dark:text-zinc-100"
    : "text-zinc-700 dark:text-zinc-300";
}

/** Red→amber→green heat background for a cell, positioned by where its value
 * falls in the column's [min,max] (higher = greener, for every metric). Returns
 * a translucent rgba so it reads on both light and dark rows, or undefined when
 * there's nothing to scale (null value, or a single distinct value). */
function heatBg(value: number | null, min: number, max: number): string | undefined {
  if (value === null || !Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
    return undefined;
  }
  const t = (value - min) / (max - min); // 0 = worst (red), 1 = best (green)
  const stops: [number, number, number][] = [
    [239, 68, 68], // red-500
    [245, 158, 11], // amber-500
    [34, 197, 94], // green-500
  ];
  const i = t < 0.5 ? 0 : 1;
  const lt = t < 0.5 ? t / 0.5 : (t - 0.5) / 0.5;
  const a = stops[i];
  const b = stops[i + 1];
  const r = Math.round(a[0] + (b[0] - a[0]) * lt);
  const g = Math.round(a[1] + (b[1] - a[1]) * lt);
  const bl = Math.round(a[2] + (b[2] - a[2]) * lt);
  // Fainter near the middle, stronger at the best/worst extremes.
  const alpha = 0.16 + 0.24 * Math.abs(2 * t - 1);
  return `rgba(${r}, ${g}, ${bl}, ${alpha.toFixed(3)})`;
}

const QUALITY_META: Record<BucketQuality, { label: string; cls: string }> = {
  good: { label: "Good", cls: "text-emerald-600 dark:text-emerald-400 border-emerald-500/40" },
  medium: { label: "Medium", cls: "text-amber-600 dark:text-amber-400 border-amber-500/40" },
  bad: { label: "Bad", cls: "text-red-600 dark:text-red-400 border-red-500/40" },
};

/** Compact colored select to mark a segment good / medium / bad (or clear). The
 * chosen value tints the control; the mark persists on the bucket and shows up
 * on the Planning page's bucket picker. */
function QualitySelect({
  value,
  onChange,
}: {
  value: BucketQuality | null;
  onChange: (q: BucketQuality | null) => void;
}) {
  const meta = value ? QUALITY_META[value] : null;
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange((e.target.value || null) as BucketQuality | null)}
      title="Mark segment quality"
      className={`shrink-0 rounded border bg-white dark:bg-zinc-900 px-1.5 py-0.5 text-[11px] font-semibold cursor-pointer focus:outline-none focus:ring-1 focus:ring-violet-500 ${
        meta ? meta.cls : "text-zinc-400 border-zinc-300 dark:border-zinc-700"
      }`}
    >
      <option value="">— Mark —</option>
      <option value="good">Good</option>
      <option value="medium">Medium</option>
      <option value="bad">Bad</option>
    </select>
  );
}

function SegmentRow({
  row,
  colStats,
  isOther,
  onSetQuality,
}: {
  row: SegmentFunnelRow;
  colStats: Record<CellKey, { min: number; max: number }>;
  isOther: boolean;
  onSetQuality?: (bucketId: string, quality: BucketQuality | null) => void;
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
        <div className="flex items-center gap-2">
          <span className="truncate">{row.bucketName ?? "—"}</span>
          {!isOther && row.bucketId && onSetQuality && (
            <QualitySelect
              value={row.quality}
              onChange={(q) => onSetQuality(row.bucketId!, q)}
            />
          )}
        </div>
      </td>
      {NUMERIC_COLUMNS.map((col) => {
        const v = c[col.key];
        const { min, max } = colStats[col.key];
        // Heat only the named segment rows; the "Other (no bucket)" catch-all
        // isn't part of the comparison set.
        const bg = isOther ? undefined : heatBg(v, min, max);
        return (
          <td
            key={col.key}
            style={bg ? { backgroundColor: bg } : undefined}
            className={`${COL} ${leaderCls(v, max)}`}
          >
            {col.fmt(c)}
          </td>
        );
      })}
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
      {NUMERIC_COLUMNS.map((col) => (
        <td key={col.key} className={cls}>
          {col.fmt(c)}
        </td>
      ))}
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
