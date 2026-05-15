"use client";

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchCalendarAccountHealth,
  setCalendarAccountSendersBulk,
  type ApiAccountHealthCell,
  type ApiAccountHealthRow,
  type ApiAccountHealthSender,
  type ApiAccountHealthWebinar,
  type CalendarAccountHealthResponse,
} from "@/lib/api";

/* ─── Sticky identity columns
 * Order: Calendar_account | Workspace acc | Sender | Notes,
 *        then the leftmost (newest) webinar's Total Sent column is frozen.
 *   ACC      260px @ left-0
 *   WORKSPC  140px @ left-260
 *   SENDER   160px @ left-400
 *   NOTES    140px @ left-560
 *   FIRST_TOT 80px @ left-700                                              */
const W_ACC = "w-[260px] min-w-[260px] max-w-[260px]";
const W_WS = "w-[140px] min-w-[140px] max-w-[140px]";
const W_SENDER = "w-[160px] min-w-[160px] max-w-[160px]";
const W_NOTE = "w-[140px] min-w-[140px] max-w-[140px]";
const W_FIRST_TOT = "w-[80px] min-w-[80px] max-w-[80px]";

const L_ACC = "sticky left-0";
const L_WS = "sticky left-[260px]";
const L_SENDER = "sticky left-[400px]";
const L_NOTE = "sticky left-[560px]";
const L_FIRST_TOT = "sticky left-[700px]";

const IDENTITY_PANEL_PX = 700;
const IDENTITY_COLS = 4; // account, ws, sender, notes

const Z_HEADER = "z-30";
const Z_ROW = "z-20";

const BG_HEADER = "bg-zinc-50 dark:bg-zinc-900";
const BG_TOTAL = "bg-emerald-500/5 dark:bg-emerald-500/10";
const BG_LIST = "bg-white dark:bg-zinc-950";

const W_METRIC = "min-w-[80px] px-2 py-1.5 text-right tabular-nums";
const FREEZE_EDGE = "border-r-2 border-zinc-300 dark:border-zinc-700";

type MetricKey = "sent" | "yes" | "maybe" | "ym" | "pct";
type SortKey = "account" | "sender" | `metric:${string}:${MetricKey}`;
type SortDir = "asc" | "desc";

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

function metricValue(cell: ApiAccountHealthCell | undefined, metric: MetricKey): number {
  const sent = cell?.total_sent ?? 0;
  const yes = cell?.yes ?? 0;
  const maybe = cell?.maybe ?? 0;
  const ym = yes + maybe;
  switch (metric) {
    case "sent": return sent;
    case "yes": return yes;
    case "maybe": return maybe;
    case "ym": return ym;
    case "pct": return sent > 0 ? ym / sent : -1;
  }
}

type SubTab = { kind: "total" } | { kind: "sender"; senderId: string };

export function AccountHealthTab() {
  const [data, setData] = useState<CalendarAccountHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [subTab, setSubTab] = useState<SubTab>({ kind: "total" });
  const [modalOpen, setModalOpen] = useState(false);

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
              Per-Calendar_account invite stats by webinar, sourced from Added-to-Calendar CSV uploads.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setModalOpen(true)}
              disabled={data.webinars.length === 0 || data.senders.length === 0}
              className="px-3 py-1.5 text-xs rounded-lg bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Set Senders
            </button>
            <button
              onClick={reload}
              className="px-3 py-1.5 text-xs rounded-lg bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-200"
            >
              Refresh
            </button>
          </div>
        </div>
      </div>

      {/* Sub-tab bar: TOTAL + one tab per sender */}
      <div className="flex-none border-b border-zinc-200 dark:border-zinc-800 px-6">
        <div className="flex gap-1 overflow-x-auto">
          <SubTabButton
            active={subTab.kind === "total"}
            onClick={() => setSubTab({ kind: "total" })}
          >
            TOTAL
          </SubTabButton>
          {data.senders.map((s) => (
            <SubTabButton
              key={s.id}
              active={subTab.kind === "sender" && subTab.senderId === s.id}
              onClick={() => setSubTab({ kind: "sender", senderId: s.id })}
              color={s.color}
            >
              {s.name}
            </SubTabButton>
          ))}
        </div>
      </div>

      {data.webinars.length === 0 ? (
        <div className="mx-6 mt-6 mb-6 text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
          No webinars yet.
        </div>
      ) : data.accounts.length === 0 ? (
        <div className="mx-6 mt-6 mb-6 text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
          No calendar uploads yet — head to the Calendar Uploads tab to add one.
        </div>
      ) : subTab.kind === "total" ? (
        <TotalView data={data} />
      ) : (
        <SenderView
          data={data}
          sender={
            data.senders.find((s) => s.id === subTab.senderId) ?? null
          }
        />
      )}

      {modalOpen && (
        <SetSendersModal
          webinars={data.webinars}
          senders={data.senders}
          onClose={() => setModalOpen(false)}
          onSaved={() => {
            setModalOpen(false);
            reload();
          }}
        />
      )}
    </div>
  );
}

function SubTabButton({
  active,
  onClick,
  children,
  color,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  color?: string | null;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-2 text-xs font-semibold border-b-2 -mb-px transition-colors whitespace-nowrap inline-flex items-center gap-1.5 ${
        active
          ? "border-violet-500 text-violet-500"
          : "border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
      }`}
    >
      {color && (
        <span
          className="inline-block w-2 h-2 rounded-full"
          style={{ backgroundColor: color }}
        />
      )}
      {children}
    </button>
  );
}

/* ── TOTAL sub-tab: single-webinar view (default = latest) ────────────── */

function TotalView({ data }: { data: CalendarAccountHealthResponse }) {
  const [webinarId, setWebinarId] = useState<string>(data.webinars[0]?.id ?? "");
  const webinar = data.webinars.find((w) => w.id === webinarId) ?? null;

  const filtered = useMemo(() => {
    if (!webinar) {
      return { webinars: [], accounts: [], totals: {} };
    }
    const accounts = data.accounts.filter(
      (a) => a.per_webinar[webinar.id] !== undefined,
    );
    return {
      webinars: [webinar],
      accounts,
      totals: { [webinar.id]: data.totals[webinar.id] ?? { total_sent: 0, yes: 0, maybe: 0 } },
    };
  }, [data, webinar]);

  const senderForAccount = useCallback(
    (account: string): string | null => {
      if (!webinar) return null;
      const sid = data.sender_map[webinar.id]?.[account];
      return sid ? data.sender_names[sid] ?? null : null;
    },
    [data, webinar],
  );

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <div className="flex-none px-6 py-3 flex items-center gap-3 border-b border-zinc-200 dark:border-zinc-800">
        <label className="text-xs text-zinc-500">Webinar</label>
        <select
          value={webinarId}
          onChange={(e) => setWebinarId(e.target.value)}
          className="bg-zinc-50 dark:bg-zinc-950 border border-zinc-300 dark:border-zinc-700 rounded-md px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
        >
          {data.webinars.map((w) => (
            <option key={w.id} value={w.id}>
              {w.label}
              {w.has_upload ? "" : " — no upload"}
            </option>
          ))}
        </select>
      </div>
      <HealthTable
        webinars={filtered.webinars}
        accounts={filtered.accounts}
        totals={filtered.totals}
        senderForAccount={senderForAccount}
      />
    </div>
  );
}

/* ── Per-Sender sub-tab: matrix filtered to that sender's accounts ────── */

function SenderView({
  data,
  sender,
}: {
  data: CalendarAccountHealthResponse;
  sender: ApiAccountHealthSender | null;
}) {
  const filtered = useMemo(() => {
    if (!sender) {
      return { accounts: [], totals: {} };
    }
    // Account belongs in this tab if it's mapped to `sender` in ANY webinar.
    const mappedAccounts = new Set<string>();
    for (const wid of Object.keys(data.sender_map)) {
      const m = data.sender_map[wid];
      for (const [acc, sid] of Object.entries(m)) {
        if (sid === sender.id) mappedAccounts.add(acc);
      }
    }
    const accounts = data.accounts.filter((a) => mappedAccounts.has(a.calendar_account));

    // Recompute per-webinar totals across just these accounts.
    const totals: Record<string, ApiAccountHealthCell> = {};
    for (const w of data.webinars) {
      let sent = 0, yes = 0, maybe = 0;
      for (const a of accounts) {
        const c = a.per_webinar[w.id];
        if (!c) continue;
        sent += c.total_sent;
        yes += c.yes;
        maybe += c.maybe;
      }
      totals[w.id] = { total_sent: sent, yes, maybe };
    }
    return { accounts, totals };
  }, [data, sender]);

  const senderForAccount = useCallback(() => sender?.name ?? null, [sender]);

  if (!sender) return null;
  if (filtered.accounts.length === 0) {
    return (
      <div className="mx-6 mt-6 mb-6 text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
        No accounts mapped to <span className="font-semibold">{sender.name}</span> yet. Use{" "}
        <span className="font-semibold">Set Senders</span> to assign accounts to this sender.
      </div>
    );
  }
  return (
    <HealthTable
      webinars={data.webinars}
      accounts={filtered.accounts}
      totals={filtered.totals}
      senderForAccount={senderForAccount}
    />
  );
}

/* ── Generic table — reused by both sub-tabs ──────────────────────────── */

function HealthTable({
  webinars,
  accounts,
  totals,
  senderForAccount,
}: {
  webinars: ApiAccountHealthWebinar[];
  accounts: ApiAccountHealthRow[];
  totals: Record<string, ApiAccountHealthCell>;
  senderForAccount: (account: string) => string | null;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("account");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "account" || key === "sender" ? "asc" : "desc");
    }
  };

  const maxByWebinar = useMemo(() => {
    const m: Record<string, { sent: number; yes: number; maybe: number; ym: number }> = {};
    for (const w of webinars) {
      m[w.id] = { sent: 0, yes: 0, maybe: 0, ym: 0 };
    }
    for (const acc of accounts) {
      for (const w of webinars) {
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
  }, [webinars, accounts]);

  const sortedAccounts = useMemo(() => {
    const arr = [...accounts];
    arr.sort((a, b) => {
      let cmp = 0;
      if (sortKey === "account") {
        cmp = a.calendar_account.localeCompare(b.calendar_account);
      } else if (sortKey === "sender") {
        const an = senderForAccount(a.calendar_account) ?? "";
        const bn = senderForAccount(b.calendar_account) ?? "";
        cmp = an.localeCompare(bn);
        if (cmp === 0) cmp = a.calendar_account.localeCompare(b.calendar_account);
      } else {
        const parts = sortKey.split(":");
        const webinarId = parts[1];
        const metric = parts[2] as MetricKey;
        cmp = metricValue(a.per_webinar[webinarId], metric) -
              metricValue(b.per_webinar[webinarId], metric);
        if (cmp === 0) cmp = a.calendar_account.localeCompare(b.calendar_account);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [accounts, sortKey, sortDir, senderForAccount]);

  return (
    <div className="flex-1 min-h-0 overflow-auto px-6 pb-6 pt-3">
      <div className="inline-block min-w-full border border-zinc-200 dark:border-zinc-800 rounded-lg overflow-hidden">
        <table className="text-xs border-collapse">
          <thead>
            {/* Row 1: webinar labels (5 cells per webinar; label sits on Total Sent cell) */}
            <tr>
              <th className={`${L_ACC} ${Z_HEADER} ${BG_HEADER} ${W_ACC} px-3 py-2 text-left font-semibold text-zinc-700 dark:text-zinc-300 border-b border-zinc-200 dark:border-zinc-800`}>
                &nbsp;
              </th>
              <th className={`${L_WS} ${Z_HEADER} ${BG_HEADER} ${W_WS} px-3 py-2 border-b border-zinc-200 dark:border-zinc-800`}>
                &nbsp;
              </th>
              <th className={`${L_SENDER} ${Z_HEADER} ${BG_HEADER} ${W_SENDER} px-3 py-2 border-b border-zinc-200 dark:border-zinc-800`}>
                &nbsp;
              </th>
              <th className={`${L_NOTE} ${Z_HEADER} ${BG_HEADER} ${W_NOTE} px-3 py-2 border-b border-r border-zinc-200 dark:border-zinc-800`}>
                &nbsp;
              </th>
              {webinars.map((w, idx) => {
                const isFirst = idx === 0;
                const labelCellCls = isFirst
                  ? `${L_FIRST_TOT} ${Z_HEADER} ${BG_HEADER} ${W_FIRST_TOT} px-2 py-2 text-left font-semibold border-b border-zinc-200 dark:border-zinc-800 ${FREEZE_EDGE}`
                  : `${BG_HEADER} ${W_FIRST_TOT} px-2 py-2 text-left font-semibold border-b border-zinc-200 dark:border-zinc-800`;
                const fillerCls = `${BG_HEADER} px-2 py-2 border-b border-zinc-200 dark:border-zinc-800`;
                const lastFillerCls = `${BG_HEADER} px-2 py-2 border-b border-r border-zinc-200 dark:border-zinc-800`;
                return (
                  <Fragment key={w.id}>
                    <th
                      className={labelCellCls}
                      title={w.has_upload ? w.label : `${w.label} — no calendar upload yet`}
                    >
                      <div className="flex items-center gap-1.5 whitespace-nowrap">
                        <span className="text-zinc-800 dark:text-zinc-200">{w.label}</span>
                        {!w.has_upload && (
                          <span className="px-1 py-px text-[9px] font-semibold rounded bg-amber-500/15 text-amber-500 border border-amber-500/30 uppercase tracking-wider">
                            no upload
                          </span>
                        )}
                      </div>
                    </th>
                    <th className={fillerCls}>&nbsp;</th>
                    <th className={fillerCls}>&nbsp;</th>
                    <th className={fillerCls}>&nbsp;</th>
                    <th className={lastFillerCls}>&nbsp;</th>
                  </Fragment>
                );
              })}
            </tr>

            {/* Row 2: TOTAL aggregates across rendered accounts */}
            <tr>
              <th
                colSpan={IDENTITY_COLS}
                className={`${L_ACC} ${Z_HEADER} ${BG_TOTAL} px-3 py-2 text-left font-semibold text-zinc-800 dark:text-zinc-100 border-b border-r border-zinc-200 dark:border-zinc-800`}
                style={{ width: IDENTITY_PANEL_PX }}
              >
                TOTAL
              </th>
              {webinars.map((w, idx) => {
                const t = totals[w.id];
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
                    isFirst={idx === 0}
                  />
                );
              })}
            </tr>

            {/* Row 3: column headers */}
            <tr>
              <th
                onClick={() => handleSort("account")}
                className={`${L_ACC} ${Z_HEADER} ${BG_HEADER} ${W_ACC} px-3 py-2 text-left font-semibold text-zinc-700 dark:text-zinc-300 border-b border-zinc-200 dark:border-zinc-800 cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800`}
              >
                <span className="inline-flex items-center gap-1">
                  Calendar_account
                  <SortArrow active={sortKey === "account"} dir={sortDir} />
                </span>
              </th>
              <th className={`${L_WS} ${Z_HEADER} ${BG_HEADER} ${W_WS} px-3 py-2 text-left font-semibold text-zinc-500 dark:text-zinc-500 border-b border-zinc-200 dark:border-zinc-800`}>
                Workspace acc
              </th>
              <th
                onClick={() => handleSort("sender")}
                className={`${L_SENDER} ${Z_HEADER} ${BG_HEADER} ${W_SENDER} px-3 py-2 text-left font-semibold text-zinc-700 dark:text-zinc-300 border-b border-zinc-200 dark:border-zinc-800 cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800`}
              >
                <span className="inline-flex items-center gap-1">
                  Sender
                  <SortArrow active={sortKey === "sender"} dir={sortDir} />
                </span>
              </th>
              <th className={`${L_NOTE} ${Z_HEADER} ${BG_HEADER} ${W_NOTE} px-3 py-2 text-left font-semibold text-zinc-500 dark:text-zinc-500 border-b border-r border-zinc-200 dark:border-zinc-800`}>
                Notes
              </th>
              {webinars.map((w, idx) => (
                <MetricHeaders
                  key={w.id}
                  webinarId={w.id}
                  sortKey={sortKey}
                  sortDir={sortDir}
                  onSort={handleSort}
                  isFirst={idx === 0}
                />
              ))}
            </tr>
          </thead>

          <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
            {sortedAccounts.map((acc) => (
              <AccountRow
                key={acc.calendar_account}
                row={acc}
                webinars={webinars}
                maxByWebinar={maxByWebinar}
                senderName={senderForAccount(acc.calendar_account)}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SortArrow({ active, dir }: { active: boolean; dir: SortDir }) {
  if (!active) {
    return <span className="text-zinc-400 dark:text-zinc-600 text-[10px]">↕</span>;
  }
  return (
    <span className="text-violet-500 text-[10px]">{dir === "asc" ? "↑" : "↓"}</span>
  );
}

function MetricHeaders({
  webinarId,
  sortKey,
  sortDir,
  onSort,
  isFirst,
}: {
  webinarId: string;
  sortKey: SortKey;
  sortDir: SortDir;
  onSort: (key: SortKey) => void;
  isFirst: boolean;
}) {
  const base =
    "px-2 py-1.5 text-right font-semibold text-zinc-500 dark:text-zinc-500 border-b border-zinc-200 dark:border-zinc-800 cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800";
  const last =
    "px-2 py-1.5 text-right font-semibold text-zinc-500 dark:text-zinc-500 border-b border-r border-zinc-200 dark:border-zinc-800 cursor-pointer select-none hover:bg-zinc-100 dark:hover:bg-zinc-800";
  const sentSticky = isFirst
    ? `${L_FIRST_TOT} ${Z_HEADER} ${BG_HEADER} ${W_FIRST_TOT} ${FREEZE_EDGE} ${base}`
    : base;

  const cells: { metric: MetricKey; label: string; className: string }[] = [
    { metric: "sent", label: "Total Sent", className: sentSticky },
    { metric: "yes", label: "Yes", className: base },
    { metric: "maybe", label: "Maybe", className: base },
    { metric: "ym", label: "Yes+Maybe", className: base },
    { metric: "pct", label: "Yes+Maybe %", className: last },
  ];

  return (
    <>
      {cells.map(({ metric, label, className }) => {
        const key = `metric:${webinarId}:${metric}` as SortKey;
        const active = sortKey === key;
        return (
          <th key={metric} onClick={() => onSort(key)} className={className}>
            <span className="inline-flex items-center justify-end gap-1">
              {label}
              <SortArrow active={active} dir={sortDir} />
            </span>
          </th>
        );
      })}
    </>
  );
}

function TotalGroup({
  sent,
  yes,
  maybe,
  ym,
  isFirst,
}: {
  sent: number;
  yes: number;
  maybe: number;
  ym: number;
  isFirst: boolean;
}) {
  const c = `${W_METRIC} ${BG_TOTAL} border-b border-zinc-200 dark:border-zinc-800 font-semibold tabular-nums`;
  const lastCls = `${W_METRIC} ${BG_TOTAL} border-b border-r border-zinc-200 dark:border-zinc-800 font-semibold tabular-nums`;
  const sentSticky = isFirst
    ? `${L_FIRST_TOT} ${Z_HEADER} ${BG_TOTAL} ${W_FIRST_TOT} ${FREEZE_EDGE} px-2 py-1.5 text-right border-b border-zinc-200 dark:border-zinc-800 font-semibold tabular-nums`
    : c;
  return (
    <>
      <th className={sentSticky}>{fmtInt(sent)}</th>
      <th className={c}>{fmtInt(yes)}</th>
      <th className={c}>{fmtInt(maybe)}</th>
      <th className={c}>{fmtInt(ym)}</th>
      <th className={lastCls}>{fmtPct(ym, sent)}</th>
    </>
  );
}

function AccountRow({
  row,
  webinars,
  maxByWebinar,
  senderName,
}: {
  row: ApiAccountHealthRow;
  webinars: ApiAccountHealthWebinar[];
  maxByWebinar: Record<string, { sent: number; yes: number; maybe: number; ym: number }>;
  senderName: string | null;
}) {
  return (
    <tr className="hover:bg-zinc-50 dark:hover:bg-zinc-900/60">
      <td className={`${L_ACC} ${Z_ROW} ${BG_LIST} ${W_ACC} px-3 py-1.5 font-mono text-emerald-500 dark:text-emerald-400 truncate`} title={row.calendar_account}>
        {row.calendar_account}
      </td>
      <td className={`${L_WS} ${Z_ROW} ${BG_LIST} ${W_WS} px-3 py-1.5 text-zinc-500 dark:text-zinc-500`}>
        &nbsp;
      </td>
      <td className={`${L_SENDER} ${Z_ROW} ${BG_LIST} ${W_SENDER} px-3 py-1.5 truncate ${senderName ? "text-zinc-800 dark:text-zinc-200" : "text-zinc-500"}`} title={senderName ?? ""}>
        {senderName ?? "—"}
      </td>
      <td className={`${L_NOTE} ${Z_ROW} ${BG_LIST} ${W_NOTE} px-3 py-1.5 text-zinc-500 dark:text-zinc-500 border-r border-zinc-200 dark:border-zinc-800`}>
        &nbsp;
      </td>
      {webinars.map((w, idx) => (
        <MetricGroup
          key={w.id}
          cell={row.per_webinar[w.id]}
          max={maxByWebinar[w.id]}
          isFirst={idx === 0}
        />
      ))}
    </tr>
  );
}

function MetricGroup({
  cell,
  max,
  isFirst,
}: {
  cell: ApiAccountHealthCell | undefined;
  max: { sent: number; yes: number; maybe: number; ym: number };
  isFirst: boolean;
}) {
  const sent = cell?.total_sent ?? 0;
  const yes = cell?.yes ?? 0;
  const maybe = cell?.maybe ?? 0;
  const ym = yes + maybe;
  const pct = sent > 0 ? ym / sent : null;

  const base = `${W_METRIC} text-zinc-700 dark:text-zinc-300`;
  const lastBase = `${base} border-r border-zinc-200 dark:border-zinc-800`;
  const sentClass = isFirst
    ? `${L_FIRST_TOT} ${Z_ROW} ${BG_LIST} ${W_FIRST_TOT} ${FREEZE_EDGE} px-2 py-1.5 text-right tabular-nums text-zinc-700 dark:text-zinc-300`
    : base;

  return (
    <>
      <td className={sentClass}>{sent > 0 ? fmtInt(sent) : <span className="text-zinc-500">0</span>}</td>
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

/* ── Set Senders modal (Pattern B) ────────────────────────────────────── */

function SetSendersModal({
  webinars,
  senders,
  onClose,
  onSaved,
}: {
  webinars: ApiAccountHealthWebinar[];
  senders: ApiAccountHealthSender[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [webinarId, setWebinarId] = useState(webinars[0]?.id ?? "");
  const [senderId, setSenderId] = useState(senders[0]?.id ?? "");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const previewAccounts = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const line of text.split(/\r?\n|,/)) {
      const v = line.trim().toLowerCase();
      if (v && !seen.has(v)) {
        seen.add(v);
        out.push(v);
      }
    }
    return out;
  }, [text]);

  const handleSave = async () => {
    if (!webinarId || !senderId || previewAccounts.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      await setCalendarAccountSendersBulk(webinarId, senderId, previewAccounts);
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-xl w-full max-h-[85vh] overflow-y-auto"
      >
        <div className="px-6 py-4 border-b border-zinc-200 dark:border-zinc-800 flex justify-between items-center">
          <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
            Set Senders for Calendar Accounts
          </h3>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-200 text-lg leading-none">×</button>
        </div>
        <div className="px-6 py-5 space-y-4">
          {error && (
            <div className="px-3 py-2 rounded-md bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
              {error}
            </div>
          )}
          <div>
            <label className="block text-xs font-semibold text-zinc-700 dark:text-zinc-300 mb-1.5">
              Webinar
            </label>
            <select
              value={webinarId}
              onChange={(e) => setWebinarId(e.target.value)}
              className="w-full bg-zinc-50 dark:bg-zinc-950 border border-zinc-300 dark:border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
            >
              {webinars.map((w) => (
                <option key={w.id} value={w.id}>{w.label}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-semibold text-zinc-700 dark:text-zinc-300 mb-1.5">
              Sender
            </label>
            <select
              value={senderId}
              onChange={(e) => setSenderId(e.target.value)}
              className="w-full bg-zinc-50 dark:bg-zinc-950 border border-zinc-300 dark:border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
            >
              {senders.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-semibold text-zinc-700 dark:text-zinc-300 mb-1.5">
              Calendar accounts — one per line
            </label>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={"alex@webinarsalesengine.co\nalexander@webinarsthatscale.com\n…"}
              rows={10}
              className="w-full bg-zinc-50 dark:bg-zinc-950 border border-zinc-300 dark:border-zinc-700 rounded-lg px-3 py-2 text-xs font-mono text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
            />
            <div className="mt-1 text-[11px] text-zinc-500">
              {previewAccounts.length} unique account{previewAccounts.length === 1 ? "" : "s"}.
              Existing mappings for this webinar are overwritten.
            </div>
          </div>
        </div>
        <div className="px-6 py-4 border-t border-zinc-200 dark:border-zinc-800 flex justify-end gap-2">
          <button onClick={onClose} className="px-3 py-1.5 text-xs rounded-lg bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-200">
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={busy || !webinarId || !senderId || previewAccounts.length === 0}
            className="px-3 py-1.5 text-xs rounded-lg bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
