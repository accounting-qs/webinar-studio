"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchCalendarDayOfWeek,
  type ApiAccountHealthWebinar,
  type ApiDayOfWeekCell,
  type CalendarDayOfWeekResponse,
} from "@/lib/api";

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
// Postgres EXTRACT(DOW) returns 0=Sun..6=Sat. Map to Mon-first display columns.
const DOW_TO_COL: Record<number, number> = { 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0: 6 };

type Bucket = { sent: number; yes: number; maybe: number };

function emptyBucket(): Bucket {
  return { sent: 0, yes: 0, maybe: 0 };
}

function emptyWeek(): Bucket[] {
  return Array.from({ length: 7 }, emptyBucket);
}

function addInto(target: Bucket, c: { sent: number; yes: number; maybe: number }): void {
  target.sent += c.sent;
  target.yes += c.yes;
  target.maybe += c.maybe;
}

function sumWeek(week: Bucket[]): Bucket {
  const t = emptyBucket();
  for (const b of week) addInto(t, b);
  return t;
}

function ymCount(b: Bucket): number {
  return b.yes + b.maybe;
}

function rate(b: Bucket): number {
  return b.sent > 0 ? ymCount(b) / b.sent : -1;
}

function fmtInt(n: number): string {
  return n.toLocaleString();
}

function fmtPct(b: Bucket): string {
  if (b.sent <= 0) return "—";
  return `${((ymCount(b) / b.sent) * 100).toFixed(1)}%`;
}

function shade(r: number): string {
  if (r < 0) return "";
  if (r >= 0.5) return "bg-emerald-500/30 dark:bg-emerald-500/30";
  if (r >= 0.35) return "bg-emerald-500/20 dark:bg-emerald-500/20";
  if (r >= 0.2) return "bg-emerald-500/10 dark:bg-emerald-500/10";
  return "";
}

function senderNameFor(
  data: CalendarDayOfWeekResponse,
  account: string,
  webinarFilter: string,
): string | null {
  if (webinarFilter !== "all") {
    const sid = data.sender_map[webinarFilter]?.[account];
    return sid ? data.sender_names[sid] ?? null : null;
  }
  for (const wid of Object.keys(data.sender_map)) {
    const sid = data.sender_map[wid]?.[account];
    if (sid) return data.sender_names[sid] ?? null;
  }
  return null;
}

export function DayOfWeekTab() {
  const [data, setData] = useState<CalendarDayOfWeekResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [webinarFilter, setWebinarFilter] = useState<string>("all");
  const [senderFilter, setSenderFilter] = useState<string>("all");

  const reload = useCallback(async () => {
    try {
      const d = await fetchCalendarDayOfWeek();
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

  const filteredCells = useMemo<ApiDayOfWeekCell[]>(() => {
    if (!data) return [];
    return data.cells.filter((c) => {
      if (webinarFilter !== "all" && c.webinar_id !== webinarFilter) return false;
      if (senderFilter !== "all") {
        const sid = data.sender_map[c.webinar_id]?.[c.calendar_account];
        if (sid !== senderFilter) return false;
      }
      return true;
    });
  }, [data, webinarFilter, senderFilter]);

  const overall = useMemo<Bucket[]>(() => {
    const week = emptyWeek();
    for (const c of filteredCells) {
      const col = DOW_TO_COL[c.dow];
      if (col === undefined) continue;
      addInto(week[col], c);
    }
    return week;
  }, [filteredCells]);

  const byWebinar = useMemo(() => {
    if (!data) return [];
    const map: Record<string, Bucket[]> = {};
    for (const c of filteredCells) {
      const col = DOW_TO_COL[c.dow];
      if (col === undefined) continue;
      if (!map[c.webinar_id]) map[c.webinar_id] = emptyWeek();
      addInto(map[c.webinar_id][col], c);
    }
    return data.webinars
      .filter((w) => map[w.id])
      .map((w) => ({ webinar: w, days: map[w.id] }));
  }, [data, filteredCells]);

  const byAccount = useMemo(() => {
    if (!data) return [];
    const map: Record<string, Bucket[]> = {};
    for (const c of filteredCells) {
      const col = DOW_TO_COL[c.dow];
      if (col === undefined) continue;
      if (!map[c.calendar_account]) map[c.calendar_account] = emptyWeek();
      addInto(map[c.calendar_account][col], c);
    }
    return Object.keys(map)
      .map((acc) => ({ account: acc, days: map[acc], total: sumWeek(map[acc]) }))
      .sort((a, b) => b.total.sent - a.total.sent);
  }, [data, filteredCells]);

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

  const noUploads = data.webinars.every((w) => !w.has_upload);

  return (
    <div className="flex flex-col h-full">
      <div className="flex-none px-6 pt-5 pb-3">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              Send Day
            </h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              Yes/Maybe response rate by the day of week the calendar invite was sent. Sourced from Added-to-Calendar CSV uploads.
            </p>
          </div>
          <button
            onClick={reload}
            className="px-3 py-1.5 text-xs rounded-lg bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-200"
          >
            Refresh
          </button>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <label className="text-zinc-500">Webinar</label>
          <select
            value={webinarFilter}
            onChange={(e) => setWebinarFilter(e.target.value)}
            className="bg-zinc-50 dark:bg-zinc-950 border border-zinc-300 dark:border-zinc-700 rounded-md px-2 py-1 text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
          >
            <option value="all">All webinars</option>
            {data.webinars
              .filter((w) => w.has_upload)
              .map((w) => (
                <option key={w.id} value={w.id}>
                  {w.label}
                </option>
              ))}
          </select>

          <label className="text-zinc-500 ml-3">Sender</label>
          <select
            value={senderFilter}
            onChange={(e) => setSenderFilter(e.target.value)}
            className="bg-zinc-50 dark:bg-zinc-950 border border-zinc-300 dark:border-zinc-700 rounded-md px-2 py-1 text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
          >
            <option value="all">All senders</option>
            {data.senders.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {noUploads ? (
        <div className="mx-6 mt-6 mb-6 text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
          No calendar uploads yet — head to the Calendar Uploads tab to add one.
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-auto px-6 pb-6 space-y-6">
          <OverviewCard buckets={overall} />
          <ByWebinarTable rows={byWebinar} />
          <ByAccountTable
            rows={byAccount}
            senderForAccount={(acc) => senderNameFor(data, acc, webinarFilter)}
          />
        </div>
      )}
    </div>
  );
}

function OverviewCard({ buckets }: { buckets: Bucket[] }) {
  const total = sumWeek(buckets);
  const maxYm = buckets.reduce((m, b) => Math.max(m, ymCount(b)), 0);
  let bestIdx = -1;
  let bestRate = -1;
  buckets.forEach((b, i) => {
    if (b.sent > 0 && rate(b) > bestRate) {
      bestRate = rate(b);
      bestIdx = i;
    }
  });

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          Overview by Day of Week
        </h3>
        <div className="text-[11px] text-zinc-500">
          {fmtInt(total.sent)} invites · {fmtInt(ymCount(total))} yes+maybe · {fmtPct(total)} overall
        </div>
      </div>
      <div className="grid grid-cols-7 gap-2">
        {buckets.map((b, i) => {
          const isBest = i === bestIdx;
          const r = rate(b);
          return (
            <div
              key={i}
              className={`rounded-lg border px-3 py-3 ${
                isBest
                  ? "border-violet-500 bg-violet-500/5"
                  : "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950"
              }`}
            >
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">
                {DAY_LABELS[i]}
              </div>
              <div
                className={`text-lg font-bold font-mono mt-1 ${
                  isBest
                    ? "text-violet-500"
                    : "text-zinc-900 dark:text-zinc-100"
                }`}
              >
                {b.sent > 0 ? `${(r * 100).toFixed(1)}%` : "—"}
              </div>
              <div className="text-[10px] text-zinc-500 mt-0.5">
                {fmtInt(ymCount(b))} / {fmtInt(b.sent)}
              </div>
              <div className="mt-2 h-1 bg-zinc-200 dark:bg-zinc-800 rounded overflow-hidden">
                <div
                  className={`h-full ${isBest ? "bg-violet-500" : "bg-emerald-500"}`}
                  style={{
                    width: maxYm > 0 ? `${(ymCount(b) / maxYm) * 100}%` : "0%",
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function DayCell({ b }: { b: Bucket }) {
  if (b.sent === 0) {
    return (
      <td className="px-3 py-2 text-right text-zinc-500 font-mono">—</td>
    );
  }
  return (
    <td className={`px-3 py-2 text-right font-mono ${shade(rate(b))}`}>
      <div className="text-zinc-900 dark:text-zinc-100">{fmtPct(b)}</div>
      <div className="text-[10px] text-zinc-500">
        {fmtInt(ymCount(b))} / {fmtInt(b.sent)}
      </div>
    </td>
  );
}

function TotalCell({ b }: { b: Bucket }) {
  if (b.sent === 0) {
    return (
      <td className="px-3 py-2 text-right text-zinc-500 font-mono border-l border-zinc-200 dark:border-zinc-800">
        —
      </td>
    );
  }
  return (
    <td className="px-3 py-2 text-right font-mono border-l border-zinc-200 dark:border-zinc-800">
      <div className="text-zinc-900 dark:text-zinc-100 font-semibold">{fmtPct(b)}</div>
      <div className="text-[10px] text-zinc-500">
        {fmtInt(ymCount(b))} / {fmtInt(b.sent)}
      </div>
    </td>
  );
}

function ByWebinarTable({
  rows,
}: {
  rows: { webinar: ApiAccountHealthWebinar; days: Bucket[] }[];
}) {
  if (rows.length === 0) {
    return (
      <section>
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 mb-2">
          By Webinar
        </h3>
        <div className="text-xs text-zinc-500 py-6 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
          No invite data matches the current filters.
        </div>
      </section>
    );
  }
  return (
    <section>
      <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 mb-2">
        By Webinar
      </h3>
      <div className="border border-zinc-200 dark:border-zinc-800 rounded-lg overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-zinc-50 dark:bg-zinc-900 text-[10px] uppercase tracking-wider text-zinc-500">
            <tr>
              <th className="text-left px-3 py-2 font-semibold min-w-[220px]">Webinar</th>
              {DAY_LABELS.map((d) => (
                <th
                  key={d}
                  className="text-right px-3 py-2 font-semibold min-w-[100px]"
                >
                  {d}
                </th>
              ))}
              <th className="text-right px-3 py-2 font-semibold border-l border-zinc-200 dark:border-zinc-800 min-w-[100px]">
                Total
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
            {rows.map((r) => {
              const total = sumWeek(r.days);
              return (
                <tr
                  key={r.webinar.id}
                  className="bg-white dark:bg-zinc-950 hover:bg-zinc-50 dark:hover:bg-zinc-900/60"
                >
                  <td className="px-3 py-2 text-zinc-800 dark:text-zinc-200 font-mono">
                    {r.webinar.label}
                  </td>
                  {r.days.map((b, i) => (
                    <DayCell key={i} b={b} />
                  ))}
                  <TotalCell b={total} />
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ByAccountTable({
  rows,
  senderForAccount,
}: {
  rows: { account: string; days: Bucket[]; total: Bucket }[];
  senderForAccount: (acc: string) => string | null;
}) {
  if (rows.length === 0) {
    return (
      <section>
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 mb-2">
          By Calendar Account
        </h3>
        <div className="text-xs text-zinc-500 py-6 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
          No invite data matches the current filters.
        </div>
      </section>
    );
  }
  return (
    <section>
      <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 mb-2">
        By Calendar Account
      </h3>
      <div className="border border-zinc-200 dark:border-zinc-800 rounded-lg overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-zinc-50 dark:bg-zinc-900 text-[10px] uppercase tracking-wider text-zinc-500">
            <tr>
              <th className="text-left px-3 py-2 font-semibold min-w-[240px]">
                Calendar account
              </th>
              <th className="text-left px-3 py-2 font-semibold min-w-[140px]">
                Sender
              </th>
              {DAY_LABELS.map((d) => (
                <th
                  key={d}
                  className="text-right px-3 py-2 font-semibold min-w-[100px]"
                >
                  {d}
                </th>
              ))}
              <th className="text-right px-3 py-2 font-semibold border-l border-zinc-200 dark:border-zinc-800 min-w-[100px]">
                Total
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
            {rows.map((r) => {
              const sender = senderForAccount(r.account);
              return (
                <tr
                  key={r.account}
                  className="bg-white dark:bg-zinc-950 hover:bg-zinc-50 dark:hover:bg-zinc-900/60"
                >
                  <td
                    className="px-3 py-2 font-mono text-zinc-800 dark:text-zinc-200 truncate max-w-[320px]"
                    title={r.account}
                  >
                    {r.account}
                  </td>
                  <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">
                    {sender ?? <span className="text-zinc-500">—</span>}
                  </td>
                  {r.days.map((b, i) => (
                    <DayCell key={i} b={b} />
                  ))}
                  <TotalCell b={r.total} />
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
