"use client";

/**
 * Per-webinar report page — renders the frozen report artifact built by
 * services.webinar_report (scorecard vs averages, funnel breakdowns, bookings
 * deep-dive, non-joiner package, AI insights at the bottom + caveats).
 *
 * If no report exists yet the GET schedules generation (2–4 min) and this page
 * polls status until it lands. "Regenerate" re-runs the whole pipeline.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiWebinarReport,
  WebinarReportFunnelCell,
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

/* ── small building blocks ───────────────────────────────────────────────── */

function SectionHeading({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mt-8 mb-3">
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

/* ── main page ───────────────────────────────────────────────────────────── */

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

export function WebinarReportPage({ webinarId }: { webinarId: string }) {
  const [report, setReport] = useState<ApiWebinarReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

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
    <div className="max-w-5xl mx-auto px-4 py-6">
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

      {payload && (
        <>
          {/* 1 — scorecard */}
          <SectionHeading
            title="Scorecard — vs the average webinar"
            subtitle="Baselines: every prior webinar (all) and the 4 weeks before this one — not just the previous webinar."
          />
          <Card>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    <th className={TH}>Metric</th>
                    <th className={TH}>This webinar</th>
                    <th className={TH}>All avg ({payload.scorecard.baselineAll?.webinarCount ?? 0})</th>
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
            return <FunnelCard key={dim} title={title} cells={block.cells} />;
          })}

          {/* 3 — bookings */}
          <SectionHeading
            title="Bookings deep-dive"
            subtitle="Unique booked contacts from the booking-attribution layer — a rebooked contact counts once."
          />
          <BookingsCard payload={payload} />

          {/* 4 — non-joiners */}
          <SectionHeading
            title="Non-joiner package"
            subtitle={`Pool = registered in any of the last ${payload.nonjoiners?.windowWebinars ?? 6} webinars, never attended live, not on this week's lists.`}
          />
          <NonjoinersCard payload={payload} />

          {/* 5 — AI insights (bottom, beta) */}
          <SectionHeading title="AI insights" />
          <div className="rounded-lg border border-amber-400/60 bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-300 text-xs px-3 py-2 mb-3">
            ⚠️ AI-generated insights (beta) — not always accurate. Verify against the numbers above
            before acting on them.
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
      )}
    </div>
  );
}

/* ── section components ──────────────────────────────────────────────────── */

function FunnelCard({ title, cells }: { title: string; cells: WebinarReportFunnelCell[] }) {
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

function BookingsCard({ payload }: { payload: NonNullable<ApiWebinarReport["payload"]> }) {
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
            <div key={String(label)} className="flex justify-between text-[13px] py-1 border-b border-zinc-100 dark:border-zinc-800">
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
            <div key={s.source} className="flex justify-between text-[13px] py-1 border-b border-zinc-100 dark:border-zinc-800">
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

function NonjoinersCard({ payload }: { payload: NonNullable<ApiWebinarReport["payload"]> }) {
  const nj = payload.nonjoiners ?? {};
  return (
    <Card>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <Stat label="Pool size" value={fmtInt(nj.poolSize)} />
        <Stat label="Re-registered" value={fmtInt(nj.regs)} sub={`${fmtPct(nj.regRate)} of pool`} />
        <Stat
          label="NJ attendance"
          value={fmtPct(nj.attendRateOfRegs)}
          sub={`${fmtInt(nj.attended)} attended of ${fmtInt(nj.regs)} regs`}
        />
        <Stat
          label="Net-new attendance"
          value={fmtPct(nj.netNewAttendRateOfRegs)}
          sub={`vs ${fmtPct(nj.netNewRegRateOfInvited)} reg rate of invited`}
        />
      </div>
    </Card>
  );
}
