"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchStatisticsEmployeeCount,
  type SegmentFunnelWebinar,
  type EmployeeFunnelResponse,
  type EmployeeFunnelRow,
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

/* ── Rows (flat by company-size bucket) ──────────────────────────────────── */

/** Raw count fields carried on every bucket row (mirror the backend
 * SOURCE_FUNNEL_RAW_KEYS). Rates are derived from the sums. */
const FUNNEL_KEYS = [
  "invites", "regs", "attendees10m", "bookings",
  "confirmed", "shows", "noShows", "canceled", "won",
  "disqualified", "qualified",
  "leadQualityGreat", "leadQualityOk", "leadQualityBarelyPassable", "leadQualityBadDq",
] as const;
type FunnelKey = (typeof FUNNEL_KEYS)[number];
type Funnel = Record<FunnelKey, number>;
type BucketRow = Funnel & { bucket: string };

function funnelOf(x: Funnel): Funnel {
  const out = Object.fromEntries(FUNNEL_KEYS.map((k) => [k, 0])) as Funnel;
  for (const k of FUNNEL_KEYS) out[k] = x[k] ?? 0;
  return out;
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
  confirmed: number;
  shows: number;
  showPct: number | null;
  noShows: number;
  canceled: number;
  won: number;
  closeRate: number | null;
  disqualified: number;
  qualified: number;
  qualRate: number | null;
  leadQualityGreat: number;
  leadQualityOk: number;
  leadQualityBarelyPassable: number;
  leadQualityBadDq: number;
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
    confirmed: r.confirmed,
    shows: r.shows,
    showPct: safeDiv(r.shows, r.bookings),
    noShows: r.noShows,
    canceled: r.canceled,
    won: r.won,
    closeRate: safeDiv(r.won, r.shows),
    disqualified: r.disqualified,
    qualified: r.qualified,
    qualRate: safeDiv(r.qualified, r.shows),
    leadQualityGreat: r.leadQualityGreat,
    leadQualityOk: r.leadQualityOk,
    leadQualityBarelyPassable: r.leadQualityBarelyPassable,
    leadQualityBadDq: r.leadQualityBadDq,
  };
}

/* ── Tab ────────────────────────────────────────────────────────────────── */

export function EmployeeCountTab() {
  const [data, setData] = useState<EmployeeFunnelResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const load = useCallback(async (ids: string[] | null, isRefresh: boolean) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const d = await fetchStatisticsEmployeeCount(ids);
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
              Bookings Funnel by Company Size{rangeLabel ? ` — ${rangeLabel}` : ""}
            </h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              Cold outreach rolled up by company size (employee-count bucket), across the
              selected webinars. Contacts with no size are shown as “(no size)”. Nonjoiners /
              no-list-data are excluded. Percentages are computed from summed totals, not
              averaged per-webinar rates.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
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
            title="All selected webinars — by company size"
            subtitle={`${includedCount} webinar${includedCount === 1 ? "" : "s"} combined`}
          />
          <EmployeeFunnelTable byEmployee={data.byEmployee} totals={data.totals} />

          {/* Per-webinar breakdown, newest first. */}
          {data.perWebinar.length > 0 && (
            <div className="mt-8">
              <SectionHeading title="Per webinar" subtitle="Broken down by company size" />
              <div className="flex flex-col gap-6">
                {data.perWebinar.map((w) => (
                  <div key={w.webinarId}>
                    <div className="text-xs font-semibold text-zinc-700 dark:text-zinc-300 mb-1.5">
                      {w.label ?? `W${w.number ?? "?"}`}
                    </div>
                    <EmployeeFunnelTable byEmployee={w.byEmployee} />
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

/* ── Funnel table (flat, sortable) ──────────────────────────────────────── */

const COL = "px-3 py-2 text-right tabular-nums whitespace-nowrap";

/** Sort key for the company-size column: the bucket's numeric lower bound
 * ("0 - 2" → 0, "10000+" → 10000). "(no size)" and any unparseable label sort
 * before the numbered buckets, so ascending shows "(no size)" on top then sizes
 * small→large (descending reverses it). */
function bucketOrder(bucket: string): number {
  const n = parseInt(bucket, 10);
  return Number.isNaN(n) ? Number.NEGATIVE_INFINITY : n;
}

type CellKey = keyof FunnelCells;
type SortKey = "group" | CellKey;
type SortDir = "asc" | "desc";

type ColGroup = "Leads" | "Registrations" | "Attendance" | "Sales" | "Quality";

/** Single source of truth for the numeric columns — drives the header, the body
 * cells, the totals row and the sort keys so they can't drift. `group` bands
 * each column into a section-wrapper header row above the labels. `lowerIsBetter`
 * flips the heatmap so fewer reads as greener. */
const NUMERIC_COLUMNS: {
  key: CellKey;
  label: string;
  title?: string;
  group: ColGroup;
  fmt: (c: FunnelCells) => string;
  lowerIsBetter?: boolean;
}[] = [
  { key: "invites", label: "Leads", group: "Leads", title: "Distinct contacts mailed in this size bucket", fmt: (c) => fmtInt(c.invites) },
  { key: "regs", label: "Regs", group: "Registrations", fmt: (c) => fmtInt(c.regs) },
  { key: "regPct", label: "Reg%", group: "Registrations", fmt: (c) => fmtPct(c.regPct) },
  { key: "attendees10m", label: "Attendees (10min+)", group: "Attendance", fmt: (c) => fmtInt(c.attendees10m) },
  { key: "attOfInv", label: "Att% (of leads)", group: "Attendance", title: "10-min+ attendees ÷ leads", fmt: (c) => fmtPct(c.attOfInv) },
  { key: "attOfReg", label: "Att% (of reg)", group: "Attendance", title: "10-min+ attendees ÷ registrations", fmt: (c) => fmtPct(c.attOfReg) },
  { key: "bookings", label: "Bookings", group: "Sales", fmt: (c) => fmtInt(c.bookings) },
  { key: "bookOfAtt", label: "Book% (of att)", group: "Sales", title: "Bookings ÷ 10-min+ attendees", fmt: (c) => fmtPct(c.bookOfAtt) },
  { key: "bookPer1kInv", label: "Book/1k leads", group: "Sales", title: "Bookings per 1,000 leads", fmt: (c) => fmtPer1k(c.bookPer1kInv) },
  { key: "confirmed", label: "Confirmed", group: "Sales", title: "Opportunities with Call 1 status = Confirmed", fmt: (c) => fmtInt(c.confirmed) },
  { key: "shows", label: "Shows", group: "Sales", title: "Opportunities whose first call showed up", fmt: (c) => fmtInt(c.shows) },
  { key: "showPct", label: "Show%", group: "Sales", title: "Shows ÷ bookings", fmt: (c) => fmtPct(c.showPct) },
  { key: "noShows", label: "No Shows", group: "Sales", title: "Opportunities that no-showed on Call 1", fmt: (c) => fmtInt(c.noShows), lowerIsBetter: true },
  { key: "canceled", label: "Canceled", group: "Sales", title: "Opportunities whose Call 1 was cancelled", fmt: (c) => fmtInt(c.canceled), lowerIsBetter: true },
  { key: "won", label: "Won", group: "Sales", title: "Opportunities that reached the Deal Won stage", fmt: (c) => fmtInt(c.won) },
  { key: "closeRate", label: "Close%", group: "Sales", title: "Won ÷ shows", fmt: (c) => fmtPct(c.closeRate) },
  { key: "disqualified", label: "DQ", group: "Quality", title: "Opportunities in the Disqualified stage", fmt: (c) => fmtInt(c.disqualified), lowerIsBetter: true },
  { key: "qualified", label: "Qualified", group: "Quality", title: "Shows with non-DQ lead quality (Great / Ok / Barely Passable)", fmt: (c) => fmtInt(c.qualified) },
  { key: "qualRate", label: "Qual%", group: "Quality", title: "Qualified ÷ shows", fmt: (c) => fmtPct(c.qualRate) },
  { key: "leadQualityGreat", label: "Great", group: "Quality", title: "Lead quality 'Great'", fmt: (c) => fmtInt(c.leadQualityGreat) },
  { key: "leadQualityOk", label: "Ok", group: "Quality", title: "Lead quality 'Ok'", fmt: (c) => fmtInt(c.leadQualityOk) },
  { key: "leadQualityBarelyPassable", label: "Barely", group: "Quality", title: "Lead quality 'Barely Passable'", fmt: (c) => fmtInt(c.leadQualityBarelyPassable) },
  { key: "leadQualityBadDq", label: "Bad/DQ", group: "Quality", title: "Lead quality 'Bad / DQ'", fmt: (c) => fmtInt(c.leadQualityBadDq), lowerIsBetter: true },
];

/** Contiguous column groups → colSpans for the section-wrapper header row. */
const COLUMN_GROUPS: { group: ColGroup; span: number }[] = NUMERIC_COLUMNS.reduce(
  (acc, col) => {
    const last = acc[acc.length - 1];
    if (last && last.group === col.group) last.span += 1;
    else acc.push({ group: col.group, span: 1 });
    return acc;
  },
  [] as { group: ColGroup; span: number }[],
);

/** True if the numeric column at `idx` starts a new group (gets a left divider). */
function isColGroupBoundary(idx: number): boolean {
  return idx === 0 || NUMERIC_COLUMNS[idx].group !== NUMERIC_COLUMNS[idx - 1].group;
}

const groupHeadBase =
  "sticky top-0 z-20 h-6 bg-zinc-50 dark:bg-zinc-900 px-2 py-1 text-[9px] font-bold text-zinc-400 whitespace-nowrap select-none";

const groupHeadCorner =
  "sticky top-0 left-0 z-30 h-6 bg-zinc-50 dark:bg-zinc-900 px-2 py-1";

function SortArrow({ active, dir }: { active: boolean; dir: SortDir }) {
  if (!active) return <span className="text-zinc-400 dark:text-zinc-600 text-[10px]">↕</span>;
  return <span className="text-violet-500 text-[10px]">{dir === "asc" ? "↑" : "↓"}</span>;
}

function EmployeeFunnelTable({
  byEmployee,
  totals,
}: {
  byEmployee: EmployeeFunnelRow[];
  totals?: EmployeeFunnelResponse["totals"];
}) {
  // Default to company-size order (ascending): "(no size)" first, then the
  // buckets small→large. Clicking the column header toggles to reverse.
  const [sortKey, setSortKey] = useState<SortKey>("group");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  const rows = useMemo<BucketRow[]>(
    () => byEmployee.map((r) => ({ bucket: r.bucket, ...funnelOf(r) })),
    [byEmployee],
  );

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "group" ? "asc" : "desc");
    }
  };

  // Per-column sorted non-null values across the rows → rank-based (quantile)
  // heatmap: color tracks a value's position in the ranked order, not its
  // distance from the max, so one standout bucket can't wash the rest red.
  const colStats = useMemo(() => {
    const cells = rows.map((r) => deriveCells(r));
    const s = {} as Record<CellKey, number[]>;
    for (const col of NUMERIC_COLUMNS) {
      const vals: number[] = [];
      for (const c of cells) {
        const v = c[col.key];
        if (v !== null) vals.push(v);
      }
      vals.sort((a, b) => a - b);
      s[col.key] = vals;
    }
    return s;
  }, [rows]);

  const sortedRows = useMemo(() => {
    const decorated = rows.map((row) => ({ row, cells: deriveCells(row) }));
    decorated.sort((a, b) => {
      let cmp: number;
      if (sortKey === "group") {
        // Order by the bucket's numeric lower bound so sizes increment properly
        // ("6 - 10" before "11 - 20", "10000+" last). "(no size)" sorts to the end.
        cmp = bucketOrder(a.row.bucket) - bucketOrder(b.row.bucket);
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
        No company-size data for the selected webinars.
      </div>
    );
  }

  const headBase =
    "sticky top-6 z-10 bg-zinc-50 dark:bg-zinc-900 px-3 py-2 font-semibold text-zinc-500 dark:text-zinc-500 whitespace-nowrap cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800 shadow-[inset_0_-1px_0_#e4e4e7] dark:shadow-[inset_0_-1px_0_#27272a]";
  const headCorner =
    "sticky top-6 left-0 z-30 bg-zinc-50 dark:bg-zinc-900 px-3 py-2 font-semibold text-zinc-500 dark:text-zinc-500 whitespace-nowrap cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800 shadow-[inset_0_-1px_0_#e4e4e7] dark:shadow-[inset_0_-1px_0_#27272a]";

  return (
    <div className="overflow-auto border border-zinc-200 dark:border-zinc-800 rounded-lg">
      <table className="w-full text-xs border-collapse">
        <thead className="text-[11px] uppercase tracking-wider">
          {/* Row 1: section-wrapper group bands */}
          <tr>
            <th className={groupHeadCorner} />
            {COLUMN_GROUPS.map((g) => (
              <th
                key={g.group}
                colSpan={g.span}
                className={`${groupHeadBase} text-center border-l border-zinc-200 dark:border-zinc-800`}
              >
                {g.group}
              </th>
            ))}
          </tr>
          {/* Row 2: individual column labels */}
          <tr>
            <th onClick={() => handleSort("group")} className={`${headCorner} text-left min-w-[200px]`}>
              <span className="inline-flex items-center gap-1">
                Company size
                <SortArrow active={sortKey === "group"} dir={sortDir} />
              </span>
            </th>
            {NUMERIC_COLUMNS.map((col, idx) => (
              <th
                key={col.key}
                onClick={() => handleSort(col.key)}
                title={col.title}
                className={`${headBase} text-right ${
                  isColGroupBoundary(idx) ? "border-l border-zinc-200 dark:border-zinc-800" : ""
                }`}
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
          {sortedRows.map((row) => (
            <FunnelRow key={row.bucket} row={row} colStats={colStats} />
          ))}
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

function leaderCls(value: number | null, best: number): string {
  return value !== null && value === best && Number.isFinite(best)
    ? "font-bold text-zinc-900 dark:text-zinc-100"
    : "text-zinc-700 dark:text-zinc-300";
}

/** A value's quantile (0–1) within the column's sorted values, average-rank for
 * ties so equal values share a color. null when there's nothing to rank against. */
function quantileRank(value: number, sorted: number[]): number | null {
  const n = sorted.length;
  if (n <= 1 || sorted[0] === sorted[n - 1]) return null;
  let below = 0;
  let equal = 0;
  for (const x of sorted) {
    if (x < value) below++;
    else if (x === value) equal++;
  }
  const avgRank = below + (equal - 1) / 2;
  return avgRank / (n - 1);
}

/** Red→amber→green heat background positioned by the value's RANK (quantile)
 * within the column — median → amber, best → green, worst → red. Pass
 * invert=true for negative-signal columns so the LOW end greens instead. */
function heatBg(value: number | null, sorted: number[], invert = false): string | undefined {
  if (value === null) return undefined;
  const q = quantileRank(value, sorted);
  if (q === null) return undefined;
  let t = q;
  if (invert) t = 1 - t;
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
  const alpha = 0.16 + 0.24 * Math.abs(2 * t - 1);
  return `rgba(${r}, ${g}, ${bl}, ${alpha.toFixed(3)})`;
}

/** A single company-size bucket row. */
function FunnelRow({
  row,
  colStats,
}: {
  row: BucketRow;
  colStats: Record<CellKey, number[]>;
}) {
  const c = deriveCells(row);
  return (
    <tr className="bg-white dark:bg-zinc-950 hover:bg-zinc-50 dark:hover:bg-zinc-900/60">
      <td className="sticky left-0 z-20 bg-white dark:bg-zinc-950 px-3 py-2 text-left text-zinc-800 dark:text-zinc-200 font-medium" title={row.bucket}>
        <span className="truncate">{row.bucket}</span>
      </td>
      {NUMERIC_COLUMNS.map((col, idx) => {
        const v = c[col.key];
        const sorted = colStats[col.key];
        const best = col.lowerIsBetter ? sorted[0] : sorted[sorted.length - 1];
        const bg = heatBg(v, sorted, col.lowerIsBetter);
        return (
          <td
            key={col.key}
            style={bg ? { backgroundColor: bg } : undefined}
            className={`${COL} ${leaderCls(v, best)} ${
              isColGroupBoundary(idx) ? "border-l border-zinc-200 dark:border-zinc-800/60" : ""
            }`}
          >
            {col.fmt(c)}
          </td>
        );
      })}
    </tr>
  );
}

function TotalsRow({ totals }: { totals: EmployeeFunnelResponse["totals"] }) {
  const c = deriveCells(totals);
  const cls = "px-3 py-2 text-right tabular-nums font-bold text-zinc-900 dark:text-zinc-100 whitespace-nowrap";
  return (
    <tr className="bg-zinc-100 dark:bg-zinc-900 border-t-2 border-zinc-300 dark:border-zinc-700">
      <td className="sticky left-0 z-20 bg-zinc-100 dark:bg-zinc-900 px-3 py-2 text-left font-bold text-zinc-900 dark:text-zinc-100">Total</td>
      {NUMERIC_COLUMNS.map((col, idx) => (
        <td
          key={col.key}
          className={`${cls} ${
            isColGroupBoundary(idx) ? "border-l border-zinc-200 dark:border-zinc-800/60" : ""
          }`}
        >
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
