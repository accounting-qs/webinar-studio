"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchStatisticsSegments,
  fetchSegmentEmployee,
  updateBucketQuality,
  updateBucketStatEmpRange,
  type BucketQuality,
  type SegmentEmployeeBand,
  type SegmentEmployeeResponse,
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
  // Sales + quality (counts summed per bucket; rates derived from the sums)
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

/** Raw count fields deriveCells needs — satisfied by BOTH a segment row and an
 * employee-size band, so the drill-down reuses the exact same cells + heatmap. */
type RawFunnelCounts = {
  invites: number; regs: number; attendees10m: number; bookings: number; callsPassed: number;
  confirmed: number; shows: number; noShows: number; canceled: number; won: number;
  disqualified: number; qualified: number;
  leadQualityGreat: number; leadQualityOk: number;
  leadQualityBarelyPassable: number; leadQualityBadDq: number;
};

function deriveCells(r: RawFunnelCounts): FunnelCells {
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
    // Shows ÷ the calls whose date has passed (not ÷ bookings): a call
    // still in the future is not a call that failed to show.
    showPct: safeDiv(r.shows, r.callsPassed),
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

/* ── Quality recommendation (attendance-led + booking tiebreak) ─────────── */

/** Below this invite volume a segment's attendance/booking counts are too thin
 * to judge, so no recommendation is offered. Attendance only firms up around
 * here (~29 attendees at the portfolio rate); bookings stay sparse regardless,
 * which is why they carry the lighter weight below. Tunable. */
const MIN_INVITES_FOR_RECO = 50_000;
const ATT_WEIGHT = 0.7;
const BOOK_WEIGHT = 0.3;
/** Clamp the booking ratio so one fluke booking streak can't override the
 * (more reliable) attendance signal. */
const BOOK_RATIO_CAP = 3;
const GOOD_AT = 1.15; // combined score ≥ → good
const BAD_AT = 0.85; // combined score ≤ → bad

type Benchmark = { attPerInv: number; bookPerInv: number };

/** Portfolio benchmark from the named bucket rows only (bucketId !== null) —
 * mirrors colStats, which excludes the "Other (no bucket)" catch-all and the
 * Total so they don't skew the average. null when there's nothing to divide by. */
function computeBenchmark(rows: SegmentFunnelRow[]): Benchmark | null {
  let inv = 0;
  let att = 0;
  let book = 0;
  for (const r of rows) {
    if (r.bucketId === null) continue;
    inv += r.invites;
    att += r.attendees10m;
    book += r.bookings;
  }
  if (inv <= 0 || att <= 0) return null;
  return { attPerInv: att / inv, bookPerInv: book / inv };
}

type Recommendation = { quality: BucketQuality; score: number; a: number; b: number };

/** Attendance-led quality suggestion for a segment, scored against the portfolio
 * benchmark. null when the segment hasn't cleared the volume gate or the
 * benchmark is unusable. Attendance-per-invite carries the score (70%);
 * bookings-per-invite nudge borderline cases (30%, clamped). */
function computeRecommendation(
  row: SegmentFunnelRow,
  bench: Benchmark | null,
): Recommendation | null {
  if (bench === null || row.bucketId === null) return null;
  if (row.invites < MIN_INVITES_FOR_RECO) return null;
  const a = row.invites > 0 ? row.attendees10m / row.invites / bench.attPerInv : 0;
  const bRaw =
    bench.bookPerInv > 0 && row.invites > 0
      ? row.bookings / row.invites / bench.bookPerInv
      : 0;
  const b = Math.min(bRaw, BOOK_RATIO_CAP);
  const score = ATT_WEIGHT * a + BOOK_WEIGHT * b;
  const quality: BucketQuality =
    score >= GOOD_AT ? "good" : score <= BAD_AT ? "bad" : "medium";
  return { quality, score, a, b };
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

  // Set/clear a segment's employee-count range. Optimistic patch then persist,
  // mirroring setSegmentQuality. Either bound may be null (open); both null clears.
  const setSegmentEmpRange = useCallback(
    async (bucketId: string, statEmpMin: number | null, statEmpMax: number | null) => {
      setData((prev) =>
        prev
          ? {
              ...prev,
              segments: prev.segments.map((s) =>
                s.bucketId === bucketId ? { ...s, statEmpMin, statEmpMax } : s,
              ),
            }
          : prev,
      );
      try {
        await updateBucketStatEmpRange(bucketId, statEmpMin, statEmpMax);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        refresh();
      }
    },
    [refresh],
  );

  // Apply a quality to several buckets at once (bulk approve / override). Same
  // optimistic-then-persist pattern as setSegmentQuality; on any failure surface
  // the error and reload to resync.
  const setSegmentQualities = useCallback(
    async (updates: { bucketId: string; quality: BucketQuality }[]) => {
      if (updates.length === 0) return;
      const map = new Map(updates.map((u) => [u.bucketId, u.quality]));
      setData((prev) =>
        prev
          ? {
              ...prev,
              segments: prev.segments.map((s) =>
                s.bucketId && map.has(s.bucketId)
                  ? { ...s, quality: map.get(s.bucketId)! }
                  : s,
              ),
            }
          : prev,
      );
      try {
        await Promise.all(
          updates.map((u) => updateBucketQuality(u.bucketId, u.quality)),
        );
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
              Quality suggestions appear once a segment passes{" "}
              {MIN_INVITES_FOR_RECO.toLocaleString()} invites.
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
            // Drill-down live-computes; scope it to the SAME snapshot-backed
            // webinars the segment rows sum (exclude pending), so the per-size
            // breakdown + suggested range tie out to the row they explain.
            includedWebinarIds={data.includedWebinarIds.filter(
              (id) => !data.pendingWebinarIds.includes(id),
            )}
            onSetQuality={setSegmentQuality}
            onBulkSetQuality={setSegmentQualities}
            onSetEmpRange={setSegmentEmpRange}
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

type ColGroup = "Invites" | "Registrations" | "Attendance" | "Sales" | "Quality";

/** Single source of truth for the numeric columns — drives the header, the body
 * cells, the totals row, and the sort keys so they can never drift apart. The
 * `group` bands each column into a section-wrapper header row above the labels
 * (mirrors the Statistics tab). */
const NUMERIC_COLUMNS: {
  key: CellKey;
  label: string;
  title?: string;
  group: ColGroup;
  fmt: (c: FunnelCells) => string;
  /** Negative-signal metric: heatmap greens the LOW end (fewer = better). */
  lowerIsBetter?: boolean;
}[] = [
  { key: "invites", label: "Invites", group: "Invites", fmt: (c) => fmtInt(c.invites) },
  { key: "regs", label: "Regs", group: "Registrations", fmt: (c) => fmtInt(c.regs) },
  { key: "regPct", label: "Reg%", group: "Registrations", fmt: (c) => fmtPct(c.regPct) },
  { key: "attendees10m", label: "Attendees (10m+)", group: "Attendance", fmt: (c) => fmtInt(c.attendees10m) },
  { key: "attOfInv", label: "Att% (of inv)", group: "Attendance", title: "10-min+ attendees ÷ invites", fmt: (c) => fmtPct(c.attOfInv) },
  { key: "attOfReg", label: "Att% (of reg)", group: "Attendance", title: "10-min+ attendees ÷ registrations", fmt: (c) => fmtPct(c.attOfReg) },
  { key: "bookings", label: "Bookings", group: "Sales", fmt: (c) => fmtInt(c.bookings) },
  { key: "bookOfAtt", label: "Book% (of att)", group: "Sales", title: "Bookings ÷ 10-min+ attendees", fmt: (c) => fmtPct(c.bookOfAtt) },
  { key: "bookPer1kInv", label: "Book/1k inv", group: "Sales", title: "Bookings per 1,000 invites", fmt: (c) => fmtPer1k(c.bookPer1kInv) },
  { key: "confirmed", label: "Confirmed", group: "Sales", title: "Opportunities with Call 1 status = Confirmed", fmt: (c) => fmtInt(c.confirmed) },
  { key: "shows", label: "Shows", group: "Sales", title: "Opportunities whose first call showed up", fmt: (c) => fmtInt(c.shows) },
  { key: "showPct", label: "Show%", group: "Sales", title: "Shows ÷ calls whose date has passed", fmt: (c) => fmtPct(c.showPct) },
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

/** Contiguous column groups → colSpans for the section-wrapper header row.
 * Derived from NUMERIC_COLUMNS so the bands can't drift from the columns. */
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

/** Group (section-wrapper) header cell — a sticky top band above the sortable
 * column labels. Fixed h-6 so the label row can stick flush at top-6. */
const groupHeadBase =
  "sticky top-0 z-20 h-6 bg-zinc-50 dark:bg-zinc-900 px-2 py-1 text-[9px] font-bold text-zinc-400 whitespace-nowrap select-none";

// Frozen top-left corner: sticks to both top and left so the Segment label
// stays visible while scrolling right. Higher z than the header row cells.
const groupHeadCorner =
  "sticky top-0 left-0 z-30 h-6 bg-zinc-50 dark:bg-zinc-900 px-2 py-1";

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
  includedWebinarIds,
  onSetQuality,
  onBulkSetQuality,
  onSetEmpRange,
}: {
  segments: SegmentFunnelRow[];
  totals: SegmentFunnelRow;
  includedCount: number;
  includedWebinarIds: string[];
  onSetQuality: (bucketId: string, quality: BucketQuality | null) => void;
  onBulkSetQuality: (
    updates: { bucketId: string; quality: BucketQuality }[],
  ) => void;
  onSetEmpRange: (bucketId: string, statEmpMin: number | null, statEmpMax: number | null) => void;
}) {
  // Default to invites desc — matches the server's initial ordering.
  const [sortKey, setSortKey] = useState<SortKey>("invites");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  // Bulk-approve selection — bucketIds ticked in the Segment column. Only rows
  // with a still-pending suggestion are selectable (see pendingIds below).
  const [selected, setSelected] = useState<Set<string>>(new Set());

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

  // Per-column sorted non-null values across the named bucket rows. Drives a
  // rank-based (quantile) heatmap — color tracks a value's position in the
  // ranked order, not its distance from the max — so a single standout segment
  // can't stretch the scale and wash everyone else red. Also feeds the
  // column-leader emphasis (min/max are the ends of the sorted array).
  // "Other"/Total are excluded so a catch-all bucket doesn't skew the ranking.
  const colStats = useMemo(() => {
    const s = {} as Record<CellKey, number[]>;
    for (const col of NUMERIC_COLUMNS) {
      const vals: number[] = [];
      for (const c of namedCells) {
        const v = c[col.key];
        if (v !== null) vals.push(v);
      }
      vals.sort((a, b) => a - b);
      s[col.key] = vals;
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

  // Portfolio benchmark + per-segment quality suggestions (attendance-led).
  const benchmark = useMemo(() => computeBenchmark(segments), [segments]);
  const recos = useMemo(() => {
    const m = new Map<string, Recommendation>();
    for (const s of segments) {
      if (s.bucketId === null) continue;
      const rec = computeRecommendation(s, benchmark);
      if (rec) m.set(s.bucketId, rec);
    }
    return m;
  }, [segments, benchmark]);

  // Rows with a live suggestion still awaiting a decision (quality unset). These
  // are the only selectable / approvable rows.
  const pendingIds = useMemo(() => {
    const ids = new Set<string>();
    for (const s of segments) {
      if (s.bucketId && s.quality === null && recos.has(s.bucketId)) {
        ids.add(s.bucketId);
      }
    }
    return ids;
  }, [segments, recos]);

  // Selection pruned to rows that are still pending — approved rows drop out on
  // the next render so a stale id can't linger in the bulk bar.
  const selectedPending = useMemo(() => {
    const n = new Set<string>();
    for (const id of selected) if (pendingIds.has(id)) n.add(id);
    return n;
  }, [selected, pendingIds]);

  const toggleOne = (id: string) =>
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const allPendingSelected =
    pendingIds.size > 0 && selectedPending.size === pendingIds.size;
  const toggleAllPending = () =>
    setSelected(allPendingSelected ? new Set() : new Set(pendingIds));
  const clearSelection = () => setSelected(new Set());

  const approveSuggested = () => {
    const updates: { bucketId: string; quality: BucketQuality }[] = [];
    for (const id of selectedPending) {
      const rec = recos.get(id);
      if (rec) updates.push({ bucketId: id, quality: rec.quality });
    }
    onBulkSetQuality(updates);
    clearSelection();
  };
  const overrideSelected = (quality: BucketQuality) => {
    onBulkSetQuality(
      Array.from(selectedPending).map((id) => ({ bucketId: id, quality })),
    );
    clearSelection();
  };

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
  // top-6 so the label row sticks just below the h-6 group-band row above it.
  const headBase =
    "sticky top-6 z-10 bg-zinc-50 dark:bg-zinc-900 px-3 py-2 font-semibold text-zinc-500 dark:text-zinc-500 whitespace-nowrap cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800 shadow-[inset_0_-1px_0_#e4e4e7] dark:shadow-[inset_0_-1px_0_#27272a]";
  // Frozen Segment-column header cell — sticks left AND top-6, above the
  // scrolling header cells (z-30) so the "Segment" label stays put on scroll.
  const headCorner =
    "sticky top-6 left-0 z-30 bg-zinc-50 dark:bg-zinc-900 px-3 py-2 font-semibold text-zinc-500 dark:text-zinc-500 whitespace-nowrap cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800 shadow-[inset_0_-1px_0_#e4e4e7] dark:shadow-[inset_0_-1px_0_#27272a]";

  return (
    <div className="mt-2 flex-1 min-h-0 flex flex-col">
      {selectedPending.size > 0 && (
        <div className="shrink-0 mb-2 flex items-center gap-2 flex-wrap rounded-lg border border-violet-500/30 bg-violet-500/10 px-3 py-2 text-xs">
          <span className="font-semibold text-zinc-700 dark:text-zinc-200">
            {selectedPending.size} selected
          </span>
          <button
            onClick={approveSuggested}
            className="rounded-md bg-violet-600 hover:bg-violet-500 px-2.5 py-1 font-semibold text-white"
          >
            Approve suggested ({selectedPending.size})
          </button>
          <span className="text-zinc-400 dark:text-zinc-500">or mark selected</span>
          {(["good", "medium", "bad"] as BucketQuality[]).map((q) => (
            <button
              key={q}
              onClick={() => overrideSelected(q)}
              className={`rounded-md border bg-white dark:bg-zinc-900 px-2 py-1 font-semibold hover:bg-zinc-50 dark:hover:bg-zinc-800 ${QUALITY_META[q].cls}`}
            >
              {QUALITY_META[q].label}
            </button>
          ))}
          <button
            onClick={clearSelection}
            className="ml-auto text-zinc-500 hover:underline"
          >
            Clear
          </button>
        </div>
      )}
      <div className="flex-1 min-h-0 overflow-auto border border-zinc-200 dark:border-zinc-800 rounded-lg">
      <table className="w-full text-xs border-collapse">
        <thead className="text-[11px] uppercase tracking-wider">
          {/* Row 1: section-wrapper group bands */}
          <tr>
            <th className={groupHeadCorner} />
            {/* Two leading non-numeric columns (Quality, Employee range) live
                outside COLUMN_GROUPS — empty bands above them keep the column
                count aligned with the label + body rows. */}
            <th className={`${groupHeadBase} text-center border-l border-zinc-200 dark:border-zinc-800`} />
            <th className={`${groupHeadBase} text-center border-l border-zinc-200 dark:border-zinc-800`} />
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
          {/* Row 2: individual column labels (stick just below the group band) */}
          <tr>
            <th
              onClick={() => handleSort("segment")}
              className={`${headCorner} text-left min-w-[200px]`}
            >
              <span className="inline-flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={allPendingSelected}
                  disabled={pendingIds.size === 0}
                  onClick={(e) => e.stopPropagation()}
                  onChange={toggleAllPending}
                  title="Select all suggested segments"
                  className="accent-violet-500 align-middle disabled:opacity-40 cursor-pointer"
                />
                Segment
                <SortArrow active={sortKey === "segment"} dir={sortDir} />
              </span>
            </th>
            <th
              className={`${headBase} text-left border-l border-zinc-200 dark:border-zinc-800`}
              style={{ cursor: "default" }}
            >
              Quality
            </th>
            <th
              className={`${headBase} text-left border-l border-zinc-200 dark:border-zinc-800`}
              style={{ cursor: "default" }}
              title="User-set employee-count range for this segment. Click a row to see the size breakdown + a suggested range."
            >
              Employee range
            </th>
            {NUMERIC_COLUMNS.map((col, idx) => (
              <th
                key={col.key}
                onClick={() => handleSort(col.key)}
                title={col.title}
                className={`${headBase} text-right ${
                  isColGroupBoundary(idx)
                    ? "border-l border-zinc-200 dark:border-zinc-800"
                    : ""
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
          {sortedNamed.map((s) => {
            const reco = s.bucketId ? recos.get(s.bucketId) ?? null : null;
            const pending = !!(s.bucketId && s.quality === null && reco);
            return (
              <SegmentRow
                // Include the drill-down scope in the key so changing the webinar
                // selection remounts the row — collapsing + invalidating any open
                // drill-down so it refetches for the new selection (never shows
                // stale counts). Stable across plain refreshes (same id set).
                key={`${s.bucketId}#${includedWebinarIds.join(",")}`}
                row={s}
                colStats={colStats}
                isOther={false}
                onSetQuality={onSetQuality}
                onSetEmpRange={onSetEmpRange}
                includedWebinarIds={includedWebinarIds}
                reco={reco}
                pending={pending}
                checked={s.bucketId ? selectedPending.has(s.bucketId) : false}
                onToggle={s.bucketId ? () => toggleOne(s.bucketId!) : undefined}
              />
            );
          })}
          {otherRows.map((s, i) => (
            <SegmentRow key={`other-${i}`} row={s} colStats={colStats} isOther />
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

/** A value's quantile (0–1) within the column's sorted values, using
 * average-rank for ties so equal values share a color: min → 0, median → ~0.5,
 * max → 1. null when there's nothing to rank against (0/1 values, or no spread
 * at all). Ranking by position — not raw magnitude — is what keeps the heatmap
 * robust to a single outlying segment. */
function quantileRank(value: number, sorted: number[]): number | null {
  const n = sorted.length;
  if (n <= 1 || sorted[0] === sorted[n - 1]) return null;
  let below = 0;
  let equal = 0;
  for (const x of sorted) {
    if (x < value) below++;
    else if (x === value) equal++;
  }
  const avgRank = below + (equal - 1) / 2; // 0-indexed average rank among ties
  return avgRank / (n - 1);
}

/** Red→amber→green heat background for a cell, positioned by the value's RANK
 * (quantile) within the column — median → amber, best → green, worst → red — so
 * one standout segment can't stretch the scale and push everyone else to red.
 * Pass invert=true for negative-signal columns (No Shows, Canceled, DQ, Bad/DQ)
 * so the LOW end greens instead. Returns a translucent rgba so it reads on both
 * light and dark rows, or undefined when there's nothing to rank (null value,
 * or a column with no spread). */
function heatBg(value: number | null, sorted: number[], invert = false): string | undefined {
  if (value === null) return undefined;
  const q = quantileRank(value, sorted);
  if (q === null) return undefined;
  let t = q; // 0 = worst (red), 1 = best (green)
  if (invert) t = 1 - t; // fewer = better: flip so low values read green
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

/** The suggested quality for a pending segment: a colored chip plus a one-click
 * Approve that writes the recommendation onto the bucket. The title spells out
 * the score so the suggestion is auditable. */
function SuggestionChip({
  reco,
  onApprove,
}: {
  reco: Recommendation;
  onApprove: () => void;
}) {
  const meta = QUALITY_META[reco.quality];
  const title = `Suggested from attendance ${reco.a.toFixed(2)}× avg + bookings ${reco.b.toFixed(
    2,
  )}× avg → score ${reco.score.toFixed(2)}`;
  return (
    <span className="inline-flex shrink-0 items-center gap-1" title={title}>
      <span className={`rounded border px-1.5 py-0.5 text-[11px] font-semibold ${meta.cls}`}>
        Suggested: {meta.label}
      </span>
      <button
        onClick={onApprove}
        className="rounded border border-violet-500/40 px-1.5 py-0.5 text-[11px] font-semibold text-violet-600 dark:text-violet-400 hover:bg-violet-500/10"
      >
        Approve
      </button>
    </span>
  );
}

// Segment + Quality + Employee range — the non-numeric leading columns. Kept in
// sync with the header rows and the TotalsRow placeholders.
const LEADING_COL_COUNT = 3;

function SegmentRow({
  row,
  colStats,
  isOther,
  onSetQuality,
  onSetEmpRange,
  includedWebinarIds,
  reco,
  pending,
  checked,
  onToggle,
}: {
  row: SegmentFunnelRow;
  colStats: Record<CellKey, number[]>;
  isOther: boolean;
  onSetQuality?: (bucketId: string, quality: BucketQuality | null) => void;
  onSetEmpRange?: (bucketId: string, statEmpMin: number | null, statEmpMax: number | null) => void;
  includedWebinarIds?: string[];
  reco?: Recommendation | null;
  pending?: boolean;
  checked?: boolean;
  onToggle?: () => void;
}) {
  const c = deriveCells(row);
  const canExpand = !isOther && !!row.bucketId;
  const [expanded, setExpanded] = useState(false);
  const [drill, setDrill] = useState<SegmentEmployeeResponse | null>(null);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillError, setDrillError] = useState<string | null>(null);

  // Lazy-load the size breakdown on first expand; cache it for later toggles.
  const toggleExpand = useCallback(() => {
    if (!canExpand) return;
    setExpanded((prev) => {
      const next = !prev;
      if (next && !drill && !drillLoading) {
        setDrillLoading(true);
        setDrillError(null);
        fetchSegmentEmployee(row.bucketId!, includedWebinarIds ?? null)
          .then((d) => setDrill(d))
          .catch((e) => setDrillError(e instanceof Error ? e.message : String(e)))
          .finally(() => setDrillLoading(false));
      }
      return next;
    });
  }, [canExpand, drill, drillLoading, row.bucketId, includedWebinarIds]);

  return (
    <>
      <tr className="bg-white dark:bg-zinc-950 hover:bg-zinc-50 dark:hover:bg-zinc-900/60">
        {/* Segment: bulk checkbox + expand chevron + name */}
        <td
          className={`sticky left-0 z-20 bg-white dark:bg-zinc-950 px-3 py-2 text-left ${
            isOther
              ? "text-zinc-500 italic"
              : "text-zinc-800 dark:text-zinc-200 font-medium"
          }`}
          title={row.bucketName ?? ""}
        >
          <div className="flex items-center gap-2">
            {pending && onToggle ? (
              <input
                type="checkbox"
                checked={!!checked}
                onChange={onToggle}
                title="Select for bulk approve"
                className="accent-violet-500 align-middle cursor-pointer"
              />
            ) : (
              !isOther && <span className="inline-block w-[13px] shrink-0" />
            )}
            {canExpand ? (
              <button
                onClick={toggleExpand}
                title="Show company-size breakdown + suggested range"
                className="shrink-0 w-3 text-[10px] text-zinc-400 hover:text-violet-500"
              >
                {expanded ? "▼" : "▶"}
              </button>
            ) : (
              !isOther && <span className="inline-block w-3 shrink-0" />
            )}
            <span className="truncate">{row.bucketName ?? "—"}</span>
          </div>
        </td>
        {/* Quality: suggestion chip + colored select (moved out of Segment) */}
        <td className="px-3 py-2 border-l border-zinc-200 dark:border-zinc-800/60">
          {!isOther && row.bucketId && onSetQuality ? (
            <div className="flex items-center gap-1.5">
              {pending && reco && (
                <SuggestionChip
                  reco={reco}
                  onApprove={() => onSetQuality(row.bucketId!, reco.quality)}
                />
              )}
              <QualitySelect
                value={row.quality}
                onChange={(q) => onSetQuality(row.bucketId!, q)}
              />
            </div>
          ) : null}
        </td>
        {/* Employee range: inline min/max editor */}
        <td className="px-3 py-2 border-l border-zinc-200 dark:border-zinc-800/60 whitespace-nowrap">
          {!isOther && row.bucketId && onSetEmpRange ? (
            <EmpRangeEditor
              statEmpMin={row.statEmpMin ?? null}
              statEmpMax={row.statEmpMax ?? null}
              onSave={(mn, mx) => onSetEmpRange(row.bucketId!, mn, mx)}
            />
          ) : null}
        </td>
        {NUMERIC_COLUMNS.map((col, idx) => {
          const v = c[col.key];
          const sorted = colStats[col.key];
          // For negative-signal columns the "best" (bold-emphasized) extreme is
          // the low end, and the heatmap greens the low end too.
          const best = col.lowerIsBetter ? sorted[0] : sorted[sorted.length - 1];
          // Heat only the named segment rows; the "Other (no bucket)" catch-all
          // isn't part of the comparison set.
          const bg = isOther ? undefined : heatBg(v, sorted, col.lowerIsBetter);
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
      {expanded && canExpand && (
        <SegmentDrilldownRows
          data={drill}
          loading={drillLoading}
          error={drillError}
          currentMin={row.statEmpMin ?? null}
          currentMax={row.statEmpMax ?? null}
          onApplyRange={(mn, mx) =>
            onSetEmpRange && row.bucketId && onSetEmpRange(row.bucketId, mn, mx)
          }
        />
      )}
    </>
  );
}

/* ── Per-segment employee-range editor + drill-down ──────────────────────── */

/** Inline min/max editor for a segment's employee range. Commits on blur / Enter
 * (only when changed) so it doesn't fire a PUT per keystroke. Empty = open bound;
 * both empty clears the definition. */
function EmpRangeEditor({
  statEmpMin,
  statEmpMax,
  onSave,
}: {
  statEmpMin: number | null;
  statEmpMax: number | null;
  onSave: (mn: number | null, mx: number | null) => void;
}) {
  const toStr = (n: number | null) => (n != null ? String(n) : "");
  // Local draft, seeded from the persisted range. Resynced from props DURING
  // RENDER (React's "adjust state when a prop changes" pattern) rather than a
  // key-remount, so an external change (Apply-suggested / Clear) updates the
  // inputs WITHOUT unmounting them — which would steal focus mid-entry. A user's
  // own commit sets props to the value they already typed, so this is a no-op then.
  const [mn, setMn] = useState(toStr(statEmpMin));
  const [mx, setMx] = useState(toStr(statEmpMax));
  const [prevMin, setPrevMin] = useState(statEmpMin);
  const [prevMax, setPrevMax] = useState(statEmpMax);
  if (statEmpMin !== prevMin) {
    setPrevMin(statEmpMin);
    setMn(toStr(statEmpMin));
  }
  if (statEmpMax !== prevMax) {
    setPrevMax(statEmpMax);
    setMx(toStr(statEmpMax));
  }

  // Clamp negatives to 0 on entry (type=number min=0 doesn't block typed "-5"),
  // so a fat-fingered negative can't silently parse to null and clear a set bound.
  const onNumChange = (setter: (s: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value;
    setter(v === "" ? "" : String(Math.max(0, parseInt(v, 10) || 0)));
  };
  const parse = (s: string): number | null => {
    if (s.trim() === "") return null;
    const n = parseInt(s, 10);
    return Number.isFinite(n) && n >= 0 ? n : null;
  };
  const commit = () => {
    const nMn = parse(mn);
    const nMx = parse(mx);
    // Skip an inverted range (backend also rejects it with a 400) so a transient
    // min>max mid-edit doesn't fire a doomed request.
    if (nMn != null && nMx != null && nMn > nMx) return;
    if (nMn !== statEmpMin || nMx !== statEmpMax) onSave(nMn, nMx);
  };
  const inputCls =
    "w-14 bg-white dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700 rounded px-1.5 py-0.5 text-[11px] tabular-nums focus:outline-none focus:ring-1 focus:ring-violet-500";
  return (
    <div className="flex items-center gap-1">
      <input
        type="number"
        min={0}
        placeholder="min"
        value={mn}
        onChange={onNumChange(setMn)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        className={inputCls}
      />
      <span className="text-zinc-400 text-[10px]">–</span>
      <input
        type="number"
        min={0}
        placeholder="max"
        value={mx}
        onChange={onNumChange(setMx)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        className={inputCls}
      />
    </div>
  );
}

/* ── Suggested employee range (conversion-led: calls + lead quality + won) ── */

/** Per-band invite gate for the suggested range — bands thinner than this are
 * too noisy to weigh. Lower than the segment-level MIN_INVITES_FOR_RECO since a
 * single size band is much smaller than the whole segment. Tunable. */
const MIN_BAND_INVITES_FOR_RECO = 1_000;
// Conversion weighting: booked calls + lead-quality tiers + won (user-chosen).
const EMP_CALLS_WEIGHT = 0.4;
const EMP_LQ_WEIGHT = 0.35;
const EMP_WON_WEIGHT = 0.25;
// Lead-quality tier points, best → worst (Bad/DQ contributes nothing).
const LQ_GREAT_PTS = 3;
const LQ_OK_PTS = 2;
const LQ_BARELY_PTS = 1;

type EmpRangeReco = { minEmp: number; maxEmp: number | null; bandCount: number };

function bandCalls(b: SegmentEmployeeBand): number {
  return b.confirmed + b.shows;
}
function bandLqPoints(b: SegmentEmployeeBand): number {
  return (
    LQ_GREAT_PTS * b.leadQualityGreat +
    LQ_OK_PTS * b.leadQualityOk +
    LQ_BARELY_PTS * b.leadQualityBarelyPassable
  );
}

/** Suggested employee range for a segment: the contiguous span of canonical size
 * bands whose blended conversion score is at/above the segment's own average.
 * Scores each qualifying band on booked-calls, lead-quality points, and won —
 * each normalized to the segment benchmark (only terms with a positive benchmark
 * count, so a segment with no wons still gets a call/quality-based suggestion).
 * Falls back to attendance-per-invite when there is no conversion signal at all.
 * null when no band clears the volume gate or nothing beats the average. */
function computeEmpRangeRecommendation(bands: SegmentEmployeeBand[]): EmpRangeReco | null {
  const cand = bands.filter((b) => b.minEmp !== null && b.invites >= MIN_BAND_INVITES_FOR_RECO);
  if (cand.length === 0) return null;
  let inv = 0, calls = 0, lq = 0, won = 0, att = 0;
  for (const b of cand) {
    inv += b.invites;
    calls += bandCalls(b);
    lq += bandLqPoints(b);
    won += b.won;
    att += b.attendees10m;
  }
  if (inv <= 0) return null;
  const benchCalls = calls / inv;
  const benchLq = lq / inv;
  const benchWon = won / inv;
  const benchAtt = att / inv;
  const hasConv = benchCalls > 0 || benchLq > 0 || benchWon > 0;

  const score = (b: SegmentEmployeeBand): number => {
    if (b.invites <= 0) return 0;
    if (!hasConv) return benchAtt > 0 ? b.attendees10m / b.invites / benchAtt : 0;
    let s = 0, w = 0;
    if (benchCalls > 0) { s += EMP_CALLS_WEIGHT * (bandCalls(b) / b.invites / benchCalls); w += EMP_CALLS_WEIGHT; }
    if (benchLq > 0) { s += EMP_LQ_WEIGHT * (bandLqPoints(b) / b.invites / benchLq); w += EMP_LQ_WEIGHT; }
    if (benchWon > 0) { s += EMP_WON_WEIGHT * (b.won / b.invites / benchWon); w += EMP_WON_WEIGHT; }
    return w > 0 ? s / w : 0;
  };

  const good = cand.filter((b) => score(b) >= 1);
  if (good.length === 0) return null;
  // Contiguous span from the lowest to the highest qualifying band (fills any
  // interior gap so the result is one usable range). "10000+" has maxEmp null → open top.
  const lo = good.reduce((a, b) => (a.minEmp! <= b.minEmp! ? a : b));
  const hi = good.reduce((a, b) => ((a.maxEmp ?? Infinity) >= (b.maxEmp ?? Infinity) ? a : b));
  return { minEmp: lo.minEmp!, maxEmp: hi.maxEmp, bandCount: good.length };
}

/** Expanded drill-down under a segment row — rendered as REAL rows of the main
 * table (not a nested table) so every size band's numbers sit directly under the
 * segment column they explain, at the same width and formatting. One control row
 * (suggested range + apply/clear) then one row per canonical company-size band;
 * bands inside the segment's current stat_emp range are highlighted. */
function SegmentDrilldownRows({
  data,
  loading,
  error,
  currentMin,
  currentMax,
  onApplyRange,
}: {
  data: SegmentEmployeeResponse | null;
  loading: boolean;
  error: string | null;
  currentMin: number | null;
  currentMax: number | null;
  onApplyRange: (mn: number | null, mx: number | null) => void;
}) {
  // Sticky first cell needs its own opaque background (it slides over the other
  // cells on horizontal scroll); keep it in step with the row tint.
  const drillRow = "bg-zinc-50 dark:bg-zinc-900";
  // pl-8 indents the band under its segment name; kept in the shared class so
  // no caller has to fight the horizontal padding.
  const drillSticky =
    "sticky left-0 z-20 bg-zinc-50 dark:bg-zinc-900 pl-8 pr-3 py-1.5 text-left whitespace-nowrap";
  const spanRest = LEADING_COL_COUNT - 1 + NUMERIC_COLUMNS.length;

  if (loading || error || !data) {
    return (
      <tr className={drillRow}>
        <td className={`${drillSticky} text-[11px] text-zinc-500`}>
          Company-size breakdown
        </td>
        <td colSpan={spanRest} className="px-3 py-1.5 text-[11px]">
          {loading ? (
            <span className="text-zinc-500">Loading…</span>
          ) : error ? (
            <span className="text-red-500">{error}</span>
          ) : null}
        </td>
      </tr>
    );
  }

  const reco = computeEmpRangeRecommendation(data.bands);
  const pendingCount = data.pendingWebinarIds?.length ?? 0;
  const inRange = (b: SegmentEmployeeBand): boolean =>
    b.minEmp !== null &&
    (currentMin == null || (b.maxEmp ?? Infinity) >= currentMin) &&
    (currentMax == null || b.minEmp <= currentMax);
  const fmtRange = (mn: number | null, mx: number | null): string =>
    `${mn ?? 0}${mx == null ? "+" : `–${mx}`}`;

  // Per-size cells + a heatmap scaled WITHIN this segment's size bands, so the
  // color shows which employee size performs best per metric (independent of the
  // main per-bucket scale). "(no size)" is excluded from the ranking — a catch-all,
  // like the main table's "Other (no bucket)".
  const bandCells = data.bands.map(deriveCells);
  const rankedCells = data.bands
    .map((b, i) => ({ b, c: bandCells[i] }))
    .filter(({ b }) => b.sizeLabel !== "(no size)")
    .map(({ c }) => c);
  const colStats = {} as Record<CellKey, number[]>;
  for (const col of NUMERIC_COLUMNS) {
    const vals: number[] = [];
    for (const c of rankedCells) {
      const v = c[col.key];
      if (v !== null) vals.push(v);
    }
    vals.sort((a, b) => a - b);
    colStats[col.key] = vals;
  }

  return (
    <>
      <tr className={drillRow}>
        <td className={`${drillSticky} text-[11px] font-semibold text-zinc-600 dark:text-zinc-300`}>
          Company-size breakdown
        </td>
        <td colSpan={spanRest} className="px-3 py-1.5">
          <div className="flex items-center gap-2 flex-wrap text-[11px]">
            {reco ? (
              <span className="inline-flex items-center gap-1" title="Suggested from booked calls + lead quality + won, per size band vs the segment average">
                <span className="rounded border border-violet-500/40 px-1.5 py-0.5 font-semibold text-violet-600 dark:text-violet-400">
                  Suggested: {fmtRange(reco.minEmp, reco.maxEmp)}
                </span>
                <button
                  onClick={() => onApplyRange(reco.minEmp, reco.maxEmp)}
                  className="rounded border border-violet-500/40 px-1.5 py-0.5 font-semibold text-violet-600 dark:text-violet-400 hover:bg-violet-500/10"
                >
                  Apply
                </button>
              </span>
            ) : (
              <span className="text-zinc-400">Not enough conversion data for a suggestion</span>
            )}
            {(currentMin != null || currentMax != null) && (
              <button
                onClick={() => onApplyRange(null, null)}
                className="text-zinc-500 hover:underline"
              >
                Clear range
              </button>
            )}
            {pendingCount > 0 && (
              <span className="text-amber-600 dark:text-amber-400">
                {pendingCount} selected webinar{pendingCount === 1 ? "" : "s"} not computed yet —
                run Recompute to include {pendingCount === 1 ? "it" : "them"}.
              </span>
            )}
          </div>
        </td>
      </tr>
      {data.bands.map((b, i) => {
        const c = bandCells[i];
        const isNoSize = b.sizeLabel === "(no size)";
        const highlighted = inRange(b);
        const isLast = i === data.bands.length - 1;
        return (
          <tr
            key={b.sizeLabel}
            className={`${drillRow} ${isNoSize ? "text-zinc-400" : "text-zinc-700 dark:text-zinc-300"} ${
              isLast ? "border-b border-zinc-200 dark:border-zinc-800" : ""
            }`}
          >
            <td className={`${drillSticky} text-[11px] font-medium`}>
              <span className={`inline-block rounded px-1 ${highlighted ? "bg-violet-500/15 text-violet-700 dark:text-violet-300" : ""}`}>
                {b.sizeLabel}
              </span>
            </td>
            {/* Quality + Employee-range columns stay empty on band rows — both
                are segment-level settings, not per-size ones. */}
            <td className="border-l border-zinc-200 dark:border-zinc-800/60" />
            <td className="border-l border-zinc-200 dark:border-zinc-800/60" />
            {NUMERIC_COLUMNS.map((col, idx) => {
              const v = c[col.key];
              const sorted = colStats[col.key];
              const best = col.lowerIsBetter ? sorted[0] : sorted[sorted.length - 1];
              const bg = isNoSize ? undefined : heatBg(v, sorted, col.lowerIsBetter);
              return (
                <td
                  key={col.key}
                  style={bg ? { backgroundColor: bg } : undefined}
                  className={`px-3 py-1.5 text-right tabular-nums whitespace-nowrap ${
                    !isNoSize && sorted.length ? leaderCls(v, best) : ""
                  } ${isColGroupBoundary(idx) ? "border-l border-zinc-200 dark:border-zinc-800/60" : ""}`}
                >
                  {col.fmt(c)}
                </td>
              );
            })}
          </tr>
        );
      })}
    </>
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
      <td className="sticky left-0 z-20 bg-zinc-100 dark:bg-zinc-900 px-3 py-2 text-left font-bold text-zinc-900 dark:text-zinc-100">
        Total
        <span className="ml-2 font-normal text-[11px] text-zinc-500">
          {includedCount} webinar{includedCount === 1 ? "" : "s"}
        </span>
      </td>
      {/* Quality + Employee-range placeholders (non-numeric leading columns) */}
      <td className="bg-zinc-100 dark:bg-zinc-900 border-l border-zinc-200 dark:border-zinc-800/60" />
      <td className="bg-zinc-100 dark:bg-zinc-900 border-l border-zinc-200 dark:border-zinc-800/60" />
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
