"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchStatisticsSegmentsV2,
  type SegmentsV2Response,
  type SegmentsV2Webinar,
} from "@/lib/api";
import { RecomputeControl } from "./RecomputeControl";

/* ── Formatting ─────────────────────────────────────────────────────────── */

const fmtInt = (n: number) => n.toLocaleString();

/** Ratio (0–1) → percent. Small rates keep 3 decimals because 10-minute
 * attendance lives around 0.06% of invites and rounds to "0.1%" otherwise. */
function fmtPct(r: number | null, decimals?: number): string {
  if (r === null || !isFinite(r)) return "—";
  const v = r * 100;
  return `${v.toFixed(decimals ?? (v < 1 ? 3 : v < 10 ? 2 : 1))}%`;
}
const fmtPer1k = (r: number) => r.toFixed(3);
const fmtScore = (n: number) => n.toFixed(2);
const safeDiv = (a: number, b: number): number | null => (b > 0 ? a / b : null);

/* ── The cube ───────────────────────────────────────────────────────────── */

/** Raw counts at one point of the cube. Every rate on the tab is derived from
 * these sums — never by averaging per-webinar rates. */
export type Counts = {
  inv: number; reg: number; atd: number; att: number;
  bk: number; cp: number; sh: number; wn: number; ql: number;
};
const COUNT_KEYS = ["inv", "reg", "atd", "att", "bk", "cp", "sh", "wn", "ql"] as const;
const zero = (): Counts => ({ inv: 0, reg: 0, atd: 0, att: 0, bk: 0, cp: 0, sh: 0, wn: 0, ql: 0 });
function addInto(a: Counts, b: Counts): Counts {
  for (const k of COUNT_KEYS) a[k] += b[k];
  return a;
}

/** The server names the packed count order in `metricKeys`; resolve it into
 * positions once so a reordering on the server can't silently shift columns. */
function countReader(metricKeys: string[]): (row: number[], from: number) => Counts {
  const at = (name: string) => {
    const i = metricKeys.indexOf(name);
    if (i < 0) throw new Error(`segments v2: the payload is missing metric "${name}"`);
    return i;
  };
  const idx = {
    inv: at("invites"), reg: at("regs"), atd: at("attended"), att: at("attendees10m"),
    bk: at("bookings"), cp: at("callsPassed"), sh: at("shows"), wn: at("won"),
    ql: at("qualified"),
  };
  return (row, from) => ({
    inv: row[from + idx.inv] || 0, reg: row[from + idx.reg] || 0,
    atd: row[from + idx.atd] || 0, att: row[from + idx.att] || 0,
    bk: row[from + idx.bk] || 0, cp: row[from + idx.cp] || 0,
    sh: row[from + idx.sh] || 0, wn: row[from + idx.wn] || 0,
    ql: row[from + idx.ql] || 0,
  });
}

/* ── Scoring ────────────────────────────────────────────────────────────── */

export type Weights = { reg: number; att: number; book: number; show: number; close: number };
/** Booking carries the most weight because it is the closest measurable step to
 * revenue; closing carries the least because the whole window holds only a
 * couple of dozen won deals, far too few to separate one segment from another. */
export const DEFAULT_WEIGHTS: Weights = { reg: 12, att: 23, book: 35, show: 15, close: 15 };

export const WEIGHT_META: { key: keyof Weights; label: string; why: string }[] = [
  { key: "book", label: "Books a call", why: "The closest measurable step to revenue" },
  { key: "att", label: "Attends 10 min", why: "The most reliable signal in the data" },
  { key: "show", label: "Shows up", why: "Keeps no-show-heavy lists honest" },
  { key: "close", label: "Closes", why: "Too few won deals to be more than a tiebreaker" },
  { key: "reg", label: "Registers", why: "Easiest to inflate — counts people who found us elsewhere" },
];

/** Evidence a row needs before it is graded at all, by level. A segment carries
 * more weight than one country inside it, which carries more than one size band
 * inside that, so each level needs proportionally less. */
export type Gates = { seg: number; reg: number; band: number };
export const DEFAULT_GATES: Gates = { seg: 25_000, reg: 8_000, band: 3_000 };

/** Shrinkage priors, in denominator units. A row's rate is blended with the
 * portfolio rate in proportion to how little data it has, so three lucky
 * bookings on 4,000 invites land near average instead of at the top. */
const PRIOR = { reg: 3_000, att: 8_000, book: 40_000, show: 8, close: 12 };
/** No single stage may carry more than 3× the portfolio average into the score
 * — mirrors BOOK_RATIO_CAP on the Segments tab, and stops one anomalous stage
 * (a list registering at 6× with ordinary attendance) owning the ranking. */
const INDEX_CAP = 3;

export type Bench = { reg: number; att: number; book: number; show: number; close: number };
export function benchmark(total: Counts): Bench {
  return {
    reg: total.inv > 0 ? total.reg / total.inv : 0,
    att: total.inv > 0 ? total.att / total.inv : 0,
    book: total.inv > 0 ? total.bk / total.inv : 0,
    show: total.cp > 0 ? total.sh / total.cp : 0,
    close: total.sh > 0 ? total.wn / total.sh : 0,
  };
}

export type Grade = "good" | "medium" | "bad" | "untested";
const GOOD_AT = 1.15;
const BAD_AT = 0.85;
export const GRADE_RANK: Record<Grade, number> = { good: 0, medium: 1, bad: 2, untested: 3 };
export const GRADE_ACTION: Record<Grade, string> = {
  good: "Scrape more",
  medium: "Keep as is",
  bad: "Stop buying",
  untested: "Test before buying",
};

export type Derived = Counts & {
  regRate: number | null;
  attRate: number | null;
  attOfReg: number | null;
  bookPer1k: number | null;
  showRate: number | null;
  closeRate: number | null;
  index: { reg: number; att: number; book: number; show: number; close: number };
  score: number;
  grade: Grade;
};

const cap = (x: number) => Math.min(x, INDEX_CAP);
const shrink = (x: number, n: number, p0: number, m: number) => (x + m * p0) / (n + m);

/** Derive every rate, index, score and grade for one point of the cube.
 * `gate` is the invite floor for this level; pass 0 for rows that are always
 * graded (a webinar is never "untested"). */
export function derive(c: Counts, bench: Bench, w: Weights, gate: number): Derived {
  const index = {
    reg: bench.reg > 0 ? cap(shrink(c.reg, c.inv, bench.reg, PRIOR.reg) / bench.reg) : 1,
    att: bench.att > 0 ? cap(shrink(c.att, c.inv, bench.att, PRIOR.att) / bench.att) : 1,
    book: bench.book > 0 ? cap(shrink(c.bk, c.inv, bench.book, PRIOR.book) / bench.book) : 1,
    show: bench.show > 0 ? cap(shrink(c.sh, c.cp, bench.show, PRIOR.show) / bench.show) : 1,
    close: bench.close > 0 ? cap(shrink(c.wn, c.sh, bench.close, PRIOR.close) / bench.close) : 1,
  };
  const wsum = w.reg + w.att + w.book + w.show + w.close;
  const score = wsum > 0
    ? (w.reg * index.reg + w.att * index.att + w.book * index.book
       + w.show * index.show + w.close * index.close) / wsum
    : 1;
  // Grade the score as it is printed, so a row never shows 1.15 next to "medium".
  const rounded = Math.round(score * 100) / 100;
  const grade: Grade = c.inv < gate
    ? "untested"
    : rounded >= GOOD_AT ? "good" : rounded <= BAD_AT ? "bad" : "medium";
  return {
    ...c,
    regRate: safeDiv(c.reg, c.inv),
    attRate: safeDiv(c.att, c.inv),
    attOfReg: safeDiv(c.att, c.reg),
    bookPer1k: c.inv > 0 ? (c.bk / c.inv) * 1000 : null,
    showRate: safeDiv(c.sh, c.cp),
    closeRate: safeDiv(c.wn, c.sh),
    index, score, grade,
  };
}

/* ── Rollups ────────────────────────────────────────────────────────────── */

type RegionNode = { agg: Counts; bands: Map<number, Counts> };
type SegNode = { agg: Counts; regions: Map<number, RegionNode> };
type WebinarNode = { agg: Counts; segs: Map<number, SegNode> };

type Model = {
  total: Counts;
  bench: Bench;
  bySeg: Map<number, Counts>;
  bySegRegion: Map<string, Counts>;
  bySegRegionBand: Map<string, Counts>;
  webinars: Map<number, WebinarNode>;
  corruptBands: Set<number>;
  noSizeBand: number;
  unknownRegion: number;
};

/** Decode the packed cube once per payload into the four rollups the reports
 * read, plus the per-webinar tree behind the composition view. */
function buildModel(d: SegmentsV2Response): Model {
  const read = countReader(d.metricKeys);
  const total = zero();
  const bySeg = new Map<number, Counts>();
  const bySegRegion = new Map<string, Counts>();
  const bySegRegionBand = new Map<string, Counts>();

  const bump = <K,>(m: Map<K, Counts>, k: K, c: Counts) => {
    const cur = m.get(k);
    if (cur) addInto(cur, c);
    else m.set(k, { ...c });
  };

  for (const row of d.cells) {
    const [s, r, b] = row;
    const c = read(row, 3);
    addInto(total, c);
    bump(bySeg, s, c);
    bump(bySegRegion, `${s}|${r}`, c);
    bump(bySegRegionBand, `${s}|${r}|${b}`, c);
  }

  const webinars = new Map<number, WebinarNode>();
  for (const row of d.webinarCells) {
    const [w, s, r, b] = row;
    const c = read(row, 4);
    let wn = webinars.get(w);
    if (!wn) webinars.set(w, (wn = { agg: zero(), segs: new Map() }));
    addInto(wn.agg, c);
    let sn = wn.segs.get(s);
    if (!sn) wn.segs.set(s, (sn = { agg: zero(), regions: new Map() }));
    addInto(sn.agg, c);
    let rn = sn.regions.get(r);
    if (!rn) sn.regions.set(r, (rn = { agg: zero(), bands: new Map() }));
    addInto(rn.agg, c);
    bump(rn.bands, b, c);
  }

  return {
    total,
    bench: benchmark(total),
    bySeg,
    bySegRegion,
    bySegRegionBand,
    webinars,
    corruptBands: new Set(d.corruptBands.map((b) => d.bands.indexOf(b)).filter((i) => i >= 0)),
    noSizeBand: d.bands.indexOf(d.noSizeBand),
    unknownRegion: d.regions.indexOf("(unknown)"),
  };
}

/* ── Tab ────────────────────────────────────────────────────────────────── */

const WEIGHTS_STORAGE_KEY = "segmentsV2.weights";

export function SegmentsV2Tab() {
  const [data, setData] = useState<SegmentsV2Response | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [weights, setWeights] = useState<Weights>(DEFAULT_WEIGHTS);

  // The operator's weighting is a viewing preference, so it lives in the
  // browser rather than on the server — nothing else depends on it.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(WEIGHTS_STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw) as Partial<Weights>;
      setWeights((w) => ({ ...w, ...parsed }));
    } catch {
      /* a blocked or corrupt store just means the defaults stand */
    }
  }, []);
  const updateWeights = useCallback((next: Weights) => {
    setWeights(next);
    try {
      localStorage.setItem(WEIGHTS_STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* non-fatal — the change still applies for this session */
    }
  }, []);

  const load = useCallback(async (ids: string[] | null, isRefresh: boolean) => {
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const d = await fetchStatisticsSegmentsV2(ids);
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
      load(ids.size === allIds.length ? null : Array.from(ids), true);
    },
    [allIds.length, load],
  );
  const refresh = useCallback(() => {
    const isAll = selected.size === allIds.length || selected.size === 0;
    load(isAll ? null : Array.from(selected), true);
  }, [selected, allIds.length, load]);

  const model = useMemo(() => (data ? buildModel(data) : null), [data]);

  if (loading) return <div className="px-6 py-5 text-xs text-zinc-500">Loading…</div>;
  if (error) {
    return (
      <div className="px-6 py-5">
        <div className="px-3 py-2 rounded-md bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
          {error}
        </div>
      </div>
    );
  }
  if (!data || !model) return null;

  const graded = data.includedWebinarIds.length - data.pendingWebinarIds.length;

  return (
    <div className="flex flex-col h-full">
      <div className="flex-none px-6 pt-5 pb-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              Segments v2 — what to scrape next
            </h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              Every segment graded good / medium / bad, then broken down by country and
              company size so a list can be bought at the level it actually performs at.
              Grades are computed from the weights below; percentages come from summed
              totals, never averaged per-webinar rates.
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
        <div className="flex-1 min-h-0 overflow-auto px-6 pb-10">
          {data.pendingWebinarIds.length > 0 && (
            <div className="mt-2 mb-3 px-3 py-2 rounded-md bg-amber-500/10 border border-amber-500/30 text-amber-600 dark:text-amber-400 text-xs">
              {data.pendingWebinarIds.length} of {data.includedWebinarIds.length} selected
              webinar{data.includedWebinarIds.length === 1 ? "" : "s"} has no segment × country
              × size cube yet — excluded from everything below. Click{" "}
              <span className="font-semibold">Recompute now</span> to build it.
            </div>
          )}

          <ScoringKey weights={weights} onChange={updateWeights} total={model.total} />

          <SegmentsReport data={data} model={model} weights={weights} graded={graded} />
          <TreeReport data={data} model={model} weights={weights} />
          <WebinarReport data={data} model={model} weights={weights} />
        </div>
      )}
    </div>
  );
}

/* ── Scoring key ────────────────────────────────────────────────────────── */

/** The weights that drive every grade on the tab, and what the score means.
 * Shown up front because a grade is only as readable as its inputs. */
function ScoringKey({
  weights,
  onChange,
  total,
}: {
  weights: Weights;
  onChange: (w: Weights) => void;
  total: Counts;
}) {
  const sum = WEIGHT_META.reduce((s, m) => s + weights[m.key], 0);
  const max = Math.max(1, ...WEIGHT_META.map((m) => weights[m.key]));
  const b = benchmark(total);
  return (
    <div className="mt-3 mb-5 grid gap-4 lg:grid-cols-2">
      <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="text-xs font-semibold text-zinc-800 dark:text-zinc-200">
            How each row is graded
          </h3>
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-zinc-500 tabular-nums">
              {sum === 100 ? "100%" : `${sum}% (rescaled)`}
            </span>
            <button
              onClick={() => onChange(DEFAULT_WEIGHTS)}
              className="text-[11px] text-violet-500 hover:underline"
            >
              Reset
            </button>
          </div>
        </div>
        <p className="text-[11px] text-zinc-500 mt-1 mb-3">
          Each funnel stage counts toward the grade in proportion to its weight. Drag one
          and every grade on the tab re-derives.
        </p>
        <div className="flex flex-col gap-2">
          {WEIGHT_META.map((m) => (
            <label key={m.key} className="grid grid-cols-[3rem_5rem_1fr] items-center gap-3">
              <span className="text-xs font-semibold text-violet-500 tabular-nums text-right">
                {sum > 0 ? Math.round((weights[m.key] / sum) * 100) : 0}%
              </span>
              <span className="h-1.5 rounded bg-zinc-200 dark:bg-zinc-800 overflow-hidden">
                <span
                  className="block h-full bg-violet-500"
                  style={{ width: `${(weights[m.key] / max) * 100}%` }}
                />
              </span>
              <span className="flex items-center gap-3 min-w-0">
                <input
                  type="range"
                  min={0}
                  max={50}
                  value={weights[m.key]}
                  onChange={(e) => onChange({ ...weights, [m.key]: Number(e.target.value) })}
                  className="w-24 accent-violet-500"
                  aria-label={`${m.label} weight`}
                />
                <span className="text-xs text-zinc-700 dark:text-zinc-200 whitespace-nowrap">
                  {m.label}
                </span>
                <span className="text-[11px] text-zinc-500 truncate">{m.why}</span>
              </span>
            </label>
          ))}
        </div>
      </div>

      <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4">
        <h3 className="text-xs font-semibold text-zinc-800 dark:text-zinc-200">
          What the score means
        </h3>
        <p className="text-[11px] text-zinc-500 mt-1 mb-3">
          The score is not a percentage — it is a multiple of our own average.{" "}
          <span className="font-semibold text-zinc-700 dark:text-zinc-300">1.00 is exactly average</span>{" "}
          across {fmtInt(total.inv)} invites, 1.50 is half as good again, 0.60 is 40% worse.
          Thin rows are pulled toward 1.00 in proportion to how little data they carry.
        </p>
        <div className="flex flex-col gap-1.5">
          {([
            ["good", "1.15 and up", "At least 15% better than average. Buy more of this."],
            ["medium", "0.85 – 1.15", "Within noise of average. Keep, don't expand."],
            ["bad", "0.85 and below", "At least 15% worse than average. Stop buying."],
            ["untested", "too little volume", "No evidence either way — not a bad result."],
          ] as [Grade, string, string][]).map(([g, range, why]) => (
            <div key={g} className="grid grid-cols-[5rem_7rem_1fr] items-baseline gap-2">
              <GradePill grade={g} />
              <span className="text-[11px] tabular-nums text-zinc-600 dark:text-zinc-300 font-semibold">
                {range}
              </span>
              <span className="text-[11px] text-zinc-500">{why}</span>
            </div>
          ))}
        </div>
        <p className="text-[11px] text-zinc-500 mt-3 pt-2.5 border-t border-zinc-200 dark:border-zinc-800">
          Portfolio average — reg {fmtPct(b.reg)} of invites, 10-minute attendance{" "}
          {fmtPct(b.att)} of invites, {fmtPer1k(b.book * 1000)} booked calls per 1,000, show
          rate {fmtPct(b.show, 1)}, close rate {fmtPct(b.close, 1)}.
        </p>
      </div>
    </div>
  );
}

/* ── Shared table pieces ────────────────────────────────────────────────── */

const COL = "px-2.5 py-1.5 text-right tabular-nums whitespace-nowrap";
const HEAD =
  "px-2.5 py-1.5 text-right whitespace-nowrap text-[10px] uppercase tracking-wide font-medium text-zinc-500 sticky top-0 z-10 bg-zinc-50 dark:bg-zinc-900 border-b border-zinc-200 dark:border-zinc-800";

const METRIC_HEADS: { label: string; title?: string }[] = [
  { label: "Invites" },
  { label: "Regs", title: "Registrations" },
  { label: "Reg %", title: "Registrations ÷ invites" },
  { label: "Attended", title: "Watched any amount" },
  { label: "10m+", title: "Watched 10 minutes or more" },
  { label: "10m % inv", title: "10-minute attendees ÷ invites" },
  { label: "10m % reg", title: "10-minute attendees ÷ registrations" },
  { label: "Book /1k", title: "Booked calls per 1,000 invites" },
  { label: "Books" },
  { label: "Held", title: "Calls whose date has passed and showed" },
  { label: "Won" },
  { label: "Score" },
];

function MetricHeads() {
  return (
    <>
      {METRIC_HEADS.map((h) => (
        <th key={h.label} className={HEAD} title={h.title}>
          {h.label}
        </th>
      ))}
    </>
  );
}

/** Subtle heat behind a rate, keyed off its index against the portfolio. */
function tint(idx: number | null | undefined): React.CSSProperties | undefined {
  if (idx == null || !isFinite(idx)) return undefined;
  const d = Math.max(-1, Math.min(1, (idx - 1) / 1.1));
  const a = Math.abs(d) * 0.18;
  if (a < 0.015) return undefined;
  return { backgroundColor: d >= 0 ? `rgba(16,185,129,${a})` : `rgba(244,63,94,${a})` };
}

function MetricCells({ d, dim }: { d: Derived; dim?: boolean }) {
  const t = dim ? "text-zinc-500" : "text-zinc-700 dark:text-zinc-200";
  return (
    <>
      <td className={`${COL} ${t}`}>{fmtInt(d.inv)}</td>
      <td className={`${COL} ${t}`}>{fmtInt(d.reg)}</td>
      <td className={`${COL} ${t}`} style={tint(d.index.reg)}>{fmtPct(d.regRate)}</td>
      <td className={`${COL} ${t}`}>{fmtInt(d.atd)}</td>
      <td className={`${COL} ${t}`}>{fmtInt(d.att)}</td>
      <td className={`${COL} ${t}`} style={tint(d.index.att)}>{fmtPct(d.attRate)}</td>
      <td className={`${COL} ${t}`}>{fmtPct(d.attOfReg, 1)}</td>
      <td className={`${COL} ${t}`} style={tint(d.index.book)}>
        {d.bookPer1k === null ? "—" : fmtPer1k(d.bookPer1k)}
      </td>
      <td className={`${COL} ${t}`}>{fmtInt(d.bk)}</td>
      <td className={`${COL} ${t}`}>{fmtInt(d.sh)}</td>
      <td className={`${COL} ${t}`}>{fmtInt(d.wn)}</td>
      <td className={`${COL} font-semibold text-zinc-800 dark:text-zinc-100`} style={tint(d.score)}>
        {fmtScore(d.score)}
      </td>
    </>
  );
}

const GRADE_STYLE: Record<Grade, string> = {
  good: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30",
  medium: "bg-zinc-500/10 text-zinc-600 dark:text-zinc-300 border-zinc-500/30",
  bad: "bg-rose-500/10 text-rose-600 dark:text-rose-400 border-rose-500/30",
  untested: "bg-transparent text-zinc-400 border-zinc-400/40 border-dashed",
};

function GradePill({ grade }: { grade: Grade }) {
  return (
    <span
      className={`inline-block px-1.5 py-0.5 rounded text-[10px] border ${GRADE_STYLE[grade]}`}
    >
      {grade}
    </span>
  );
}

/** Section wrapper: heading, one-line note, and a copy-to-clipboard action. */
function Report({
  n,
  title,
  note,
  rowsLabel,
  onCopy,
  children,
}: {
  n: number;
  title: string;
  note: React.ReactNode;
  rowsLabel?: string;
  onCopy: () => string[][];
  children: React.ReactNode;
}) {
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async () => {
    const rows = onCopy();
    try {
      await navigator.clipboard.writeText(rows.map((r) => r.join("\t")).join("\n"));
      setCopied(`Copied ${fmtInt(rows.length - 1)} rows`);
    } catch {
      setCopied("Copy blocked");
    }
    setTimeout(() => setCopied(null), 1800);
  };
  return (
    <section className="mb-8">
      <div className="flex items-baseline gap-3 border-t-2 border-zinc-800 dark:border-zinc-200 pt-2 mb-2">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          {n} · {title}
        </h3>
        <p className="text-[11px] text-zinc-500 flex-1">{note}</p>
        {rowsLabel && <span className="text-[11px] text-zinc-400 tabular-nums">{rowsLabel}</span>}
        <button
          onClick={copy}
          className="px-2.5 py-1 text-[11px] rounded-md bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-600 dark:text-zinc-300 shrink-0"
        >
          {copied ?? "Copy as TSV"}
        </button>
      </div>
      <div className="overflow-auto max-h-[80vh] rounded-lg border border-zinc-200 dark:border-zinc-800">
        {children}
      </div>
    </section>
  );
}

const TSV_METRICS = [
  "Invites", "Registrations", "Reg %", "Attended", "Attendees 10m+",
  "10m % of inv", "10m % of reg", "Book per 1k", "Books", "Held", "Won", "Score",
];
const tsvMetrics = (d: Derived) => [
  String(d.inv), String(d.reg), ((d.regRate ?? 0) * 100).toFixed(4),
  String(d.atd), String(d.att), ((d.attRate ?? 0) * 100).toFixed(4),
  ((d.attOfReg ?? 0) * 100).toFixed(2), (d.bookPer1k ?? 0).toFixed(4),
  String(d.bk), String(d.sh), String(d.wn), d.score.toFixed(3),
];

/* ── 1 · Segments, ranked ───────────────────────────────────────────────── */

function SegmentsReport({
  data,
  model,
  weights,
  graded,
}: {
  data: SegmentsV2Response;
  model: Model;
  weights: Weights;
  graded: number;
}) {
  const rows = useMemo(() => {
    const out = [...model.bySeg.entries()].map(([s, c]) => ({
      s,
      seg: data.segments[s],
      d: derive(c, model.bench, weights, DEFAULT_GATES.seg),
    }));
    out.sort((a, b) => GRADE_RANK[a.d.grade] - GRADE_RANK[b.d.grade] || b.d.score - a.d.score);
    return out;
  }, [data.segments, model, weights]);

  const copy = () => [
    ["Rank", "Grade", "Segment", "Action", "Manual mark", ...TSV_METRICS],
    ...rows.map((r, i) => [
      String(i + 1), r.d.grade, r.seg?.name ?? "", GRADE_ACTION[r.d.grade],
      r.seg?.quality ?? "", ...tsvMetrics(r.d),
    ]),
  ];

  return (
    <Report
      n={1}
      title="Segments, ranked"
      note={
        <>
          Good first, then medium, then bad — this is the buy order. Rolled up across{" "}
          {graded} webinar{graded === 1 ? "" : "s"}.
        </>
      }
      rowsLabel={`${rows.length} segments`}
      onCopy={copy}
    >
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr>
            <th className={`${HEAD} text-left`}>#</th>
            <th className={`${HEAD} text-left`}>Grade</th>
            <th className={`${HEAD} text-left`}>Segment</th>
            <th className={`${HEAD} text-left`}>Action</th>
            <MetricHeads />
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={r.s}
              className="border-b border-zinc-100 dark:border-zinc-800/60 hover:bg-zinc-50 dark:hover:bg-zinc-800/40"
            >
              <td className="px-2.5 py-1.5 text-left tabular-nums text-zinc-400">{i + 1}</td>
              <td className="px-2.5 py-1.5 text-left">
                <GradePill grade={r.d.grade} />
              </td>
              <td
                className="px-2.5 py-1.5 text-left font-medium text-zinc-800 dark:text-zinc-100 max-w-[18rem] truncate"
                title={r.seg?.name}
              >
                {r.seg?.name ?? "—"}
                {r.seg?.quality && (
                  <span
                    className="ml-1.5 text-[10px] text-zinc-400"
                    title="The manual mark set on this bucket in the Segments tab"
                  >
                    ({r.seg.quality})
                  </span>
                )}
              </td>
              <td
                className={`px-2.5 py-1.5 text-left text-[11px] ${
                  r.d.grade === "good"
                    ? "text-emerald-600 dark:text-emerald-400 font-medium"
                    : r.d.grade === "bad"
                      ? "text-rose-600 dark:text-rose-400"
                      : "text-zinc-500"
                }`}
              >
                {GRADE_ACTION[r.d.grade]}
              </td>
              <MetricCells d={r.d} />
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={16} className="px-3 py-8 text-center text-zinc-500">
                No segment cube yet — run a recompute.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </Report>
  );
}

/* ── 2 · Segment → country → company size ───────────────────────────────── */

type TreeBand = { b: number; label: string; d: Derived };
type TreeRegion = { r: number; label: string; d: Derived; bands: TreeBand[] };
type TreeSeg = { s: number; name: string; quality?: string | null; d: Derived; regions: TreeRegion[] };

function TreeReport({
  data,
  model,
  weights,
}: {
  data: SegmentsV2Response;
  model: Model;
  weights: Weights;
}) {
  // Sizes read in growing order by default — the operator buys by headcount, so
  // the band column is the one axis that is not ranked. Toggle to rank it.
  const [bandsByGrade, setBandsByGrade] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [showUngraded, setShowUngraded] = useState(false);

  const regionLabel = useCallback(
    (r: number) => (r === model.unknownRegion ? "no country" : data.regions[r]),
    [data.regions, model.unknownRegion],
  );
  const bandLabel = useCallback(
    (b: number) => (b === model.noSizeBand ? "no size" : data.bands[b]),
    [data.bands, model.noSizeBand],
  );

  const tree = useMemo<TreeSeg[]>(() => {
    const out: TreeSeg[] = [];
    for (const [s, sc] of model.bySeg) {
      const sd = derive(sc, model.bench, weights, DEFAULT_GATES.seg);
      if (!showUngraded && sd.grade === "untested") continue;
      const regions: TreeRegion[] = [];
      for (let r = 0; r < data.regions.length; r++) {
        const rc = model.bySegRegion.get(`${s}|${r}`);
        if (!rc) continue;
        const rd = derive(rc, model.bench, weights, DEFAULT_GATES.reg);
        if (!showUngraded && rd.grade === "untested") continue;
        const bands: TreeBand[] = [];
        for (let b = 0; b < data.bands.length; b++) {
          // Corrupt bands hold a misparsed date, not a headcount — they still
          // count in the segment and country totals above, but they are not a
          // size anyone can buy against.
          if (model.corruptBands.has(b)) continue;
          const bc = model.bySegRegionBand.get(`${s}|${r}|${b}`);
          if (!bc) continue;
          const bd = derive(bc, model.bench, weights, DEFAULT_GATES.band);
          if (!showUngraded && bd.grade === "untested") continue;
          bands.push({ b, label: bandLabel(b), d: bd });
        }
        bands.sort((x, y) =>
          bandsByGrade
            ? GRADE_RANK[x.d.grade] - GRADE_RANK[y.d.grade] || y.d.score - x.d.score
            : x.b - y.b,
        );
        regions.push({ r, label: regionLabel(r), d: rd, bands });
      }
      regions.sort(
        (x, y) => GRADE_RANK[x.d.grade] - GRADE_RANK[y.d.grade] || y.d.score - x.d.score,
      );
      out.push({
        s,
        name: data.segments[s]?.name ?? "—",
        quality: data.segments[s]?.quality,
        d: sd,
        regions,
      });
    }
    out.sort((a, b) => GRADE_RANK[a.d.grade] - GRADE_RANK[b.d.grade] || b.d.score - a.d.score);
    return out;
  }, [data, model, weights, showUngraded, bandsByGrade, regionLabel, bandLabel]);

  const rowCount = tree.reduce(
    (n, s) => n + 1 + (collapsed.has(s.s) ? 0 : s.regions.reduce((m, r) => m + 1 + r.bands.length, 0)),
    0,
  );

  const copy = () => {
    const rows: string[][] = [
      ["Segment", "Country", "Company size", "Grade", "Action", ...TSV_METRICS],
    ];
    for (const s of tree) {
      rows.push([s.name, "— all countries —", "— all sizes —", s.d.grade, GRADE_ACTION[s.d.grade], ...tsvMetrics(s.d)]);
      for (const r of s.regions) {
        rows.push([s.name, r.label, "— all sizes —", r.d.grade, GRADE_ACTION[r.d.grade], ...tsvMetrics(r.d)]);
        for (const b of r.bands) {
          rows.push([s.name, r.label, b.label, b.d.grade, GRADE_ACTION[b.d.grade], ...tsvMetrics(b.d)]);
        }
      }
    }
    return rows;
  };

  return (
    <Report
      n={2}
      title="Segment → country → company size"
      note={
        <>
          Countries ranked good to bad inside each segment; sizes in growing order. Every
          level is graded on its own numbers, so a segment can be bad overall and good in
          one country at one size.
        </>
      }
      rowsLabel={`${fmtInt(rowCount)} rows`}
      onCopy={copy}
    >
      <div className="flex items-center gap-2 px-2.5 py-2 border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 sticky top-0 z-20">
        <button
          onClick={() => setCollapsed(new Set())}
          className="px-2 py-0.5 text-[11px] rounded bg-zinc-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-300"
        >
          Expand all
        </button>
        <button
          onClick={() => setCollapsed(new Set(tree.map((s) => s.s)))}
          className="px-2 py-0.5 text-[11px] rounded bg-zinc-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-300"
        >
          Collapse all
        </button>
        <button
          onClick={() => setBandsByGrade((v) => !v)}
          className="px-2 py-0.5 text-[11px] rounded bg-zinc-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-300"
        >
          Sizes: {bandsByGrade ? "by grade" : "growing ▲"}
        </button>
        <label className="flex items-center gap-1.5 text-[11px] text-zinc-500 cursor-pointer ml-1">
          <input
            type="checkbox"
            checked={showUngraded}
            onChange={(e) => setShowUngraded(e.target.checked)}
            className="accent-violet-500"
          />
          Show untested rows
        </label>
        <span className="text-[11px] text-zinc-400 ml-auto">
          Graded at {fmtInt(DEFAULT_GATES.seg)} / {fmtInt(DEFAULT_GATES.reg)} /{" "}
          {fmtInt(DEFAULT_GATES.band)} invites
        </span>
      </div>
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr>
            <th className={`${HEAD} text-left`}>Segment</th>
            <th className={`${HEAD} text-left`}>Country</th>
            <th className={`${HEAD} text-left`}>Size</th>
            <th className={`${HEAD} text-left`}>Grade</th>
            <MetricHeads />
          </tr>
        </thead>
        <tbody>
          {(() => {
            // Flattened into one row list rather than nested fragments: a
            // fragment per parent would need its own key, and a flat list keeps
            // every row's key unique and stable across expand/collapse.
            const out: React.ReactNode[] = [];
            for (const s of tree) {
              const open = !collapsed.has(s.s);
              out.push(
                <tr
                  key={`s${s.s}`}
                  onClick={() =>
                    setCollapsed((prev) => {
                      const n = new Set(prev);
                      if (n.has(s.s)) n.delete(s.s);
                      else n.add(s.s);
                      return n;
                    })
                  }
                  className="cursor-pointer bg-zinc-50 dark:bg-zinc-900/60 border-t border-zinc-200 dark:border-zinc-800 hover:bg-zinc-100 dark:hover:bg-zinc-800/60"
                >
                  <td
                    className="px-2.5 py-1.5 text-left font-medium text-zinc-800 dark:text-zinc-100 max-w-[18rem] truncate"
                    title={s.name}
                  >
                    <span className="inline-block w-3.5 text-zinc-400">{open ? "▾" : "▸"}</span>
                    {s.name}
                  </td>
                  <td className="px-2.5 py-1.5 text-left text-[11px] text-zinc-400">all countries</td>
                  <td className="px-2.5 py-1.5 text-left text-[11px] text-zinc-400">all sizes</td>
                  <td className="px-2.5 py-1.5 text-left">
                    <GradePill grade={s.d.grade} />
                  </td>
                  <MetricCells d={s.d} />
                </tr>,
              );
              if (!open) continue;
              for (const r of s.regions) {
                out.push(
                  <tr
                    key={`s${s.s}r${r.r}`}
                    className="border-b border-zinc-100 dark:border-zinc-800/60 hover:bg-zinc-50 dark:hover:bg-zinc-800/40"
                  >
                    <td />
                    <td className="px-2.5 py-1.5 pl-6 text-left font-medium text-zinc-700 dark:text-zinc-200">
                      {r.label}
                    </td>
                    <td className="px-2.5 py-1.5 text-left text-[11px] text-zinc-400">all sizes</td>
                    <td className="px-2.5 py-1.5 text-left">
                      <GradePill grade={r.d.grade} />
                    </td>
                    <MetricCells d={r.d} />
                  </tr>,
                );
                for (const b of r.bands) {
                  out.push(
                    <tr
                      key={`s${s.s}r${r.r}b${b.b}`}
                      className="border-b border-zinc-100 dark:border-zinc-800/60 hover:bg-zinc-50 dark:hover:bg-zinc-800/40"
                    >
                      <td />
                      <td />
                      <td className="px-2.5 py-1.5 pl-6 text-left tabular-nums text-zinc-600 dark:text-zinc-300">
                        {b.label}
                      </td>
                      <td className="px-2.5 py-1.5 text-left">
                        <GradePill grade={b.d.grade} />
                      </td>
                      <MetricCells d={b.d} dim />
                    </tr>,
                  );
                }
              }
            }
            if (out.length === 0) {
              out.push(
                <tr key="empty">
                  <td colSpan={16} className="px-3 py-8 text-center text-zinc-500">
                    Nothing clears the evidence bar. Tick “Show untested rows” to see everything.
                  </td>
                </tr>,
              );
            }
            return out;
          })()}
        </tbody>
      </table>
      <div className="px-3 py-2.5 border-t border-zinc-200 dark:border-zinc-800 text-[11px] text-zinc-500 leading-relaxed">
        <p>
          <span className="font-semibold text-zinc-600 dark:text-zinc-300">
            Two size bands are excluded:
          </span>{" "}
          {data.corruptBands.join(" and ")} hold a misparsed date rather than a headcount
          (99.5% of those contacts came from one lead import). Their volume still counts in
          every segment and country total above, so the arithmetic reconciles — they are just
          not a size to buy against.{" "}
          <span className="font-semibold text-zinc-600 dark:text-zinc-300">
            “no size” and “no country” rows are real volume,
          </span>{" "}
          not errors: contacts we invited without that field filled in. A good grade there
          means the list works and we do not yet know which slice is doing the work.
        </p>
      </div>
    </Report>
  );
}

/* ── 3 · What each webinar was made of ──────────────────────────────────── */

/** Countries and sizes inside a single webinar have a long tail of 1–50 invite
 * slivers. Anything under this folds into one remainder row per parent, so the
 * sheet stays readable and the shares still add up to their parent. */
const MIN_WEBINAR_ROW = 250;
const GRADES: Grade[] = ["good", "medium", "bad", "untested"];
const MIX_COLOR: Record<Grade, string> = {
  good: "#10b981",
  medium: "#a1a1aa",
  bad: "#f43f5e",
  untested: "#3f3f46",
};

function webinarLabel(w: SegmentsV2Webinar): string {
  if (w.label) return w.label;
  return `W${w.number ?? "?"}${w.variantLabel ? ` · ${w.variantLabel}` : ""}`;
}

/** Stacked bar of a webinar's invite volume by the grade of the segment it went
 * to, with the split written out so it reads without hovering. */
function MixBar({ mix, counts }: { mix: Record<Grade, number>; counts: Record<Grade, number> }) {
  const present = GRADES.filter((g) => mix[g] > 0);
  return (
    <div className="flex flex-col gap-1 min-w-[11rem]">
      <div className="flex h-1.5 rounded overflow-hidden bg-zinc-200 dark:bg-zinc-800">
        {present.map((g) => (
          <span
            key={g}
            style={{ width: `${mix[g] * 100}%`, backgroundColor: MIX_COLOR[g] }}
            title={`${g}: ${fmtPct(mix[g], 1)} of invites across ${counts[g]} segment${counts[g] === 1 ? "" : "s"}`}
          />
        ))}
      </div>
      <div className="flex gap-2 text-[10px] tabular-nums text-zinc-500">
        {present.map((g) => (
          <span key={g} style={{ color: MIX_COLOR[g] }}>
            {g[0].toUpperCase()} {Math.round(mix[g] * 100)}%
            <span className="opacity-60"> ·{counts[g]}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

type WebRow = {
  wi: number;
  w: SegmentsV2Webinar;
  d: Derived;
  share: number;
  corruptShare: number;
  segCount: number;
  mix: Record<Grade, number>;
  mixCounts: Record<Grade, number>;
  node: WebinarNode;
};

function WebinarReport({
  data,
  model,
  weights,
}: {
  data: SegmentsV2Response;
  model: Model;
  weights: Weights;
}) {
  const [sortBy, setSortBy] = useState<"date" | "score" | "good">("date");
  const [open, setOpen] = useState<Set<string>>(new Set());

  const regionLabel = (r: number) => (r === model.unknownRegion ? "no country" : data.regions[r]);
  const bandLabel = (b: number) => (b === model.noSizeBand ? "no size" : data.bands[b]);
  const toggle = (k: string) =>
    setOpen((prev) => {
      const n = new Set(prev);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });

  // Grades are inherited from the whole-programme rollups: one webinar's slice
  // of one segment is far too thin to grade on its own, and the useful question
  // is "how much of this send went to something we already know is bad".
  const gradeOf = useMemo(() => {
    const seg = new Map<number, Grade>();
    const segReg = new Map<string, Grade>();
    const segRegBand = new Map<string, Grade>();
    for (const [s, c] of model.bySeg) seg.set(s, derive(c, model.bench, weights, DEFAULT_GATES.seg).grade);
    for (const [k, c] of model.bySegRegion) segReg.set(k, derive(c, model.bench, weights, DEFAULT_GATES.reg).grade);
    for (const [k, c] of model.bySegRegionBand)
      segRegBand.set(k, derive(c, model.bench, weights, DEFAULT_GATES.band).grade);
    return { seg, segReg, segRegBand };
  }, [model, weights]);

  const rows = useMemo<WebRow[]>(() => {
    const programme = model.total.inv;
    const out: WebRow[] = [];
    for (const [wi, node] of model.webinars) {
      const mixInv: Record<Grade, number> = { good: 0, medium: 0, bad: 0, untested: 0 };
      const mixCounts: Record<Grade, number> = { good: 0, medium: 0, bad: 0, untested: 0 };
      let corrupt = 0;
      for (const [s, sn] of node.segs) {
        const g = gradeOf.seg.get(s) ?? "untested";
        mixInv[g] += sn.agg.inv;
        mixCounts[g] += 1;
        for (const rn of sn.regions.values())
          for (const [b, bc] of rn.bands) if (model.corruptBands.has(b)) corrupt += bc.inv;
      }
      const mix: Record<Grade, number> = { good: 0, medium: 0, bad: 0, untested: 0 };
      for (const g of GRADES) mix[g] = node.agg.inv > 0 ? mixInv[g] / node.agg.inv : 0;
      out.push({
        wi,
        w: data.includedWebinars[wi] ?? { webinarId: String(wi) },
        // A webinar is never "untested" — gate 0.
        d: derive(node.agg, model.bench, weights, 0),
        share: programme > 0 ? node.agg.inv / programme : 0,
        corruptShare: node.agg.inv > 0 ? corrupt / node.agg.inv : 0,
        segCount: node.segs.size,
        mix,
        mixCounts,
        node,
      });
    }
    out.sort((a, b) =>
      sortBy === "score"
        ? b.d.score - a.d.score
        : sortBy === "good"
          ? b.mix.good - a.mix.good
          : (b.w.date ?? "").localeCompare(a.w.date ?? "") || b.d.inv - a.d.inv,
    );
    return out;
  }, [data.includedWebinars, model, weights, gradeOf, sortBy]);

  const copy = () => {
    const out: string[][] = [
      ["Webinar", "Segment", "Country", "Company size", "Grade", "Share of webinar", ...TSV_METRICS],
    ];
    for (const wr of rows) {
      out.push([
        webinarLabel(wr.w), "— all segments —", "", "",
        GRADES.filter((g) => wr.mixCounts[g]).map((g) => `${g} ${fmtPct(wr.mix[g], 1)}`).join(" / "),
        fmtPct(wr.share, 1), ...tsvMetrics(wr.d),
      ]);
      for (const [s, sn] of wr.node.segs) {
        const sd = derive(sn.agg, model.bench, weights, 0);
        const name = data.segments[s]?.name ?? "—";
        out.push([webinarLabel(wr.w), name, "", "", gradeOf.seg.get(s) ?? "untested",
          fmtPct(sn.agg.inv / wr.d.inv, 1), ...tsvMetrics(sd)]);
        for (const [r, rn] of sn.regions) {
          if (rn.agg.inv < MIN_WEBINAR_ROW) continue;
          const rd = derive(rn.agg, model.bench, weights, 0);
          out.push([webinarLabel(wr.w), name, regionLabel(r), "",
            gradeOf.segReg.get(`${s}|${r}`) ?? "untested",
            fmtPct(rn.agg.inv / wr.d.inv, 1), ...tsvMetrics(rd)]);
          for (const [b, bc] of rn.bands) {
            if (model.corruptBands.has(b) || bc.inv < MIN_WEBINAR_ROW) continue;
            const bd = derive(bc, model.bench, weights, 0);
            out.push([webinarLabel(wr.w), name, regionLabel(r), bandLabel(b),
              gradeOf.segRegBand.get(`${s}|${r}|${b}`) ?? "untested",
              fmtPct(bc.inv / wr.d.inv, 1), ...tsvMetrics(bd)]);
          }
        }
      }
    }
    return out;
  };

  const remainder = (
    key: string,
    indent: number,
    label: React.ReactNode,
    c: Counts,
    webinarInv: number,
    warn?: boolean,
  ) => {
    const d = derive(c, model.bench, weights, 0);
    const pads = [<td key="a" />, <td key="b" />, <td key="c" />].slice(0, indent);
    return (
      <tr
        key={key}
        className={`border-b border-zinc-100 dark:border-zinc-800/60 ${
          warn ? "bg-amber-500/5" : ""
        }`}
      >
        {pads}
        <td className="px-2.5 py-1.5 pl-6 text-left italic text-zinc-500">{label}</td>
        {indent < 3 && <td />}
        <td className="px-2.5 py-1.5 text-left">
          {warn ? (
            <span className="inline-block px-1.5 py-0.5 rounded text-[10px] border bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/30">
              bad source
            </span>
          ) : (
            <span className="text-[10px] text-zinc-400">mixed</span>
          )}
        </td>
        <td />
        <td className={`${COL} text-zinc-500`}>{fmtPct(webinarInv > 0 ? c.inv / webinarInv : null, 1)}</td>
        <MetricCells d={d} dim />
      </tr>
    );
  };

  return (
    <Report
      n={3}
      title="What each webinar was made of"
      note={
        <>
          The grade mix behind every send. Share is share of that webinar&apos;s invites at
          every level, so a segment and its countries describe the same slice.
        </>
      }
      rowsLabel={`${rows.length} webinars`}
      onCopy={copy}
    >
      <div className="flex items-center gap-2 px-2.5 py-2 border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 sticky top-0 z-20">
        {(["date", "score", "good"] as const).map((k) => (
          <button
            key={k}
            onClick={() => setSortBy(k)}
            className={`px-2 py-0.5 text-[11px] rounded ${
              sortBy === k
                ? "bg-violet-600 text-white font-semibold"
                : "bg-zinc-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-300"
            }`}
          >
            {k === "date" ? "Newest first" : k === "score" ? "Best first" : "Most good segments"}
          </button>
        ))}
        <button
          onClick={() => setOpen(new Set())}
          className="px-2 py-0.5 text-[11px] rounded bg-zinc-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-300"
        >
          Collapse all
        </button>
        <span className="text-[11px] text-zinc-400 ml-auto">
          Grades inherited from reports 1 and 2 · rows under {fmtInt(MIN_WEBINAR_ROW)} invites folded
        </span>
      </div>
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr>
            <th className={`${HEAD} text-left`}>Webinar</th>
            <th className={`${HEAD} text-left`}>Segment</th>
            <th className={`${HEAD} text-left`}>Country</th>
            <th className={`${HEAD} text-left`}>Size</th>
            <th className={`${HEAD} text-left`}>Grade / mix</th>
            <th className={HEAD} title="Share of this webinar's invites sitting in the corrupt-size bands, which trace to one lead import">
              Bad source
            </th>
            <th className={HEAD}>Share</th>
            <MetricHeads />
          </tr>
        </thead>
        <tbody>
          {(() => {
            const out: React.ReactNode[] = [];
            for (const wr of rows) {
              const wk = `w${wr.wi}`;
              const wOpen = open.has(wk);
              out.push(
                <tr
                  key={wk}
                  onClick={() => toggle(wk)}
                  className="cursor-pointer bg-zinc-50 dark:bg-zinc-900/60 border-t border-zinc-200 dark:border-zinc-800 hover:bg-zinc-100 dark:hover:bg-zinc-800/60"
                >
                  <td className="px-2.5 py-1.5 text-left font-medium text-zinc-800 dark:text-zinc-100 whitespace-nowrap">
                    <span className="inline-block w-3.5 text-zinc-400">{wOpen ? "▾" : "▸"}</span>
                    {webinarLabel(wr.w)}
                  </td>
                  <td className="px-2.5 py-1.5 text-left text-[11px] text-zinc-400">
                    {wr.segCount} segment{wr.segCount === 1 ? "" : "s"}
                  </td>
                  <td />
                  <td />
                  <td className="px-2.5 py-1.5 text-left">
                    <MixBar mix={wr.mix} counts={wr.mixCounts} />
                  </td>
                  <td
                    className={`${COL} ${
                      wr.corruptShare >= 0.25
                        ? "text-amber-600 dark:text-amber-400 font-semibold"
                        : "text-zinc-500"
                    }`}
                  >
                    {wr.corruptShare > 0 ? fmtPct(wr.corruptShare, 0) : "—"}
                  </td>
                  <td className={`${COL} text-zinc-500`}>{fmtPct(wr.share, 1)}</td>
                  <MetricCells d={wr.d} />
                </tr>,
              );
              if (!wOpen) continue;

              const segs = [...wr.node.segs.entries()].sort((a, b) => b[1].agg.inv - a[1].agg.inv);
              for (const [s, sn] of segs) {
                const sk = `${wk}|s${s}`;
                const sOpen = open.has(sk);
                const sd = derive(sn.agg, model.bench, weights, 0);
                out.push(
                  <tr
                    key={sk}
                    onClick={() => toggle(sk)}
                    className="cursor-pointer border-b border-zinc-100 dark:border-zinc-800/60 hover:bg-zinc-50 dark:hover:bg-zinc-800/40"
                  >
                    <td />
                    <td
                      className="px-2.5 py-1.5 pl-6 text-left font-medium text-zinc-700 dark:text-zinc-200 max-w-[16rem] truncate"
                      title={data.segments[s]?.name}
                    >
                      <span className="inline-block w-3.5 text-zinc-400">{sOpen ? "▾" : "▸"}</span>
                      {data.segments[s]?.name ?? "—"}
                    </td>
                    <td />
                    <td />
                    <td className="px-2.5 py-1.5 text-left">
                      <GradePill grade={gradeOf.seg.get(s) ?? "untested"} />
                    </td>
                    <td />
                    <td className={`${COL} font-medium text-zinc-600 dark:text-zinc-300`}>
                      {fmtPct(sn.agg.inv / wr.d.inv, 1)}
                    </td>
                    <MetricCells d={sd} />
                  </tr>,
                );
                if (!sOpen) continue;

                const regions = [...sn.regions.entries()].sort((a, b) => b[1].agg.inv - a[1].agg.inv);
                const tinyRegions = regions.filter(([, rn]) => rn.agg.inv < MIN_WEBINAR_ROW);
                for (const [r, rn] of regions.filter(([, x]) => x.agg.inv >= MIN_WEBINAR_ROW)) {
                  const rk = `${sk}|r${r}`;
                  const rOpen = open.has(rk);
                  const rd = derive(rn.agg, model.bench, weights, 0);
                  out.push(
                    <tr
                      key={rk}
                      onClick={() => toggle(rk)}
                      className="cursor-pointer border-b border-zinc-100 dark:border-zinc-800/60 hover:bg-zinc-50 dark:hover:bg-zinc-800/40"
                    >
                      <td />
                      <td />
                      <td className="px-2.5 py-1.5 pl-6 text-left font-medium text-zinc-700 dark:text-zinc-200">
                        <span className="inline-block w-3.5 text-zinc-400">{rOpen ? "▾" : "▸"}</span>
                        {regionLabel(r)}
                      </td>
                      <td />
                      <td className="px-2.5 py-1.5 text-left">
                        <GradePill grade={gradeOf.segReg.get(`${s}|${r}`) ?? "untested"} />
                      </td>
                      <td />
                      <td className={`${COL} font-medium text-zinc-600 dark:text-zinc-300`}>
                        {fmtPct(rn.agg.inv / wr.d.inv, 1)}
                      </td>
                      <MetricCells d={rd} />
                    </tr>,
                  );
                  if (!rOpen) continue;

                  const corrupt = zero();
                  const tinyBands = zero();
                  let tinyCount = 0;
                  const bands: [number, Counts][] = [];
                  for (const [b, bc] of rn.bands) {
                    if (model.corruptBands.has(b)) {
                      addInto(corrupt, bc);
                      continue;
                    }
                    if (bc.inv < MIN_WEBINAR_ROW) {
                      addInto(tinyBands, bc);
                      tinyCount += 1;
                      continue;
                    }
                    bands.push([b, bc]);
                  }
                  bands.sort((a, b) => a[0] - b[0]);
                  for (const [b, bc] of bands) {
                    out.push(
                      <tr
                        key={`${rk}|b${b}`}
                        className="border-b border-zinc-100 dark:border-zinc-800/60 hover:bg-zinc-50 dark:hover:bg-zinc-800/40"
                      >
                        <td />
                        <td />
                        <td />
                        <td className="px-2.5 py-1.5 pl-6 text-left tabular-nums text-zinc-600 dark:text-zinc-300">
                          {bandLabel(b)}
                        </td>
                        <td className="px-2.5 py-1.5 text-left">
                          <GradePill grade={gradeOf.segRegBand.get(`${s}|${r}|${b}`) ?? "untested"} />
                        </td>
                        <td />
                        <td className={`${COL} text-zinc-500`}>{fmtPct(bc.inv / wr.d.inv, 1)}</td>
                        <MetricCells d={derive(bc, model.bench, weights, 0)} dim />
                      </tr>,
                    );
                  }
                  if (tinyCount > 0)
                    out.push(
                      remainder(`${rk}|tiny`, 3, `${tinyCount} smaller size${tinyCount === 1 ? "" : "s"}`, tinyBands, wr.d.inv),
                    );
                  if (corrupt.inv > 0)
                    out.push(
                      remainder(`${rk}|corrupt`, 3, "corrupt size data (one lead import)", corrupt, wr.d.inv, true),
                    );
                }
                if (tinyRegions.length > 0) {
                  const rest = zero();
                  for (const [, rn] of tinyRegions) addInto(rest, rn.agg);
                  out.push(
                    remainder(
                      `${sk}|tiny`,
                      2,
                      `${tinyRegions.length} smaller ${tinyRegions.length === 1 ? "country" : "countries"}`,
                      rest,
                      wr.d.inv,
                    ),
                  );
                }
              }
            }
            return out;
          })()}
        </tbody>
      </table>
    </Report>
  );
}

/* ── Webinar filter ─────────────────────────────────────────────────────── */

function WebinarMultiSelect({
  options,
  selectedIds,
  onApply,
}: {
  options: SegmentsV2Webinar[];
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
    draft.size !== selectedIds.size || Array.from(draft).some((id) => !selectedIds.has(id));

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
            {options.map((w) => (
              <label
                key={w.webinarId}
                className="flex items-center gap-2 px-3 py-1.5 text-xs cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-800/60"
              >
                <input
                  type="checkbox"
                  checked={draft.has(w.webinarId)}
                  onChange={() => toggle(w.webinarId)}
                  className="accent-violet-500"
                />
                <span
                  className="text-zinc-700 dark:text-zinc-200 truncate"
                  title={w.title ?? webinarLabel(w)}
                >
                  {webinarLabel(w)}
                </span>
              </label>
            ))}
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
