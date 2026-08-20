"use client";

/**
 * Statistics Home — the read-at-a-glance view of the campaign.
 *
 * Everything here is drawn from the same snapshots the Statistics table reads,
 * so a number on a chart and the same number in the table can never disagree:
 * raw counts are summed across the selected webinars and the rates are derived
 * from those sums (never an average of per-webinar rates).
 *
 * The scope switch mirrors the three rows the table shows per webinar —
 * assigned lists / new joiners / overall — so a chart can be read at the same
 * scope the operator was just looking at.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  fetchStatisticsEmployeeCount,
  fetchStatisticsOverview,
  fetchStatisticsSegments,
  type EmployeeFunnelRow,
  type OverviewScopeKey,
  type OverviewWebinar,
  type SegmentFunnelRow,
  type StatisticsMetrics,
} from "@/lib/api";
import {
  BarChart,
  CAT,
  ChartCard,
  EmptyChart,
  FunnelChart,
  HBarChart,
  Legend,
  LineChart,
  QualityMixBar,
  QUALITY_TIERS,
  StackedBarChart,
  StatTile,
  TableView,
  VizStyles,
  fmtValue,
  type FunnelStage,
  type LineSeries,
} from "./charts";
import { RecomputeControl } from "./RecomputeControl";

/* ── metric helpers ────────────────────────────────────────────────────── */

const num = (v: number | null | undefined): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

function safeDiv(a: number | null, b: number | null): number | null {
  if (a == null || b == null || b === 0) return null;
  return a / b;
}

function safePer1k(a: number | null, b: number | null): number | null {
  if (a == null || b == null || b === 0) return null;
  return a / (b / 1000);
}

/** Raw counts the Home charts read. Summed across webinars, never averaged. */
const RAW_KEYS = [
  "invited", "actuallyUsed", "totalRegs", "totalAttended",
  "total10MinPlus", "total30MinPlus", "uniqueBookers", "totalBookings",
  "totalCallsDatePassed", "confirmed", "shows", "noShows", "canceled", "won",
  "qualified", "disqualified",
  "leadQualityGreat", "leadQualityOk", "leadQualityBarelyPassable", "leadQualityBadDq",
] as const;

type RawKey = (typeof RAW_KEYS)[number];
type Counts = Record<RawKey, number | null>;

function countsOf(m: StatisticsMetrics | undefined): Counts {
  const out = {} as Counts;
  for (const k of RAW_KEYS) out[k] = num(m?.[k]);
  return out;
}

function sumCounts(list: Counts[]): Counts {
  const out = {} as Counts;
  for (const k of RAW_KEYS) {
    let any = false, total = 0;
    for (const c of list) {
      const v = c[k];
      if (v != null) { any = true; total += v; }
    }
    out[k] = any ? total : null;
  }
  return out;
}

/** Rate denominator: contacts actually sent to, falling back to the planned
 * volume — the same rule the backend applies per row. */
function invitedBase(c: Counts): number | null {
  const au = c.actuallyUsed;
  return au == null || au === 0 ? c.invited : au;
}

/** Distinct booked contacts, falling back to total opportunities on snapshots
 * that predate unique-booker tracking. */
function bookers(c: Counts): number | null {
  return c.uniqueBookers ?? c.totalBookings;
}

function ratesOf(c: Counts) {
  const inv = invitedBase(c);
  return {
    invited: inv,
    regRate: safeDiv(c.totalRegs, inv),
    attOfRegs: safeDiv(c.totalAttended, c.totalRegs),
    stay10: safeDiv(c.total10MinPlus, c.totalAttended),
    stay30of10: safeDiv(c.total30MinPlus, c.total10MinPlus),
    bookPer1k: safePer1k(bookers(c), inv),
    bookOf10m: safeDiv(bookers(c), c.total10MinPlus),
    showRate: safeDiv(c.shows, c.totalCallsDatePassed),
    qualRate: safeDiv(c.qualified, c.shows),
    closeRate: safeDiv(c.won, c.shows),
  };
}

function funnelStages(c: Counts): FunnelStage[] {
  return [
    { label: "Invited", value: invitedBase(c), note: "—" },
    { label: "Registered", value: c.totalRegs },
    { label: "Attended", value: c.totalAttended },
    { label: "Watched 10 min+", value: c.total10MinPlus },
    { label: "Watched 30 min+", value: c.total30MinPlus },
    { label: "Booked a call", value: bookers(c) },
  ];
}

/** Lead-quality tiers keyed to the chart's segment keys. */
function qualityCounts(c: {
  leadQualityGreat: number | null; leadQualityOk: number | null;
  leadQualityBarelyPassable: number | null; leadQualityBadDq: number | null;
  uniqueBookers?: number | null; totalBookings?: number | null;
}): Record<string, number> {
  const great = c.leadQualityGreat ?? 0;
  const ok = c.leadQualityOk ?? 0;
  const barely = c.leadQualityBarelyPassable ?? 0;
  const bad = c.leadQualityBadDq ?? 0;
  const booked = c.uniqueBookers ?? c.totalBookings ?? 0;
  return {
    great, ok, barely, bad,
    unrated: Math.max(0, booked - great - ok - barely - bad),
  };
}

/* ── controls ──────────────────────────────────────────────────────────── */

const SCOPES: { key: OverviewScopeKey; label: string; hint: string }[] = [
  {
    key: "assigned", label: "Assigned lists",
    hint: "Only the lists planned for each webinar — the Statistics table's headline row.",
  },
  {
    key: "newJoiners", label: "New joiners",
    hint: "Assigned lists plus NO LIST DATA, with Nonjoiners (people already invited to an earlier webinar) excluded.",
  },
  {
    key: "overall", label: "Overall",
    hint: "Everything attributed to the webinar — assigned lists, Nonjoiners and NO LIST DATA.",
  },
];

const RANGES = [
  { key: "6", label: "Last 6", n: 6 },
  { key: "12", label: "Last 12", n: 12 },
  { key: "all", label: "All", n: 0 },
];

function Segmented<T extends string>({
  value, options, onChange,
}: {
  value: T;
  options: { key: T; label: string; hint?: string }[];
  onChange: (k: T) => void;
}) {
  return (
    <div className="inline-flex rounded-lg border border-zinc-200 dark:border-zinc-800 p-0.5 bg-zinc-50 dark:bg-zinc-900">
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          title={o.hint}
          onClick={() => onChange(o.key)}
          className={`px-2.5 py-1 text-[11px] font-semibold rounded-md transition-colors ${
            value === o.key
              ? "bg-white dark:bg-zinc-800 text-zinc-900 dark:text-zinc-100 shadow-sm"
              : "text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ── page ──────────────────────────────────────────────────────────────── */

export function HomeTab() {
  const [scope, setScope] = useState<OverviewScopeKey>("assigned");
  const [rangeKey, setRangeKey] = useState<string>("12");
  const [webinars, setWebinars] = useState<OverviewWebinar[]>([]);
  const [pendingIds, setPendingIds] = useState<string[]>([]);
  const [segments, setSegments] = useState<SegmentFunnelRow[]>([]);
  const [employee, setEmployee] = useState<EmployeeFunnelRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);

  /** All three reads, settled independently: a failure in the segment or
   * company-size rollup must not blank the webinar charts. Also re-run when a
   * recompute finishes, so the "pending snapshots" prompt actually refreshes
   * what it was warning about. */
  const load = useCallback(async () => {
    setError(null);
    try {
      const [ov, seg, emp] = await Promise.allSettled([
        fetchStatisticsOverview(),
        fetchStatisticsSegments(),
        fetchStatisticsEmployeeCount(),
      ]);
      if (ov.status === "fulfilled") {
        setWebinars(ov.value.webinars);
        setPendingIds(ov.value.pendingWebinarIds ?? []);
      } else {
        setError("Could not load the webinar series.");
      }
      if (seg.status === "fulfilled") setSegments(seg.value.segments ?? []);
      if (emp.status === "fulfilled") setEmployee(emp.value.byEmployee ?? []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /* The selected slice, oldest first (charts read left to right in time). */
  const shown = useMemo(() => {
    const n = RANGES.find((r) => r.key === rangeKey)?.n ?? 0;
    return n > 0 ? webinars.slice(-n) : webinars;
  }, [webinars, rangeKey]);

  const perWebinar = useMemo(
    () => shown.map((w) => ({ w, c: countsOf(w.scopes?.[scope]) })),
    [shown, scope],
  );

  const totals = useMemo(() => sumCounts(perWebinar.map((p) => p.c)), [perWebinar]);
  const totalRates = useMemo(() => ratesOf(totals), [totals]);

  const shortLabel = (w: OverviewWebinar) =>
    `W${w.number ?? "?"}${w.variantLabel ? "·" + w.variantLabel.slice(0, 3) : ""}`;

  /* ── the focused webinar (defaults to the most recent one shown) ── */
  const focus = useMemo(() => {
    const id = focusId ?? shown[shown.length - 1]?.webinarId ?? null;
    return perWebinar.find((p) => p.w.webinarId === id) ?? null;
  }, [focusId, shown, perWebinar]);

  /** Everything except the focused webinar — the bar it's measured against. */
  const focusPeerRates = useMemo(() => {
    if (!focus) return null;
    const peers = perWebinar.filter((p) => p.w.webinarId !== focus.w.webinarId);
    return peers.length ? ratesOf(sumCounts(peers.map((p) => p.c))) : null;
  }, [perWebinar, focus]);

  if (loading) {
    return (
      <div className="p-6 flex items-center gap-2 text-sm text-zinc-500">
        <span className="inline-block w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
        Loading statistics…
      </div>
    );
  }

  if (error && webinars.length === 0) {
    return <div className="p-6 text-sm text-red-500">{error}</div>;
  }

  if (webinars.length === 0) {
    return (
      <div className="p-6 text-sm text-zinc-500">
        No webinar snapshots yet. Run a recompute to build them, then reload this page.
        <div className="mt-3"><RecomputeControl onDone={load} /></div>
      </div>
    );
  }

  const scopeHint = SCOPES.find((s) => s.key === scope)?.hint ?? "";

  return (
    <div className="viz p-4 md:p-6 space-y-4 max-w-[1400px]">
      <VizStyles />

      {/* ── header + filters ───────────────────────────────────────── */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-bold text-zinc-900 dark:text-zinc-100">Statistics Home</h1>
          <p className="text-xs text-zinc-500 mt-0.5">
            {perWebinar.length} webinar{perWebinar.length === 1 ? "" : "s"} · {scopeHint}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Segmented value={scope} options={SCOPES} onChange={setScope} />
          <Segmented value={rangeKey} options={RANGES} onChange={setRangeKey} />
          <Link
            href="/statistics"
            className="px-2.5 py-1 text-[11px] font-semibold rounded-lg border border-zinc-200 dark:border-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
          >
            Open the table →
          </Link>
        </div>
      </div>

      {pendingIds.length > 0 && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-700 dark:text-amber-400 flex items-center justify-between gap-3">
          <span>
            {pendingIds.length} webinar{pendingIds.length === 1 ? " has" : "s have"} no snapshot yet and
            {pendingIds.length === 1 ? " is" : " are"} excluded from every chart below.
          </span>
          <RecomputeControl onDone={load} />
        </div>
      )}

      {/* ── headline numbers ───────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        <StatTile
          label="Invited"
          value={fmtValue(totals.invited != null || totals.actuallyUsed != null ? invitedBase(totals) : null, "int")}
          sub={totals.actuallyUsed ? "actually used" : "planned volume"}
        />
        <StatTile
          label="Registered"
          value={fmtValue(totals.totalRegs, "int")}
          sub={`${fmtValue(totalRates.regRate, "pct")} of invited`}
        />
        <StatTile
          label="Attended"
          value={fmtValue(totals.totalAttended, "int")}
          sub={`${fmtValue(totalRates.attOfRegs, "pct")} of registered`}
        />
        <StatTile
          label="Watched 30 min+"
          value={fmtValue(totals.total30MinPlus, "int")}
          sub={`${fmtValue(totalRates.stay30of10, "pct")} of 10 min watchers`}
        />
        <StatTile
          label="Booked calls"
          value={fmtValue(bookers(totals), "int")}
          sub={`${fmtValue(totalRates.bookPer1k, "per1k")} per 1k invited`}
        />
        <StatTile
          label="Shows"
          value={fmtValue(totals.shows, "int")}
          sub={`${fmtValue(totalRates.showRate, "pct")} of calls passed`}
        />
      </div>

      {/* ── funnel + quality mix ───────────────────────────────────── */}
      <div className="grid grid-cols-1 xl:grid-cols-[1.35fr_1fr] gap-4">
        <ChartCard
          title="Where the audience drops off"
          subtitle="Each bar is the share of the stage above it that made it through — so the shortest bar is the biggest drop."
        >
          <FunnelChart stages={funnelStages(totals)} />
          <TableView
            columns={["Stage", "Contacts", "Step-down"]}
            caption="Funnel stages across the selected webinars"
            rows={(() => {
              const st = funnelStages(totals);
              return st.map((s, i) => [
                s.label,
                fmtValue(s.value, "int"),
                i === 0
                  ? "—"
                  : fmtValue(safeDiv(s.value ?? null, st[i - 1].value ?? null), "pct"),
              ]);
            })()}
          />
        </ChartCard>

        <ChartCard
          title="Lead quality of booked calls"
          subtitle="How sales rated the calls this audience produced. Unrated calls have not been dispositioned yet."
        >
          <QualityMixBar counts={qualityCounts(totals)} />
          <div className="grid grid-cols-3 gap-2 mt-4">
            <StatTile
              label="Qualified"
              value={fmtValue(totalRates.qualRate, "pct")}
              sub={`${fmtValue(totals.qualified, "int")} of ${fmtValue(totals.shows, "int")} shows`}
            />
            <StatTile
              label="Show rate"
              value={fmtValue(totalRates.showRate, "pct")}
              sub={`${fmtValue(totals.totalCallsDatePassed, "int")} calls passed`}
            />
            <StatTile
              label="Won"
              value={fmtValue(totals.won, "int")}
              sub={`${fmtValue(totalRates.closeRate, "pct")} of shows`}
            />
          </div>
        </ChartCard>
      </div>

      {/* ── rate trends ─────────────────────────────────────────────
       * Two charts, not one: registration rate off cold outreach runs well
       * under 1% while the downstream rates run 20–100%. On a shared axis the
       * registration line would sit flat on the baseline and tell you nothing,
       * and a second y-axis is never the answer. */}
      <div className="grid grid-cols-1 xl:grid-cols-[1fr_1.4fr] gap-4">
        <ChartCard
          title="Registration rate by webinar"
          subtitle="Registrations as a share of everyone invited — its own scale, because it runs orders of magnitude below the rates beside it."
        >
          <LineChart
            xLabels={perWebinar.map((p) => shortLabel(p.w))}
            format="pct"
            series={
              [
                {
                  key: "reg", label: "Reg %", color: CAT[0],
                  values: perWebinar.map((p) => ratesOf(p.c).regRate),
                },
              ] satisfies LineSeries[]
            }
          />
        </ChartCard>

        <ChartCard
          title="Downstream conversion by webinar"
          subtitle="What happens after someone registers. All three share a scale. Hover for the exact numbers."
          right={
            <Legend
              items={[
                { label: "Att % of regs", color: CAT[1] },
                { label: "30 min of 10 min", color: CAT[2] },
                { label: "Show %", color: CAT[3] },
              ]}
            />
          }
        >
          <LineChart
            xLabels={perWebinar.map((p) => shortLabel(p.w))}
            format="pct"
            series={
              [
                { key: "att", label: "Att %", color: CAT[1], values: perWebinar.map((p) => ratesOf(p.c).attOfRegs) },
                { key: "s30", label: "30m", color: CAT[2], values: perWebinar.map((p) => ratesOf(p.c).stay30of10) },
                { key: "show", label: "Show %", color: CAT[3], values: perWebinar.map((p) => ratesOf(p.c).showRate) },
              ] satisfies LineSeries[]
            }
          />
          <TableView
            columns={["Webinar", "Date", "Reg %", "Att % of regs", "30 min of 10 min", "Show %"]}
            caption="Conversion rates per webinar"
            rows={perWebinar.map((p) => {
              const r = ratesOf(p.c);
              return [
                shortLabel(p.w), p.w.date ?? "—",
                fmtValue(r.regRate, "pct"), fmtValue(r.attOfRegs, "pct"),
                fmtValue(r.stay30of10, "pct"), fmtValue(r.showRate, "pct"),
              ];
            })}
          />
        </ChartCard>
      </div>

      {/* ── booking yield + quality per webinar ────────────────────── */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <ChartCard
          title="Booked calls per 1,000 invited"
          subtitle="Pipeline yield per webinar, normalised for send volume."
        >
          <BarChart
            valueLabel="Booked / 1k invited"
            format="per1k"
            bars={perWebinar.map((p) => ({
              label: shortLabel(p.w),
              value: ratesOf(p.c).bookPer1k,
              tipRows: [
                { label: "Booked / 1k invited", value: fmtValue(ratesOf(p.c).bookPer1k, "per1k") },
                { label: "Booked calls", value: fmtValue(bookers(p.c), "int") },
                { label: "Invited", value: fmtValue(invitedBase(p.c), "int") },
              ],
            }))}
          />
        </ChartCard>

        <ChartCard
          title="Lead quality by webinar"
          subtitle="Booked calls split by the rating sales gave them."
          right={<Legend items={QUALITY_TIERS.map((t) => ({ label: t.label, color: t.color }))} />}
        >
          <StackedBarChart
            segments={QUALITY_TIERS.map((t) => ({ key: t.key, label: t.label, color: t.color }))}
            categories={perWebinar.map((p) => ({
              label: shortLabel(p.w),
              values: qualityCounts(p.c),
            }))}
          />
          <TableView
            columns={["Webinar", ...QUALITY_TIERS.map((t) => t.label), "Qual %"]}
            caption="Lead quality per webinar"
            rows={perWebinar.map((p) => {
              const q = qualityCounts(p.c);
              return [
                shortLabel(p.w),
                ...QUALITY_TIERS.map((t) => q[t.key] ?? 0),
                fmtValue(ratesOf(p.c).qualRate, "pct"),
              ];
            })}
          />
        </ChartCard>
      </div>

      {/* ── segments + company size ────────────────────────────────── */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <ChartCard
          title="Booking yield by segment"
          subtitle="Booked calls per 1,000 invited, across every selected webinar. Top 12 segments by volume."
        >
          <SegmentBars segments={segments} />
        </ChartCard>

        <ChartCard
          title="Booking yield by company size"
          subtitle="Booked calls per 1,000 invited, by the contact's employee-count band."
        >
          <EmployeeBars rows={employee} />
        </ChartCard>
      </div>

      {/* ── one webinar in focus ───────────────────────────────────── */}
      {focus && (
        <ChartCard
          title={`Webinar ${focus.w.number ?? "?"}${focus.w.variantLabel ? " · " + focus.w.variantLabel : ""} in detail`}
          subtitle={`${focus.w.date ?? "no date"}${focus.w.title ? " · " + focus.w.title : ""} — measured against the other ${perWebinar.length - 1} webinar${perWebinar.length === 2 ? "" : "s"} in this range.`}
          right={
            <div className="flex items-center gap-2">
              <select
                value={focus.w.webinarId}
                onChange={(e) => setFocusId(e.target.value)}
                className="px-2 py-1 text-[11px] rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 text-zinc-700 dark:text-zinc-300"
              >
                {[...perWebinar].reverse().map((p) => (
                  <option key={p.w.webinarId} value={p.w.webinarId}>
                    {p.w.label ?? shortLabel(p.w)}
                  </option>
                ))}
              </select>
              <Link
                href={`/statistics/report/${focus.w.webinarId}`}
                target="_blank"
                className="px-2 py-1 text-[11px] font-semibold rounded-lg border border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-500/20 transition-colors"
              >
                Full report →
              </Link>
            </div>
          }
        >
          <div className="grid grid-cols-1 lg:grid-cols-[1.3fr_1fr] gap-6">
            <div>
              <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-2">Funnel</div>
              <FunnelChart stages={funnelStages(focus.c)} />
            </div>
            <div>
              <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-2">
                Lead quality of its booked calls
              </div>
              <QualityMixBar counts={qualityCounts(focus.c)} />
              <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mt-5 mb-2">
                Versus the rest of the range
              </div>
              <div className="grid grid-cols-2 gap-2">
                <VsTile label="Reg %" cur={ratesOf(focus.c).regRate} base={focusPeerRates?.regRate ?? null} />
                <VsTile label="Att % of regs" cur={ratesOf(focus.c).attOfRegs} base={focusPeerRates?.attOfRegs ?? null} />
                <VsTile label="30 min of 10 min" cur={ratesOf(focus.c).stay30of10} base={focusPeerRates?.stay30of10 ?? null} />
                <VsTile label="Booked / 1k inv" cur={ratesOf(focus.c).bookPer1k} base={focusPeerRates?.bookPer1k ?? null} fmt="per1k" />
                <VsTile label="Show %" cur={ratesOf(focus.c).showRate} base={focusPeerRates?.showRate ?? null} />
                <VsTile label="Qualified %" cur={ratesOf(focus.c).qualRate} base={focusPeerRates?.qualRate ?? null} />
              </div>
            </div>
          </div>
        </ChartCard>
      )}
    </div>
  );
}

/* ── sub-components ────────────────────────────────────────────────────── */

/** A rate next to the same rate for every other webinar in the range. The
 * arrow is paired with a signed number so direction never rests on colour. */
function VsTile({
  label, cur, base, fmt = "pct",
}: {
  label: string;
  cur: number | null;
  base: number | null;
  fmt?: "pct" | "per1k";
}) {
  const diff = cur != null && base != null ? cur - base : null;
  const flat = diff != null && Math.abs(diff) < (fmt === "pct" ? 0.0005 : 0.05);
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 px-2.5 py-1.5">
      <div className="text-[9px] uppercase tracking-wider text-zinc-500 font-semibold truncate">{label}</div>
      <div className="text-base font-bold text-zinc-900 dark:text-zinc-100 tabular-nums">
        {fmtValue(cur, fmt)}
      </div>
      <div className="text-[10px] tabular-nums">
        {diff == null ? (
          <span className="text-zinc-400">no comparison</span>
        ) : (
          <span className={flat ? "text-zinc-500" : diff > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-500"}>
            {flat ? "±" : diff > 0 ? "▲ +" : "▼ −"}
            {fmt === "pct" ? `${Math.abs(diff * 100).toFixed(1)}pp` : Math.abs(diff).toFixed(1)}
          </span>
        )}{" "}
        <span className="text-zinc-500">vs rest ({fmtValue(base, fmt)})</span>
      </div>
    </div>
  );
}

/** Show rate, or an honest "no calls yet" — a bare em dash beside the word
 * "show" reads as a rendering fault rather than an empty denominator.
 *
 * `callsPassed` is the real appointment-date count, the same denominator the
 * Statistics table's Show % uses. It reads 0 on snapshots written before the
 * field was plumbed through these rollups, which is indistinguishable from
 * "nothing has happened yet" — so both states say "no calls yet" until a
 * recompute rather than inventing a rate. */
function showLabel(r: { shows: number; callsPassed: number }): string {
  return r.callsPassed ? `${fmtValue(safeDiv(r.shows, r.callsPassed), "pct")} show` : "no calls yet";
}

function SegmentBars({ segments }: { segments: SegmentFunnelRow[] }) {
  const rows = useMemo(
    () =>
      segments
        .filter((s) => s.bucketId && s.invites > 0)
        .map((s) => ({
          label: s.bucketName ?? "Unnamed segment",
          value: safePer1k(s.bookings, s.invites),
          sub: `${fmtValue(s.invites, "int")} invited · ${showLabel(s)}`,
          tipRows: [
            { label: "Invited", value: fmtValue(s.invites, "int") },
            { label: "Booked", value: fmtValue(s.bookings, "int") },
            { label: "Show rate", value: fmtValue(safeDiv(s.shows, s.callsPassed), "pct") },
            { label: "Qualified", value: fmtValue(safeDiv(s.qualified, s.shows), "pct") },
          ],
        }))
        .sort((a, b) => (b.value ?? 0) - (a.value ?? 0))
        .slice(0, 12),
    [segments],
  );
  if (!rows.length) return <EmptyChart message="No segment data — run a recompute to build the snapshots." />;
  return (
    <>
      <HBarChart rows={rows} format="per1k" />
      <TableView
        columns={["Segment", "Booked / 1k invited"]}
        caption="Booking yield by segment"
        rows={rows.map((r) => [r.label, fmtValue(r.value, "per1k")])}
      />
    </>
  );
}

function EmployeeBars({ rows }: { rows: EmployeeFunnelRow[] }) {
  const bars = useMemo(
    () =>
      rows
        .filter((r) => r.invites > 0)
        .map((r) => ({
          label: r.bucket,
          value: safePer1k(r.bookings, r.invites),
          sub: `${fmtValue(r.invites, "int")} invited · ${showLabel(r)}`,
          tipRows: [
            { label: "Invited", value: fmtValue(r.invites, "int") },
            { label: "Booked", value: fmtValue(r.bookings, "int") },
            { label: "Show rate", value: fmtValue(safeDiv(r.shows, r.callsPassed), "pct") },
            { label: "Qualified", value: fmtValue(safeDiv(r.qualified, r.shows), "pct") },
          ],
        }))
        .sort((a, b) => (b.value ?? 0) - (a.value ?? 0))
        .slice(0, 12),
    [rows],
  );
  if (!bars.length) return <EmptyChart message="No company-size data — run a recompute to build the snapshots." />;
  return (
    <>
      <HBarChart rows={bars} format="per1k" />
      <TableView
        columns={["Company size", "Booked / 1k invited"]}
        caption="Booking yield by company size"
        rows={bars.map((r) => [r.label, fmtValue(r.value, "per1k")])}
      />
    </>
  );
}
