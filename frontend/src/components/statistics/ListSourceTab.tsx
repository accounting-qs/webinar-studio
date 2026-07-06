"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchStatisticsListSource,
  type SegmentFunnelWebinar,
  type SourceFunnelResponse,
  type SourceFunnelRow,
} from "@/lib/api";
import { RecomputeControl } from "./RecomputeControl";

/* ── Formatting ─────────────────────────────────────────────────────────── */

function fmtInt(n: number): string {
  return n.toLocaleString();
}

/** Ratio (0–1) → percent string. Small rates (<10%) get 2 decimals so values
 * like Reg% (0.79%) stay legible; larger rates get 1 decimal. */
function fmtPct(ratio: number | null): string {
  if (ratio === null) return "—";
  const v = ratio * 100;
  return `${v < 10 ? v.toFixed(2) : v.toFixed(1)}%`;
}

function fmtPer1k(ratio: number | null): string {
  return ratio === null ? "—" : ratio.toFixed(2);
}

function safeDiv(a: number, b: number): number | null {
  return b > 0 ? a / b : null;
}

function safePer1k(a: number, b: number): number | null {
  return b > 0 ? a / (b / 1000) : null;
}

const MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2025-11" → "Nov 2025"; passes through "(undated)" and anything unexpected. */
function fmtVintage(v: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(v);
  if (!m) return v;
  return `${MONTH_ABBR[parseInt(m[2], 10)] ?? m[2]} ${m[1]}`;
}

/* ── Grouping (Provider ⇄ Vintage) ──────────────────────────────────────── */

type GroupMode = "source" | "vintage";

type Funnel = { invites: number; regs: number; attendees10m: number; bookings: number };
type FlatCell = Funnel & { source: string; vintage: string };
type ChildRow = Funnel & { key: string };
type GroupRow = Funnel & { key: string; children: ChildRow[] };

/** bySource rows (source → nested vintages) → flat (source, vintage) cells. */
function flattenCells(bySource: SourceFunnelRow[]): FlatCell[] {
  const cells: FlatCell[] = [];
  for (const s of bySource) {
    for (const v of s.vintages) {
      cells.push({
        source: s.source,
        vintage: v.vintage,
        invites: v.invites,
        regs: v.regs,
        attendees10m: v.attendees10m,
        bookings: v.bookings,
      });
    }
  }
  return cells;
}

/** Pivot flat cells by the chosen primary dimension, nesting the other as
 * children. Both parents and children are sorted by invites desc. */
function pivot(cells: FlatCell[], primary: GroupMode): GroupRow[] {
  const secondary: GroupMode = primary === "source" ? "vintage" : "source";
  const groups = new Map<string, GroupRow>();
  for (const c of cells) {
    const pk = c[primary];
    let g = groups.get(pk);
    if (!g) {
      g = { key: pk, invites: 0, regs: 0, attendees10m: 0, bookings: 0, children: [] };
      groups.set(pk, g);
    }
    g.invites += c.invites;
    g.regs += c.regs;
    g.attendees10m += c.attendees10m;
    g.bookings += c.bookings;
    g.children.push({
      key: c[secondary],
      invites: c.invites,
      regs: c.regs,
      attendees10m: c.attendees10m,
      bookings: c.bookings,
    });
  }
  const rows = Array.from(groups.values());
  rows.sort((a, b) => b.invites - a.invites);
  for (const r of rows) r.children.sort((a, b) => b.invites - a.invites);
  return rows;
}

/* ── Derived funnel cells (from summed raw counts) ───────────────────────── */

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

function deriveCells(r: Funnel): FunnelCells {
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

export function ListSourceTab() {
  const [data, setData] = useState<SourceFunnelResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<GroupMode>("source");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const load = useCallback(async (ids: string[] | null, isRefresh: boolean) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const d = await fetchStatisticsListSource(ids);
      setData(d);
      setSelected((prev) => (prev.size === 0 ? new Set(d.includedWebinarIds) : prev));
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

  const allIds = useMemo(() => (data ? data.webinars.map((w) => w.webinarId) : []), [data]);

  const applySelection = useCallback(
    (ids: Set<string>) => {
      setSelected(ids);
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
  const includedCount = data.includedWebinarIds.length - data.pendingWebinarIds.length;

  return (
    <div className="flex flex-col h-full">
      <div className="flex-none px-6 pt-5 pb-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              Bookings Funnel by List Source{rangeLabel ? ` — ${rangeLabel}` : ""}
            </h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              Cold outreach rolled up by data source (parsed from the lead list name), across the
              selected webinars. Nonjoiners / no-list-data are excluded. Percentages are computed
              from summed totals, not averaged per-webinar rates.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <GroupingToggle mode={mode} onChange={setMode} />
            <RecomputeControl onDone={refresh} />
            <WebinarMultiSelect options={data.webinars} selectedIds={selected} onApply={applySelection} />
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
              {data.pendingWebinarIds.length} of {data.includedWebinarIds.length} selected webinar
              {data.includedWebinarIds.length === 1 ? "" : "s"} not computed yet — excluded from the
              totals below. Click <span className="font-semibold">Recompute now</span> to build them.
            </div>
          )}

          {/* Aggregate across all selected webinars. */}
          <SectionHeading
            title={`All selected webinars — by ${mode === "source" ? "source" : "list age"}`}
            subtitle={`${includedCount} webinar${includedCount === 1 ? "" : "s"} combined`}
          />
          <GroupedFunnelTable bySource={data.bySource} mode={mode} totals={data.totals} />

          {/* Per-webinar breakdown, newest first. */}
          {data.perWebinar.length > 0 && (
            <div className="mt-8">
              <SectionHeading title="Per webinar" subtitle={`Broken down by ${mode === "source" ? "source" : "list age"}`} />
              <div className="flex flex-col gap-6">
                {data.perWebinar.map((w) => (
                  <div key={w.webinarId}>
                    <div className="text-xs font-semibold text-zinc-700 dark:text-zinc-300 mb-1.5">
                      {w.label ?? `W${w.number ?? "?"}`}
                    </div>
                    <GroupedFunnelTable bySource={w.bySource} mode={mode} />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SectionHeading({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mt-2 mb-1.5">
      <span className="text-xs font-semibold text-zinc-800 dark:text-zinc-200">{title}</span>
      {subtitle && <span className="ml-2 text-[11px] text-zinc-500">{subtitle}</span>}
    </div>
  );
}

/** "(W144–W146)" for the selected webinars, or "" when none/unknown. */
function webinarRangeLabel(webinars: SegmentFunnelWebinar[], selected: Set<string>): string {
  const nums = webinars
    .filter((w) => selected.size === 0 || selected.has(w.webinarId))
    .map((w) => w.number)
    .filter((n) => typeof n === "number");
  if (nums.length === 0) return "";
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  return min === max ? `(W${min})` : `(W${min}–W${max})`;
}

/* ── Grouping toggle ────────────────────────────────────────────────────── */

function GroupingToggle({ mode, onChange }: { mode: GroupMode; onChange: (m: GroupMode) => void }) {
  const opt = (m: GroupMode, label: string) => (
    <button
      onClick={() => onChange(m)}
      className={`px-2.5 py-1 text-xs font-semibold rounded-md transition-colors ${
        mode === m
          ? "bg-white dark:bg-zinc-700 text-violet-600 dark:text-violet-300 shadow-sm"
          : "text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
      }`}
    >
      {label}
    </button>
  );
  return (
    <div className="inline-flex items-center gap-0.5 rounded-lg bg-zinc-100 dark:bg-zinc-800 p-0.5" title="Group rows by">
      {opt("source", "Provider")}
      {opt("vintage", "List age")}
    </div>
  );
}

/* ── Funnel table (grouped, expandable) ─────────────────────────────────── */

const COL = "px-3 py-2 text-right tabular-nums whitespace-nowrap";

type CellKey = keyof FunnelCells;
type SortKey = "group" | CellKey;
type SortDir = "asc" | "desc";

const NUMERIC_COLUMNS: { key: CellKey; label: string; title?: string; fmt: (c: FunnelCells) => string }[] = [
  { key: "invites", label: "Leads", title: "Distinct contacts mailed from this source", fmt: (c) => fmtInt(c.invites) },
  { key: "regs", label: "Regs", fmt: (c) => fmtInt(c.regs) },
  { key: "regPct", label: "Reg%", fmt: (c) => fmtPct(c.regPct) },
  { key: "attendees10m", label: "Attendees (10m+)", fmt: (c) => fmtInt(c.attendees10m) },
  { key: "attOfInv", label: "Att% (of leads)", title: "10-min+ attendees ÷ leads", fmt: (c) => fmtPct(c.attOfInv) },
  { key: "attOfReg", label: "Att% (of reg)", title: "10-min+ attendees ÷ registrations", fmt: (c) => fmtPct(c.attOfReg) },
  { key: "bookings", label: "Bookings", fmt: (c) => fmtInt(c.bookings) },
  { key: "bookOfAtt", label: "Book% (of att)", title: "Bookings ÷ 10-min+ attendees", fmt: (c) => fmtPct(c.bookOfAtt) },
  { key: "bookPer1kInv", label: "Book/1k leads", title: "Bookings per 1,000 leads", fmt: (c) => fmtPer1k(c.bookPer1kInv) },
];

function SortArrow({ active, dir }: { active: boolean; dir: SortDir }) {
  if (!active) return <span className="text-zinc-400 dark:text-zinc-600 text-[10px]">↕</span>;
  return <span className="text-violet-500 text-[10px]">{dir === "asc" ? "↑" : "↓"}</span>;
}

function GroupedFunnelTable({
  bySource,
  mode,
  totals,
}: {
  bySource: SourceFunnelRow[];
  mode: GroupMode;
  totals?: SourceFunnelResponse["totals"];
}) {
  const [sortKey, setSortKey] = useState<SortKey>("invites");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const rows = useMemo(() => pivot(flattenCells(bySource), mode), [bySource, mode]);
  const groupLabel = mode === "source" ? "Source" : "List age";
  const childIsVintage = mode === "source";

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "group" ? "asc" : "desc");
    }
  };

  const toggleExpand = (key: string) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });

  // Per-column min/max across the parent rows — drives the heatmap + leader bold.
  const colStats = useMemo(() => {
    const parentCells = rows.map((r) => deriveCells(r));
    const s = {} as Record<CellKey, { min: number; max: number }>;
    for (const col of NUMERIC_COLUMNS) {
      let min = Infinity;
      let max = -Infinity;
      for (const c of parentCells) {
        const v = c[col.key];
        if (v !== null) {
          if (v < min) min = v;
          if (v > max) max = v;
        }
      }
      s[col.key] = { min, max };
    }
    return s;
  }, [rows]);

  const sortedRows = useMemo(() => {
    const decorated = rows.map((row) => ({ row, cells: deriveCells(row) }));
    decorated.sort((a, b) => {
      let cmp: number;
      if (sortKey === "group") {
        cmp = a.row.key.localeCompare(b.row.key);
      } else {
        const av = a.cells[sortKey];
        const bv = b.cells[sortKey];
        if (av === null && bv === null) return 0;
        if (av === null) return 1;
        if (bv === null) return -1;
        cmp = av - bv;
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return decorated.map((d) => d.row);
  }, [rows, sortKey, sortDir]);

  if (rows.length === 0) {
    return (
      <div className="mt-1 text-xs text-zinc-500 py-6 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
        No cold-list source data for the selected webinars.
      </div>
    );
  }

  const headBase =
    "sticky top-0 z-10 bg-zinc-50 dark:bg-zinc-900 px-3 py-2 font-semibold text-zinc-500 dark:text-zinc-500 whitespace-nowrap cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800 shadow-[inset_0_-1px_0_#e4e4e7] dark:shadow-[inset_0_-1px_0_#27272a]";

  return (
    <div className="overflow-auto border border-zinc-200 dark:border-zinc-800 rounded-lg">
      <table className="w-full text-xs border-collapse">
        <thead className="text-[11px] uppercase tracking-wider">
          <tr>
            <th onClick={() => handleSort("group")} className={`${headBase} text-left min-w-[200px]`}>
              <span className="inline-flex items-center gap-1">
                {groupLabel}
                <SortArrow active={sortKey === "group"} dir={sortDir} />
              </span>
            </th>
            {NUMERIC_COLUMNS.map((col) => (
              <th key={col.key} onClick={() => handleSort(col.key)} title={col.title} className={`${headBase} text-right`}>
                <span className="inline-flex items-center justify-end gap-1">
                  {col.label}
                  <SortArrow active={sortKey === col.key} dir={sortDir} />
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {sortedRows.map((row) => {
            const isOpen = expanded.has(row.key);
            const label = mode === "vintage" ? fmtVintage(row.key) : row.key;
            return (
              <FunnelRows
                key={row.key}
                row={row}
                label={label}
                isOpen={isOpen}
                onToggle={() => toggleExpand(row.key)}
                colStats={colStats}
                childIsVintage={childIsVintage}
              />
            );
          })}
        </tbody>
        {totals && (
          <tfoot>
            <TotalsRow totals={totals} />
          </tfoot>
        )}
      </table>
    </div>
  );
}

function leaderCls(value: number | null, max: number): string {
  return value !== null && value === max && Number.isFinite(max)
    ? "font-bold text-zinc-900 dark:text-zinc-100"
    : "text-zinc-700 dark:text-zinc-300";
}

/** Red→amber→green heat background positioned by where the value falls in the
 * column's [min,max] (higher = greener, for every metric). Translucent so it
 * reads on both themes; undefined when there's nothing to scale. */
function heatBg(value: number | null, min: number, max: number): string | undefined {
  if (value === null || !Number.isFinite(min) || !Number.isFinite(max) || max <= min) return undefined;
  const t = (value - min) / (max - min);
  const stops: [number, number, number][] = [
    [239, 68, 68],
    [245, 158, 11],
    [34, 197, 94],
  ];
  const i = t < 0.5 ? 0 : 1;
  const lt = t < 0.5 ? t / 0.5 : (t - 0.5) / 0.5;
  const a = stops[i];
  const b = stops[i + 1];
  const r = Math.round(a[0] + (b[0] - a[0]) * lt);
  const g = Math.round(a[1] + (b[1] - a[1]) * lt);
  const bl = Math.round(a[2] + (b[2] - a[2]) * lt);
  const alpha = 0.16 + 0.24 * Math.abs(2 * t - 1);
  return `rgba(${r}, ${g}, ${bl}, ${alpha.toFixed(3)})`;
}

/** A parent (source/vintage) row plus its expandable children (the other dim). */
function FunnelRows({
  row,
  label,
  isOpen,
  onToggle,
  colStats,
  childIsVintage,
}: {
  row: GroupRow;
  label: string;
  isOpen: boolean;
  onToggle: () => void;
  colStats: Record<CellKey, { min: number; max: number }>;
  childIsVintage: boolean;
}) {
  const c = deriveCells(row);
  const canExpand = row.children.length > 0;
  return (
    <>
      <tr className="bg-white dark:bg-zinc-950 hover:bg-zinc-50 dark:hover:bg-zinc-900/60">
        <td className="px-3 py-2 text-left text-zinc-800 dark:text-zinc-200 font-medium">
          <button
            onClick={onToggle}
            disabled={!canExpand}
            className="inline-flex items-center gap-1.5 disabled:cursor-default"
            title={canExpand ? (isOpen ? "Collapse" : "Expand") : undefined}
          >
            {canExpand ? (
              <span className="text-zinc-400 dark:text-zinc-500 text-[10px] w-2.5 inline-block">{isOpen ? "▾" : "▸"}</span>
            ) : (
              <span className="w-2.5 inline-block" />
            )}
            <span className="truncate">{label}</span>
          </button>
        </td>
        {NUMERIC_COLUMNS.map((col) => {
          const v = c[col.key];
          const { min, max } = colStats[col.key];
          const bg = heatBg(v, min, max);
          return (
            <td key={col.key} style={bg ? { backgroundColor: bg } : undefined} className={`${COL} ${leaderCls(v, max)}`}>
              {col.fmt(c)}
            </td>
          );
        })}
      </tr>
      {isOpen &&
        row.children.map((child) => {
          const cc = deriveCells(child);
          const childLabel = childIsVintage ? fmtVintage(child.key) : child.key;
          return (
            <tr key={child.key} className="bg-zinc-50/60 dark:bg-zinc-900/40">
              <td className="px-3 py-1.5 pl-9 text-left text-zinc-500 dark:text-zinc-400 text-[11px]" title={childLabel}>
                <span className="truncate">{childLabel}</span>
              </td>
              {NUMERIC_COLUMNS.map((col) => (
                <td key={col.key} className={`${COL} text-zinc-500 dark:text-zinc-400 text-[11px]`}>
                  {col.fmt(cc)}
                </td>
              ))}
            </tr>
          );
        })}
    </>
  );
}

function TotalsRow({ totals }: { totals: SourceFunnelResponse["totals"] }) {
  const c = deriveCells(totals);
  const cls = "px-3 py-2 text-right tabular-nums font-bold text-zinc-900 dark:text-zinc-100 whitespace-nowrap";
  return (
    <tr className="bg-zinc-100 dark:bg-zinc-900 border-t-2 border-zinc-300 dark:border-zinc-700">
      <td className="px-3 py-2 text-left font-bold text-zinc-900 dark:text-zinc-100">Total</td>
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
  const label = allSelected ? `All webinars (${options.length})` : `${selectedIds.size} of ${options.length} webinars`;

  const toggle = (id: string) =>
    setDraft((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const dirty = draft.size !== selectedIds.size || Array.from(draft).some((id) => !selectedIds.has(id));

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
            <button onClick={() => setDraft(new Set())} className="text-[11px] text-zinc-500 hover:underline">
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
                  <input type="checkbox" checked={checked} onChange={() => toggle(w.webinarId)} className="accent-violet-500" />
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
