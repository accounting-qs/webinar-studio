"use client";

/**
 * Per-webinar report page — renders the frozen report artifact built by
 * services.webinar_report (scorecard vs averages, funnel breakdowns, bookings
 * deep-dive, non-joiner package, AI insights at the bottom + caveats).
 *
 * Two presentations of the SAME payload (no recompute):
 *   V1 — the original dense tables.
 *   V2 — hero tiles, delta chips, invited-share bars, verdict cards.
 * A left sidebar lists webinars (latest first) for quick switching.
 *
 * If no report exists yet the GET schedules generation (2–4 min) and this page
 * polls status until it lands. "Regenerate" re-runs the whole pipeline.
 */

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiStatisticsWebinarSummary,
  ApiWebinarReport,
  WebinarReportFunnelCell,
  fetchStatisticsWebinarList,
  fetchWebinarReport,
  fetchWebinarReportStatus,
  triggerWebinarReport,
} from "@/lib/api";

/* ── formatting ──────────────────────────────────────────────────────────── */

function fmtInt(v: number | null | undefined): string {
  if (v == null) return "—";
  return Math.round(v).toLocaleString();
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtR1(v: number | null | undefined): string {
  if (v == null) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

type Fmt = "int" | "pct" | "ratio";

function fmtBy(v: number | null | undefined, fmt: Fmt): string {
  return fmt === "pct" ? fmtPct(v) : fmt === "ratio" ? fmtR1(v) : fmtInt(v);
}

function Delta({
  cur,
  base,
  fmt,
}: {
  cur: number | null | undefined;
  base: number | null | undefined;
  fmt: Fmt;
}) {
  if (cur == null || base == null) return <span className="text-zinc-400">—</span>;
  const diff = cur - base;
  const mag = Math.abs(diff);
  const zero =
    fmt === "pct" ? Math.round(mag * 1000) === 0 : fmt === "ratio" ? mag < 0.05 : Math.round(mag) === 0;
  if (zero) return <span className="text-zinc-400">±0</span>;
  const up = diff > 0;
  const text = fmt === "pct" ? `${(mag * 100).toFixed(1)}pp` : fmt === "ratio" ? mag.toFixed(1) : Math.round(mag).toLocaleString();
  return (
    <span className={`font-semibold ${up ? "text-emerald-600 dark:text-emerald-400" : "text-red-500"}`}>
      {up ? "▲" : "▼"}
      {text}
    </span>
  );
}

/** "+18% vs 4-wk avg (1,229)" style relative delta line for hero tiles. */
function RelDelta({
  cur,
  base,
  label,
  fmt = "int",
}: {
  cur: number | null | undefined;
  base: number | null | undefined;
  label: string;
  fmt?: Fmt;
}) {
  if (cur == null || base == null || !base) {
    return <div className="text-[11px] text-zinc-400">no {label}</div>;
  }
  const rel = (cur - base) / Math.abs(base);
  const up = rel > 0;
  const near = Math.abs(rel) < 0.005;
  return (
    <div className="text-[11px] text-zinc-500">
      <span
        className={
          near
            ? "text-zinc-400 font-semibold"
            : up
              ? "text-emerald-600 dark:text-emerald-400 font-semibold"
              : "text-red-500 font-semibold"
        }
      >
        {near ? "±0%" : `${up ? "+" : "−"}${Math.abs(rel * 100).toFixed(0)}%`}
      </span>{" "}
      vs {label} ({fmtBy(base, fmt)})
    </div>
  );
}

/* ── shared building blocks ──────────────────────────────────────────────── */

function SectionHeading({ title, subtitle, id }: { title: string; subtitle?: string; id?: string }) {
  return (
    <div className="mt-8 mb-3 scroll-mt-16" id={id}>
      <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">{title}</h2>
      {subtitle && <p className="text-xs text-zinc-500 mt-0.5">{subtitle}</p>}
    </div>
  );
}

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900/60 p-4 mb-4">
      {children}
    </div>
  );
}

const TH = "px-2.5 py-1.5 text-left text-[10px] font-bold uppercase tracking-wider text-zinc-500 border-b-2 border-zinc-200 dark:border-zinc-700 whitespace-nowrap";
const TD = "px-2.5 py-1.5 text-[13px] text-zinc-800 dark:text-zinc-200 border-b border-zinc-100 dark:border-zinc-800 whitespace-nowrap tabular-nums";

/* ── payload constants ───────────────────────────────────────────────────── */

const SCORECARD_ROWS: { label: string; key: string; fmt: Fmt }[] = [
  { label: "Invited", key: "invited", fmt: "int" },
  { label: "Net-new registrations", key: "netNewRegs", fmt: "int" },
  { label: "Net-new reg rate", key: "regRate", fmt: "pct" },
  { label: "Non-joiner registrations", key: "nonjoinerRegs", fmt: "int" },
  { label: "No-list-data registrations", key: "noListDataRegs", fmt: "int" },
  { label: "Total registrations", key: "totalRegs", fmt: "int" },
  { label: "Yes marks", key: "yesMarked", fmt: "int" },
  { label: "Maybe marks", key: "maybeMarked", fmt: "int" },
  { label: "Live attendance", key: "totalAttended", fmt: "int" },
  { label: "Attendance % of regs", key: "attendRateOfRegs", fmt: "pct" },
  { label: "Attendees / 10k invited", key: "attendPer10kInvited", fmt: "ratio" },
  { label: "Booked contacts", key: "uniqueBookers", fmt: "int" },
];

const FUNNEL_DIMS: { dim: string; title: string }[] = [
  { dim: "segments", title: "Segments (buckets)" },
  { dim: "industry", title: "Industry" },
  { dim: "geography", title: "Geography" },
  { dim: "employeeSize", title: "Employee size" },
];

type ReportPayload = NonNullable<ApiWebinarReport["payload"]>;

/* ── main page ───────────────────────────────────────────────────────────── */

export function WebinarReportPage({ webinarId }: { webinarId: string }) {
  const [report, setReport] = useState<ApiWebinarReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [view, setView] = useState<"v1" | "v2">("v2");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    const stored = typeof window !== "undefined" ? window.localStorage.getItem("webinarReportView") : null;
    if (stored === "v1" || stored === "v2") setView(stored);
  }, []);

  const pickView = (v: "v1" | "v2") => {
    setView(v);
    try {
      window.localStorage.setItem("webinarReportView", v);
    } catch {
      /* private mode */
    }
  };

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const load = useCallback(async () => {
    try {
      const r = await fetchWebinarReport(webinarId);
      setReport(r);
      setError(null);
      if (r.payload && !r.status.running) {
        setGenerating(false);
        stopPolling();
        return;
      }
      setGenerating(true);
      setPhase(r.status.phase);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [webinarId, stopPolling]);

  const startPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const s = await fetchWebinarReportStatus(webinarId);
        setPhase(s.phase);
        if (!s.running) {
          stopPolling();
          setGenerating(false);
          await load();
        }
      } catch {
        /* transient poll errors are fine */
      }
    }, 5000);
  }, [webinarId, load, stopPolling]);

  useEffect(() => {
    setReport(null);
    setGenerating(false);
    stopPolling();
    void load();
    return stopPolling;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [webinarId]);

  useEffect(() => {
    if (generating && !pollRef.current) startPolling();
  }, [generating, startPolling]);

  const regenerate = async () => {
    try {
      setGenerating(true);
      setPhase("queries");
      await triggerWebinarReport(webinarId);
      startPolling();
    } catch (e) {
      setGenerating(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const payload = report?.payload ?? null;
  const label = payload
    ? `W${payload.number}${payload.variantLabel ? ` · ${payload.variantLabel}` : ""}`
    : "";

  return (
    <div className="flex max-w-[1400px] mx-auto">
      <WebinarSidebar activeId={webinarId} />

      <div className="flex-1 min-w-0 px-4 py-6 max-w-5xl">
        {/* header */}
        <div className="flex items-start justify-between gap-4 mb-2">
          <div>
            <div className="text-[11px] font-bold uppercase tracking-[0.15em] text-violet-500 mb-1">
              Webinar report
            </div>
            <h1 className="text-xl font-semibold text-zinc-900 dark:text-zinc-100">
              {payload ? `${label} — ${payload.date}` : "Loading report…"}
            </h1>
            {report?.generatedAt && (
              <p className="text-xs text-zinc-500 mt-1">
                Generated {new Date(report.generatedAt).toLocaleString()}
                {report.generationMs != null && ` · ${(report.generationMs / 1000).toFixed(0)}s compute`}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <div className="inline-flex rounded-md border border-zinc-300 dark:border-zinc-700 overflow-hidden">
              {(["v1", "v2"] as const).map((v) => (
                <button
                  key={v}
                  onClick={() => pickView(v)}
                  className={`px-2.5 py-1.5 text-xs font-semibold transition-colors ${
                    view === v
                      ? "bg-violet-500/15 text-violet-600 dark:text-violet-300"
                      : "text-zinc-500 hover:bg-zinc-500/10"
                  }`}
                  title={v === "v1" ? "Original dense-table layout" : "Redesigned layout — tiles, bars, verdicts"}
                >
                  {v.toUpperCase()}
                </button>
              ))}
            </div>
            <button
              onClick={regenerate}
              disabled={generating}
              className="px-3 py-1.5 text-xs font-semibold rounded bg-zinc-500/15 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-500/25 border border-zinc-400/30 transition-colors disabled:opacity-50 inline-flex items-center gap-1.5"
            >
              {generating ? (
                <>
                  <span className="w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
                  {phase === "ai" ? "Writing insights…" : "Crunching numbers…"}
                </>
              ) : (
                "Regenerate"
              )}
            </button>
          </div>
        </div>

        {error && (
          <div className="rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-900 text-red-700 dark:text-red-300 text-sm px-3 py-2 mb-4">
            {error}
          </div>
        )}

        {!payload && !error && (
          <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900/60 p-10 text-center">
            <div className="w-6 h-6 border-2 border-violet-500 border-t-transparent rounded-full animate-spin mx-auto mb-3" />
            <p className="text-sm text-zinc-600 dark:text-zinc-300">
              Generating this webinar&apos;s report — usually 2–4 minutes.
            </p>
            <p className="text-xs text-zinc-500 mt-1">
              {phase === "ai" ? "Numbers done — writing AI insights…" : "Running the funnel queries…"}
            </p>
          </div>
        )}

        {payload &&
          (view === "v1" ? (
            <V1Body payload={payload} report={report} />
          ) : (
            <V2Body payload={payload} report={report} />
          ))}
      </div>
    </div>
  );
}

/* ── sidebar: webinar switcher ───────────────────────────────────────────── */

function WebinarSidebar({ activeId }: { activeId: string }) {
  const [items, setItems] = useState<ApiStatisticsWebinarSummary[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { webinars } = await fetchStatisticsWebinarList("auto");
        if (cancelled) return;
        const today = new Date().toISOString().slice(0, 10);
        const list = webinars
          .filter((w) => w.webinarId && w.date && w.date <= today)
          .sort((a, b) =>
            (b.date ?? "").localeCompare(a.date ?? "") || (b.number ?? 0) - (a.number ?? 0),
          );
        setItems(list);
      } catch {
        setItems([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <aside className="hidden lg:block w-56 shrink-0 border-r border-zinc-200 dark:border-zinc-800 py-6 pr-2 pl-4">
      <div className="sticky top-16 max-h-[calc(100vh-5rem)] overflow-y-auto pr-2">
        <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-2 px-2">
          Webinars
        </div>
        {items === null && (
          <div className="px-2 py-3">
            <span className="inline-block w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
          </div>
        )}
        {items?.map((w) => {
          const active = w.webinarId === activeId;
          return (
            <Link
              key={w.id}
              href={`/statistics/report/${w.webinarId}`}
              className={`block rounded-md px-2 py-1.5 mb-0.5 text-[13px] transition-colors ${
                active
                  ? "bg-violet-500/15 text-violet-700 dark:text-violet-300 font-semibold"
                  : "text-zinc-600 dark:text-zinc-400 hover:bg-zinc-500/10"
              }`}
            >
              <span className="tabular-nums">W{w.number}</span>
              {w.variantLabel ? <span className="text-[11px]"> · {w.variantLabel}</span> : null}
              <span className={`block text-[11px] ${active ? "text-violet-500/80" : "text-zinc-400"}`}>
                {w.date}
              </span>
            </Link>
          );
        })}
        {items?.length === 0 && <p className="px-2 text-xs text-zinc-400">No webinars found.</p>}
      </div>
    </aside>
  );
}

/* ═══════════════════════════════ V2 ═══════════════════════════════════════ */

function TocChips() {
  const chips = [
    ["scorecard", "Scorecard"],
    ["funnels", "Funnels"],
    ["bookings", "Bookings"],
    ["nonjoiners", "Non-joiners"],
    ["insights", "Insights"],
    ["caveats", "Caveats"],
  ] as const;
  return (
    <div className="flex flex-wrap gap-1.5 mt-3 mb-1">
      {chips.map(([id, label]) => (
        <a
          key={id}
          href={`#${id}`}
          className="px-2 py-0.5 text-[11px] font-semibold rounded-full border border-zinc-300 dark:border-zinc-700 text-zinc-500 hover:text-violet-600 hover:border-violet-400 transition-colors"
        >
          {label}
        </a>
      ))}
    </div>
  );
}

function HeroTile({
  label,
  value,
  cur,
  all,
  w4,
  fmt = "int",
  sub,
  allLabel,
}: {
  label: string;
  value: string;
  cur: number | null | undefined;
  all: number | null | undefined;
  w4: number | null | undefined;
  fmt?: Fmt;
  sub?: string;
  allLabel?: string;
}) {
  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900/60 px-4 py-3">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-bold">{label}</div>
      <div className="text-[28px] leading-9 font-bold tabular-nums text-zinc-900 dark:text-zinc-100">
        {value}
      </div>
      <RelDelta cur={cur} base={w4} label="4-wk avg" fmt={fmt} />
      <RelDelta cur={cur} base={all} label={allLabel ?? "10-webinar avg"} fmt={fmt} />
      {sub && <div className="text-[11px] text-zinc-400 mt-0.5">{sub}</div>}
    </div>
  );
}

function V2Body({ payload, report }: { payload: ReportPayload; report: ApiWebinarReport | null }) {
  const cur = payload.scorecard.current ?? {};
  const all = payload.scorecard.baselineAll ?? null;
  const w4 = payload.scorecard.baseline4w ?? null;
  const bk = payload.bookings ?? {};
  const nj = payload.nonjoiners ?? {};

  const njRegMultiple =
    nj.regRate != null && nj.netNewRegRateOfInvited ? nj.regRate / nj.netNewRegRateOfInvited : null;
  const njAttFraction =
    nj.attendRateOfRegs != null && nj.netNewAttendRateOfRegs
      ? nj.attendRateOfRegs / nj.netNewAttendRateOfRegs
      : null;

  return (
    <>
      <TocChips />

      {/* hero tiles */}
      <SectionHeading
        id="scorecard"
        title="This webinar vs the average webinar"
        subtitle={`Baselines: the last ${all?.webinarCount ?? 0} webinars and the ${w4?.webinarCount ?? 0} in the 4 weeks before this one.`}
      />
      <div className="grid grid-cols-2 xl:grid-cols-4 gap-3 mb-4">
        <HeroTile
          allLabel={`last-${all?.webinarCount ?? 10} avg`}
          label="Net-new registrations"
          value={fmtInt(cur.netNewRegs)}
          cur={cur.netNewRegs}
          all={all?.netNewRegs}
          w4={w4?.netNewRegs}
          sub={`${fmtPct(cur.regRate)} of ${fmtInt(cur.invited)} invited`}
        />
        <HeroTile
          allLabel={`last-${all?.webinarCount ?? 10} avg`}
          label="Total registrations"
          value={fmtInt(cur.totalRegs)}
          cur={cur.totalRegs}
          all={all?.totalRegs}
          w4={w4?.totalRegs}
          sub={`${fmtInt(cur.nonjoinerRegs)} non-joiner · ${fmtInt(cur.noListDataRegs)} no-list`}
        />
        <HeroTile
          allLabel={`last-${all?.webinarCount ?? 10} avg`}
          label="Live attendance"
          value={fmtInt(cur.totalAttended)}
          cur={cur.totalAttended}
          all={all?.totalAttended}
          w4={w4?.totalAttended}
          sub={`${fmtPct(cur.attendRateOfRegs)} of regs · ${fmtR1(cur.attendPer10kInvited)} per 10k invited`}
        />
        <HeroTile
          allLabel={`last-${all?.webinarCount ?? 10} avg`}
          label="Booked contacts"
          value={fmtInt(bk.uniqueBookedContacts ?? cur.uniqueBookers)}
          cur={cur.uniqueBookers}
          all={all?.uniqueBookers}
          w4={w4?.uniqueBookers}
          sub={
            bk.callStatus?.confirmed
              ? `${bk.callStatus.confirmed} calls not yet held`
              : undefined
          }
        />
      </div>

      {/* compact scorecard table */}
      <Card>
        <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-2">
          Full scorecard
        </div>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className={TH}>Metric</th>
                <th className={`${TH} text-right`}>This webinar</th>
                <th className={`${TH} text-right`}>Last-{all?.webinarCount ?? 0} avg</th>
                <th className={`${TH} text-right`}>Δ</th>
                <th className={`${TH} text-right`}>4-wk avg ({w4?.webinarCount ?? 0})</th>
                <th className={`${TH} text-right`}>Δ</th>
              </tr>
            </thead>
            <tbody>
              {SCORECARD_ROWS.map((row, i) => {
                const c = cur[row.key] ?? null;
                const a = all?.[row.key] ?? null;
                const b = w4?.[row.key] ?? null;
                const zebra = i % 2 ? "bg-zinc-50/70 dark:bg-zinc-800/20" : "";
                return (
                  <tr key={row.key} className={zebra}>
                    <td className={`${TD} text-zinc-500`}>{row.label}</td>
                    <td className={`${TD} text-right font-bold bg-violet-500/5`}>{fmtBy(c, row.fmt)}</td>
                    <td className={`${TD} text-right`}>{fmtBy(a, row.fmt)}</td>
                    <td className={`${TD} text-right`}>
                      <Delta cur={c} base={a} fmt={row.fmt} />
                    </td>
                    <td className={`${TD} text-right`}>{fmtBy(b, row.fmt)}</td>
                    <td className={`${TD} text-right`}>
                      <Delta cur={c} base={b} fmt={row.fmt} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {/* funnels */}
      <SectionHeading
        id="funnels"
        title="Funnel breakdowns"
        subtitle={`Where the invites went and what they produced — each dimension vs the last-${
          payload.funnels?.industry?.baselineWebinarCount ??
          payload.funnels?.employeeSize?.baselineWebinarCount ??
          0
        }-webinar average. Bars show invited share.`}
      />
      {FUNNEL_DIMS.map(({ dim, title }) => {
        const block = payload.funnels?.[dim];
        if (!block || !block.cells?.length) return null;
        return <V2FunnelCard key={dim} title={title} cells={block.cells} />;
      })}

      {/* bookings */}
      <SectionHeading
        id="bookings"
        title="Bookings deep-dive"
        subtitle="Unique booked contacts from the booking-attribution layer — a rebooked contact counts once."
      />
      <V2BookingsCard payload={payload} />

      {/* non-joiners */}
      <SectionHeading
        id="nonjoiners"
        title="Non-joiner package"
        subtitle={`Pool = registered in any of the last ${nj.windowWebinars ?? 6} webinars, never attended live, not on this week's lists.`}
      />
      <div className="grid gap-2.5 mb-4">
        <div className="grid grid-cols-[34px_1fr] gap-3 items-start rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900/60 px-4 py-3">
          <div className="text-lg font-bold text-emerald-600 dark:text-emerald-400 text-center">✓</div>
          <p className="text-[13.5px] text-zinc-700 dark:text-zinc-300 leading-relaxed">
            <b>Registration works:</b> {fmtInt(nj.regs)} of the {fmtInt(nj.poolSize)} pool re-registered
            ({fmtPct(nj.regRate)})
            {njRegMultiple ? (
              <> — about <b>{Math.round(njRegMultiple)}×</b> the cold-invite rate ({fmtPct(nj.netNewRegRateOfInvited)}).</>
            ) : (
              "."
            )}
          </p>
        </div>
        <div className="grid grid-cols-[34px_1fr] gap-3 items-start rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900/60 px-4 py-3">
          <div className="text-lg font-bold text-red-500 text-center">✗</div>
          <p className="text-[13.5px] text-zinc-700 dark:text-zinc-300 leading-relaxed">
            <b>Attendance doesn&apos;t:</b> only {fmtInt(nj.attended)} attended ({fmtPct(nj.attendRateOfRegs)} of
            NJ regs) vs {fmtPct(nj.netNewAttendRateOfRegs)} for net-new registrants
            {njAttFraction ? <> — roughly <b>{Math.round((1 / njAttFraction) * 10) / 10}× lower</b>.</> : "."}
          </p>
        </div>
      </div>

      {/* insights */}
      <SectionHeading id="insights" title="AI insights" />
      <InsightsBlock report={report} />

      {/* caveats */}
      {payload.caveats?.length > 0 && (
        <>
          <SectionHeading id="caveats" title="Data notes & caveats" />
          <Card>
            {payload.caveats.map((c, i) => (
              <p key={i} className="text-xs text-zinc-500 leading-relaxed mb-1.5">
                • {c}
              </p>
            ))}
          </Card>
        </>
      )}
    </>
  );
}

/** Value with a small delta chip stacked below — easier to scan than inline. */
function CellWithDelta({
  cur,
  base,
  fmt,
}: {
  cur: number | null | undefined;
  base: number | null | undefined;
  fmt: Fmt;
}) {
  return (
    <div className="text-right">
      <div className="text-[13px] font-semibold tabular-nums text-zinc-800 dark:text-zinc-200">
        {fmtBy(cur, fmt)}
      </div>
      <div className="text-[10.5px] leading-3">
        <Delta cur={cur} base={base} fmt={fmt} />
      </div>
    </div>
  );
}

function V2FunnelCard({ title, cells }: { title: string; cells: WebinarReportFunnelCell[] }) {
  const maxInvited = Math.max(...cells.map((c) => c.current?.invited ?? 0), 1);
  const shown = cells.slice(0, 10);
  return (
    <Card>
      <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-2.5">{title}</div>
      <div className="grid grid-cols-[minmax(160px,1.6fr)_repeat(3,minmax(84px,1fr))] gap-x-4 items-center">
        <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-400 pb-1.5">
          {title} · invited
        </div>
        <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-400 pb-1.5 text-right">
          Reg rate
        </div>
        <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-400 pb-1.5 text-right">
          Att % of regs
        </div>
        <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-400 pb-1.5 text-right">
          Att / 10k inv
        </div>
        {shown.map((cell) => {
          const c = cell.current ?? ({} as WebinarReportFunnelCell["current"]);
          const b = cell.baseline ?? ({} as WebinarReportFunnelCell["baseline"]);
          const pct = Math.min(100, ((c.invited ?? 0) / maxInvited) * 100);
          return (
            <div key={cell.key} className="contents">
              <div className="py-1.5 border-t border-zinc-100 dark:border-zinc-800 min-w-0">
                <div className="flex justify-between gap-2">
                  <span className="text-[13px] text-zinc-800 dark:text-zinc-200 truncate">{cell.key}</span>
                  <span className="text-[12px] text-zinc-400 tabular-nums shrink-0">{fmtInt(c.invited)}</span>
                </div>
                <div className="h-1 mt-1 bg-zinc-100 dark:bg-zinc-800 rounded-full">
                  <div className="h-1 bg-violet-500 rounded-full" style={{ width: `${pct}%` }} />
                </div>
              </div>
              <div className="py-1.5 border-t border-zinc-100 dark:border-zinc-800">
                <CellWithDelta cur={c.regRate} base={b.regRate} fmt="pct" />
              </div>
              <div className="py-1.5 border-t border-zinc-100 dark:border-zinc-800">
                <CellWithDelta cur={c.attPctOfRegs} base={b.attPctOfRegs} fmt="pct" />
              </div>
              <div className="py-1.5 border-t border-zinc-100 dark:border-zinc-800">
                <CellWithDelta cur={c.attendeesPer10kInv} base={b.attendeesPer10kInv} fmt="ratio" />
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

const QUALITY_SEGMENTS = [
  { key: "great", label: "Great", cls: "bg-emerald-500" },
  { key: "ok", label: "Ok", cls: "bg-lime-500" },
  { key: "barely", label: "Barely", cls: "bg-amber-500" },
  { key: "bad", label: "Bad/DQ", cls: "bg-red-500" },
  { key: "unrated", label: "Unrated", cls: "bg-zinc-300 dark:bg-zinc-700" },
] as const;

function V2BookingsCard({ payload }: { payload: ReportPayload }) {
  const bk = payload.bookings ?? {};
  const q = bk.quality ?? {};
  const st = bk.callStatus ?? {};
  const origin = bk.origin ?? {};
  const total = bk.uniqueBookedContacts ?? 0;
  const qTotal = QUALITY_SEGMENTS.reduce((s, seg) => s + (q[seg.key] ?? 0), 0) || 1;

  return (
    <Card>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4">
        <Stat label="Booked contacts" value={fmtInt(total)} />
        <Stat
          label="Implied close rate"
          value={fmtPct(bk.impliedCloseRate)}
          sub={`${bk.rated ?? 0} rated · Great 25% / Ok 13% / Barely 5%`}
        />
        <Stat
          label="Showed / no-show / cancelled"
          value={`${st.showed ?? 0} / ${st.noShow ?? 0} / ${st.cancelled ?? 0}`}
          sub={`${st.confirmed ?? 0} calls upcoming`}
        />
        <Stat label="Rated so far" value={`${bk.rated ?? 0} of ${total}`} sub="quality is directional below ~10" />
      </div>

      {/* quality mix bar */}
      <div className="mb-4">
        <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-1.5">
          Lead-quality mix
        </div>
        <div className="flex h-3 rounded-full overflow-hidden gap-px">
          {QUALITY_SEGMENTS.map((seg) => {
            const v = q[seg.key] ?? 0;
            if (!v) return null;
            return (
              <div
                key={seg.key}
                className={seg.cls}
                style={{ width: `${(v / qTotal) * 100}%` }}
                title={`${seg.label}: ${v}`}
              />
            );
          })}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 mt-1.5">
          {QUALITY_SEGMENTS.map((seg) => (
            <span key={seg.key} className="text-[11px] text-zinc-500 inline-flex items-center gap-1">
              <span className={`inline-block w-2 h-2 rounded-sm ${seg.cls}`} />
              {seg.label} {q[seg.key] ?? 0}
            </span>
          ))}
        </div>
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <div>
          <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-1.5">
            Booking origin
          </div>
          {[
            ["Net-new registrants", origin.netNew],
            ["Non-joiners", origin.nonjoiner],
            ["No-list-data registrants", origin.noListData],
            ["Not a registrant (series carryover)", origin.notRegistrant],
          ].map(([label, v]) => (
            <div
              key={String(label)}
              className="flex justify-between text-[13px] py-1 border-b border-zinc-100 dark:border-zinc-800"
            >
              <span className="text-zinc-500">{label}</span>
              <span className="font-semibold tabular-nums">{fmtInt(v as number)}</span>
            </div>
          ))}
        </div>
        <div>
          <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-1.5">
            Lead sources of bookers
          </div>
          {(bk.leadSources ?? []).slice(0, 6).map((s) => (
            <div
              key={s.source}
              className="flex justify-between text-[13px] py-1 border-b border-zinc-100 dark:border-zinc-800"
            >
              <span className="text-zinc-500 truncate pr-3">{s.source}</span>
              <span className="font-semibold tabular-nums">{s.count}</span>
            </div>
          ))}
          {!bk.leadSources?.length && <p className="text-xs text-zinc-500">No matched sources.</p>}
        </div>
      </div>
    </Card>
  );
}

function InsightsBlock({ report }: { report: ApiWebinarReport | null }) {
  return (
    <>
      <div className="rounded-lg border border-amber-400/60 bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-300 text-xs px-3 py-2 mb-3">
        ⚠️ AI-generated insights (beta) — not always accurate. Verify against the numbers above before
        acting on them.
      </div>
      {report?.insights?.length ? (
        <Card>
          {report.insights.map((group, i) => (
            <div key={i} className={i ? "mt-4" : ""}>
              <div className="text-xs font-bold uppercase tracking-wider text-zinc-700 dark:text-zinc-200 mb-1.5">
                {group.title}
              </div>
              {group.bullets.map((b, j) => (
                <p key={j} className="text-[13px] leading-relaxed text-zinc-700 dark:text-zinc-300 mb-1.5">
                  <span className="text-zinc-400">•</span> {b}
                </p>
              ))}
            </div>
          ))}
          {report.insightsModel && (
            <p className="text-[10px] text-zinc-400 mt-3">Generated by {report.insightsModel}</p>
          )}
        </Card>
      ) : (
        <Card>
          <p className="text-sm text-zinc-500">
            {report?.aiError
              ? `Insights unavailable: ${report.aiError}`
              : "Insights were not generated for this report."}
          </p>
        </Card>
      )}
    </>
  );
}

/* ═══════════════════════════════ V1 (original) ════════════════════════════ */

function V1Body({ payload, report }: { payload: ReportPayload; report: ApiWebinarReport | null }) {
  return (
    <>
      <div className="inline-flex items-center gap-1.5 mt-2 px-2 py-0.5 rounded-full border border-zinc-300 dark:border-zinc-700 text-[11px] font-semibold text-zinc-500">
        V1 · original layout
      </div>

      {/* 1 — scorecard */}
      <SectionHeading
        title="Scorecard — vs the average webinar"
        subtitle="Baselines: the last 10 webinars and the 4 weeks before this one — not just the previous webinar."
      />
      <Card>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className={TH}>Metric</th>
                <th className={TH}>This webinar</th>
                <th className={TH}>Last-{payload.scorecard.baselineAll?.webinarCount ?? 0} avg</th>
                <th className={TH}>Δ</th>
                <th className={TH}>4-wk avg ({payload.scorecard.baseline4w?.webinarCount ?? 0})</th>
                <th className={TH}>Δ</th>
              </tr>
            </thead>
            <tbody>
              {SCORECARD_ROWS.map((row) => {
                const cur = payload.scorecard.current?.[row.key] ?? null;
                const all = payload.scorecard.baselineAll?.[row.key] ?? null;
                const w4 = payload.scorecard.baseline4w?.[row.key] ?? null;
                return (
                  <tr key={row.key}>
                    <td className={`${TD} text-zinc-500`}>{row.label}</td>
                    <td className={`${TD} font-bold`}>{fmtBy(cur, row.fmt)}</td>
                    <td className={TD}>{fmtBy(all, row.fmt)}</td>
                    <td className={TD}>
                      <Delta cur={cur} base={all} fmt={row.fmt} />
                    </td>
                    <td className={TD}>{fmtBy(w4, row.fmt)}</td>
                    <td className={TD}>
                      <Delta cur={cur} base={w4} fmt={row.fmt} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {/* 2 — funnels */}
      <SectionHeading
        title="Funnel breakdowns"
        subtitle={`Each dimension vs the last-${
          payload.funnels?.industry?.baselineWebinarCount ??
          payload.funnels?.employeeSize?.baselineWebinarCount ??
          0
        }-webinar average (per-webinar averages; rates from pooled counts).`}
      />
      {FUNNEL_DIMS.map(({ dim, title }) => {
        const block = payload.funnels?.[dim];
        if (!block || !block.cells?.length) return null;
        return <V1FunnelCard key={dim} title={title} cells={block.cells} />;
      })}

      {/* 3 — bookings */}
      <SectionHeading
        title="Bookings deep-dive"
        subtitle="Unique booked contacts from the booking-attribution layer — a rebooked contact counts once."
      />
      <V1BookingsCard payload={payload} />

      {/* 4 — non-joiners */}
      <SectionHeading
        title="Non-joiner package"
        subtitle={`Pool = registered in any of the last ${payload.nonjoiners?.windowWebinars ?? 6} webinars, never attended live, not on this week's lists.`}
      />
      <Card>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <Stat label="Pool size" value={fmtInt(payload.nonjoiners?.poolSize)} />
          <Stat
            label="Re-registered"
            value={fmtInt(payload.nonjoiners?.regs)}
            sub={`${fmtPct(payload.nonjoiners?.regRate)} of pool`}
          />
          <Stat
            label="NJ attendance"
            value={fmtPct(payload.nonjoiners?.attendRateOfRegs)}
            sub={`${fmtInt(payload.nonjoiners?.attended)} attended of ${fmtInt(payload.nonjoiners?.regs)} regs`}
          />
          <Stat
            label="Net-new attendance"
            value={fmtPct(payload.nonjoiners?.netNewAttendRateOfRegs)}
            sub={`vs ${fmtPct(payload.nonjoiners?.netNewRegRateOfInvited)} reg rate of invited`}
          />
        </div>
      </Card>

      {/* 5 — insights */}
      <SectionHeading title="AI insights" />
      <InsightsBlock report={report} />

      {/* 6 — caveats */}
      {payload.caveats?.length > 0 && (
        <>
          <SectionHeading title="Data notes & caveats" />
          <Card>
            {payload.caveats.map((c, i) => (
              <p key={i} className="text-xs text-zinc-500 leading-relaxed mb-1.5">
                • {c}
              </p>
            ))}
          </Card>
        </>
      )}
    </>
  );
}

function V1FunnelCard({ title, cells }: { title: string; cells: WebinarReportFunnelCell[] }) {
  const maxInvited = Math.max(...cells.map((c) => c.current?.invited ?? 0), 1);
  return (
    <Card>
      <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-2">{title}</div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className={TH}>{title}</th>
              <th className={TH}>Invited</th>
              <th className={TH}>Reg rate</th>
              <th className={TH}>Att % of regs</th>
              <th className={TH}>Att / 10k inv</th>
            </tr>
          </thead>
          <tbody>
            {cells.map((cell) => {
              const c = cell.current ?? ({} as WebinarReportFunnelCell["current"]);
              const b = cell.baseline ?? ({} as WebinarReportFunnelCell["baseline"]);
              const pct = Math.min(100, ((c.invited ?? 0) / maxInvited) * 100);
              return (
                <tr key={cell.key}>
                  <td className={`${TD} min-w-[180px]`}>
                    <div className="text-[13px]">{cell.key}</div>
                    <div className="h-1 mt-1 bg-zinc-100 dark:bg-zinc-800 rounded-full">
                      <div className="h-1 bg-violet-500 rounded-full" style={{ width: `${pct}%` }} />
                    </div>
                  </td>
                  <td className={TD}>{fmtInt(c.invited)}</td>
                  <td className={TD}>
                    {fmtPct(c.regRate)} <Delta cur={c.regRate} base={b.regRate} fmt="pct" />
                  </td>
                  <td className={TD}>
                    {fmtPct(c.attPctOfRegs)} <Delta cur={c.attPctOfRegs} base={b.attPctOfRegs} fmt="pct" />
                  </td>
                  <td className={TD}>
                    {fmtR1(c.attendeesPer10kInv)}{" "}
                    <Delta cur={c.attendeesPer10kInv} base={b.attendeesPer10kInv} fmt="ratio" />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-zinc-500 font-semibold">{label}</div>
      <div className="text-lg font-bold tabular-nums text-zinc-900 dark:text-zinc-100">{value}</div>
      {sub && <div className="text-[10px] text-zinc-500">{sub}</div>}
    </div>
  );
}

function V1BookingsCard({ payload }: { payload: ReportPayload }) {
  const bk = payload.bookings ?? {};
  const q = bk.quality ?? {};
  const st = bk.callStatus ?? {};
  const origin = bk.origin ?? {};
  return (
    <Card>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4">
        <Stat label="Booked contacts" value={fmtInt(bk.uniqueBookedContacts)} />
        <Stat
          label="Implied close rate"
          value={fmtPct(bk.impliedCloseRate)}
          sub={`${bk.rated ?? 0} rated · Great 25% / Ok 13% / Barely 5%`}
        />
        <Stat
          label="Call status"
          value={`${st.showed ?? 0} / ${st.noShow ?? 0} / ${st.cancelled ?? 0}`}
          sub={`showed / no-show / cancelled · ${st.confirmed ?? 0} upcoming`}
        />
        <Stat
          label="Quality mix"
          value={`${q.great ?? 0}G ${q.ok ?? 0}O ${q.barely ?? 0}B ${q.bad ?? 0}D`}
          sub={`${q.unrated ?? 0} unrated`}
        />
      </div>
      <div className="grid md:grid-cols-2 gap-4">
        <div>
          <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-1.5">
            Booking origin
          </div>
          {[
            ["Net-new registrants", origin.netNew],
            ["Non-joiners", origin.nonjoiner],
            ["No-list-data registrants", origin.noListData],
            ["Not a registrant (series carryover)", origin.notRegistrant],
          ].map(([label, v]) => (
            <div
              key={String(label)}
              className="flex justify-between text-[13px] py-1 border-b border-zinc-100 dark:border-zinc-800"
            >
              <span className="text-zinc-500">{label}</span>
              <span className="font-semibold tabular-nums">{fmtInt(v as number)}</span>
            </div>
          ))}
        </div>
        <div>
          <div className="text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-1.5">
            Lead sources of bookers
          </div>
          {(bk.leadSources ?? []).slice(0, 6).map((s) => (
            <div
              key={s.source}
              className="flex justify-between text-[13px] py-1 border-b border-zinc-100 dark:border-zinc-800"
            >
              <span className="text-zinc-500 truncate pr-3">{s.source}</span>
              <span className="font-semibold tabular-nums">{s.count}</span>
            </div>
          ))}
          {!bk.leadSources?.length && <p className="text-xs text-zinc-500">No matched sources.</p>}
        </div>
      </div>
    </Card>
  );
}
