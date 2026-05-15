"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchCalendarAccountHealth,
  type ApiAccountHealthCell,
  type ApiAccountHealthRow,
  type ApiAccountHealthWebinar,
  type CalendarAccountHealthResponse,
} from "@/lib/api";

/* ─── Sticky identity columns (Calendar_account / Workspace acc / Notes) ─ */
const W_ACC = "w-[260px] min-w-[260px] max-w-[260px]";
const W_WS = "w-[140px] min-w-[140px] max-w-[140px]";
const W_NOTE = "w-[140px] min-w-[140px] max-w-[140px]";

const L_ACC = "sticky left-0";
const L_WS = "sticky left-[260px]";
const L_NOTE = "sticky left-[400px]";

const Z_HEADER = "z-30";
const Z_ROW = "z-20";

const BG_HEADER = "bg-zinc-50 dark:bg-zinc-900";
const BG_TOTAL = "bg-emerald-500/5 dark:bg-emerald-500/10";
const BG_LIST = "bg-white dark:bg-zinc-950";

const W_METRIC = "min-w-[80px] px-2 py-1.5 text-right tabular-nums";

function fmtInt(n: number): string {
  return n.toLocaleString();
}

function fmtPct(num: number, denom: number): string {
  if (denom <= 0) return "—";
  return `${((num / denom) * 100).toFixed(2)}%`;
}

function shade(value: number, max: number): string {
  if (value <= 0 || max <= 0) return "";
  const r = value / max;
  if (r >= 0.66) return "bg-emerald-500/30 dark:bg-emerald-500/30";
  if (r >= 0.33) return "bg-emerald-500/20 dark:bg-emerald-500/20";
  if (r > 0) return "bg-emerald-500/10 dark:bg-emerald-500/10";
  return "";
}

export function AccountHealthTab() {
  const [data, setData] = useState<CalendarAccountHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      const d = await fetchCalendarAccountHealth();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

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

  return (
    <div className="flex flex-col h-full">
      <div className="flex-none px-6 pt-5 pb-3">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              Account Health
            </h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              Per-Calendar_account invite stats by webinar, sourced from Added-to-Calendar CSV
              uploads.
            </p>
          </div>
          <button
            onClick={reload}
            className="px-3 py-1.5 text-xs rounded-lg bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-200"
          >
            Refresh
          </button>
        </div>
      </div>

      {data.webinars.length === 0 ? (
        <div className="mx-6 mb-6 text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
          No webinars yet.
        </div>
      ) : data.accounts.length === 0 ? (
        <div className="mx-6 mb-6 text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
          No calendar uploads yet — head to the Calendar Uploads tab to add one.
        </div>
      ) : (
        <HealthTable data={data} />
      )}
    </div>
  );
}

function HealthTable({ data }: { data: CalendarAccountHealthResponse }) {
  // Per-webinar maxes used for cell shading (computed from the per-account
  // values, never the TOTAL row itself).
  const maxByWebinar = useMemo(() => {
    const m: Record<string, { sent: number; yes: number; maybe: number; ym: number }> = {};
    for (const w of data.webinars) {
      m[w.id] = { sent: 0, yes: 0, maybe: 0, ym: 0 };
    }
    for (const acc of data.accounts) {
      for (const w of data.webinars) {
        const c = acc.per_webinar[w.id];
        if (!c) continue;
        const ym = c.yes + c.maybe;
        const cur = m[w.id];
        if (c.total_sent > cur.sent) cur.sent = c.total_sent;
        if (c.yes > cur.yes) cur.yes = c.yes;
        if (c.maybe > cur.maybe) cur.maybe = c.maybe;
        if (ym > cur.ym) cur.ym = ym;
      }
    }
    return m;
  }, [data]);

  return (
    <div className="flex-1 min-h-0 overflow-auto px-6 pb-6">
      <div className="inline-block min-w-full border border-zinc-200 dark:border-zinc-800 rounded-lg overflow-hidden">
        <table className="text-xs border-collapse">
          <thead>
            {/* Row 1: webinar labels (one group of 5 metric cells per webinar) */}
            <tr>
              <th className={`${L_ACC} ${Z_HEADER} ${BG_HEADER} ${W_ACC} px-3 py-2 text-left font-semibold text-zinc-700 dark:text-zinc-300 border-b border-zinc-200 dark:border-zinc-800`}>
                &nbsp;
              </th>
              <th className={`${L_WS} ${Z_HEADER} ${BG_HEADER} ${W_WS} px-3 py-2 text-left font-semibold text-zinc-700 dark:text-zinc-300 border-b border-zinc-200 dark:border-zinc-800`}>
                &nbsp;
              </th>
              <th className={`${L_NOTE} ${Z_HEADER} ${BG_HEADER} ${W_NOTE} px-3 py-2 text-left font-semibold text-zinc-700 dark:text-zinc-300 border-b border-r border-zinc-200 dark:border-zinc-800`}>
                &nbsp;
              </th>
              {data.webinars.map((w) => (
                <th
                  key={w.id}
                  colSpan={5}
                  className={`${BG_HEADER} px-2 py-2 text-center font-semibold border-b border-r border-zinc-200 dark:border-zinc-800`}
                  title={w.has_upload ? w.label : `${w.label} — no calendar upload yet`}
                >
                  <div className="flex items-center justify-center gap-1.5">
                    <span className="text-zinc-800 dark:text-zinc-200">{w.label}</span>
                    {!w.has_upload && (
                      <span className="px-1 py-px text-[9px] font-semibold rounded bg-amber-500/15 text-amber-500 border border-amber-500/30 uppercase tracking-wider">
                        no upload
                      </span>
                    )}
                  </div>
                </th>
              ))}
            </tr>

            {/* Row 2: TOTAL aggregates across all accounts */}
            <tr>
              <th
                colSpan={3}
                className={`${L_ACC} ${Z_HEADER} ${BG_TOTAL} px-3 py-2 text-left font-semibold text-zinc-800 dark:text-zinc-100 border-b border-r border-zinc-200 dark:border-zinc-800`}
                style={{ width: 540 }}
              >
                TOTAL
              </th>
              {data.webinars.map((w) => {
                const t = data.totals[w.id];
                const sent = t?.total_sent ?? 0;
                const yes = t?.yes ?? 0;
                const maybe = t?.maybe ?? 0;
                const ym = yes + maybe;
                return (
                  <TotalGroup
                    key={w.id}
                    sent={sent}
                    yes={yes}
                    maybe={maybe}
                    ym={ym}
                  />
                );
              })}
            </tr>

            {/* Row 3: column headers */}
            <tr>
              <th className={`${L_ACC} ${Z_HEADER} ${BG_HEADER} ${W_ACC} px-3 py-2 text-left font-semibold text-zinc-700 dark:text-zinc-300 border-b border-zinc-200 dark:border-zinc-800`}>
                Calendar_account
              </th>
              <th className={`${L_WS} ${Z_HEADER} ${BG_HEADER} ${W_WS} px-3 py-2 text-left font-semibold text-zinc-500 dark:text-zinc-500 border-b border-zinc-200 dark:border-zinc-800`}>
                Workspace acc
              </th>
              <th className={`${L_NOTE} ${Z_HEADER} ${BG_HEADER} ${W_NOTE} px-3 py-2 text-left font-semibold text-zinc-500 dark:text-zinc-500 border-b border-r border-zinc-200 dark:border-zinc-800`}>
                Notes
              </th>
              {data.webinars.map((w) => (
                <MetricHeaders key={w.id} />
              ))}
            </tr>
          </thead>

          <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
            {data.accounts.map((acc) => (
              <AccountRow
                key={acc.calendar_account}
                row={acc}
                webinars={data.webinars}
                maxByWebinar={maxByWebinar}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function MetricHeaders() {
  const cell =
    "px-2 py-1.5 text-right font-semibold text-zinc-500 dark:text-zinc-500 border-b border-zinc-200 dark:border-zinc-800";
  const last =
    "px-2 py-1.5 text-right font-semibold text-zinc-500 dark:text-zinc-500 border-b border-r border-zinc-200 dark:border-zinc-800";
  return (
    <>
      <th className={cell}>Total Sent</th>
      <th className={cell}>Yes</th>
      <th className={cell}>Maybe</th>
      <th className={cell}>Yes+Maybe</th>
      <th className={last}>Yes+Maybe %</th>
    </>
  );
}

function TotalGroup({
  sent,
  yes,
  maybe,
  ym,
}: {
  sent: number;
  yes: number;
  maybe: number;
  ym: number;
}) {
  const c = `${W_METRIC} ${BG_TOTAL} border-b border-zinc-200 dark:border-zinc-800 font-semibold tabular-nums`;
  const last = `${W_METRIC} ${BG_TOTAL} border-b border-r border-zinc-200 dark:border-zinc-800 font-semibold tabular-nums`;
  return (
    <>
      <th className={c}>{fmtInt(sent)}</th>
      <th className={c}>{fmtInt(yes)}</th>
      <th className={c}>{fmtInt(maybe)}</th>
      <th className={c}>{fmtInt(ym)}</th>
      <th className={last}>{fmtPct(ym, sent)}</th>
    </>
  );
}

function AccountRow({
  row,
  webinars,
  maxByWebinar,
}: {
  row: ApiAccountHealthRow;
  webinars: ApiAccountHealthWebinar[];
  maxByWebinar: Record<string, { sent: number; yes: number; maybe: number; ym: number }>;
}) {
  return (
    <tr className="hover:bg-zinc-50 dark:hover:bg-zinc-900/60">
      <td className={`${L_ACC} ${Z_ROW} ${BG_LIST} ${W_ACC} px-3 py-1.5 font-mono text-emerald-500 dark:text-emerald-400 truncate`} title={row.calendar_account}>
        {row.calendar_account}
      </td>
      <td className={`${L_WS} ${Z_ROW} ${BG_LIST} ${W_WS} px-3 py-1.5 text-zinc-500 dark:text-zinc-500`}>
        &nbsp;
      </td>
      <td className={`${L_NOTE} ${Z_ROW} ${BG_LIST} ${W_NOTE} px-3 py-1.5 text-zinc-500 dark:text-zinc-500 border-r border-zinc-200 dark:border-zinc-800`}>
        &nbsp;
      </td>
      {webinars.map((w) => (
        <MetricGroup
          key={w.id}
          cell={row.per_webinar[w.id]}
          max={maxByWebinar[w.id]}
        />
      ))}
    </tr>
  );
}

function MetricGroup({
  cell,
  max,
}: {
  cell: ApiAccountHealthCell | undefined;
  max: { sent: number; yes: number; maybe: number; ym: number };
}) {
  const sent = cell?.total_sent ?? 0;
  const yes = cell?.yes ?? 0;
  const maybe = cell?.maybe ?? 0;
  const ym = yes + maybe;
  const pct = sent > 0 ? ym / sent : null;

  const base = `${W_METRIC} text-zinc-700 dark:text-zinc-300`;
  const lastBase = `${base} border-r border-zinc-200 dark:border-zinc-800`;

  return (
    <>
      <td className={`${base}`}>{sent > 0 ? fmtInt(sent) : <span className="text-zinc-500">0</span>}</td>
      <td className={`${base} ${shade(yes, max.yes)}`}>
        {yes > 0 ? fmtInt(yes) : <span className="text-zinc-500">0</span>}
      </td>
      <td className={`${base} ${shade(maybe, max.maybe)}`}>
        {maybe > 0 ? fmtInt(maybe) : <span className="text-zinc-500">0</span>}
      </td>
      <td className={`${base} ${shade(ym, max.ym)}`}>
        {ym > 0 ? fmtInt(ym) : <span className="text-zinc-500">0</span>}
      </td>
      <td className={`${lastBase}`}>
        {pct === null ? (
          <span className="text-zinc-500">—</span>
        ) : (
          <span className={pct > 0 ? "text-emerald-500" : "text-zinc-500"}>
            {fmtPct(ym, sent)}
          </span>
        )}
      </td>
    </>
  );
}
