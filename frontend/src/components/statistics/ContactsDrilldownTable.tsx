"use client";

import { Fragment, useMemo, useState } from "react";
import { type ContactDrilldownItem, type ContactDrilldownResponse } from "@/lib/api";

type Item = ContactDrilldownItem;

/* ── helpers ──────────────────────────────────────────────────────────── */

/** Free / public email providers — a shared domain here does NOT imply the
 * same company, so it must not group two different people together. */
const FREE_EMAIL_DOMAINS = new Set([
  "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in",
  "ymail.com", "rocketmail.com", "hotmail.com", "hotmail.co.uk", "outlook.com",
  "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com",
  "protonmail.com", "proton.me", "pm.me", "gmx.com", "gmx.de", "mail.com",
  "zoho.com", "yandex.com", "yandex.ru", "fastmail.com", "hey.com",
  "qq.com", "163.com", "126.com", "comcast.net", "verizon.net", "att.net",
  "sbcglobal.net", "btinternet.com", "web.de", "t-online.de",
]);

function fullName(it: Item): string {
  return [it.first_name, it.last_name].filter(Boolean).join(" ").trim();
}
function normName(it: Item): string {
  return fullName(it).toLowerCase().replace(/\s+/g, " ");
}
function normEmail(it: Item): string {
  return (it.email || "").toLowerCase().trim();
}
/** Email domain for grouping — empty (no grouping) for free providers. */
function companyDomain(it: Item): string {
  const e = normEmail(it);
  const at = e.lastIndexOf("@");
  if (at <= 0) return "";
  const d = e.slice(at + 1);
  return FREE_EMAIL_DOMAINS.has(d) ? "" : d;
}
function websiteHref(url: string | null | undefined): string | null {
  const u = (url || "").trim();
  if (!u) return null;
  return /^https?:\/\//i.test(u) ? u : `https://${u}`;
}

/* ── grouping (union-find over name | email | company-domain) ─────────── */

type Group = {
  key: string;
  rows: Item[];
  rep: Item;
  count: number;
  valueSum: number;
};

function repScore(it: Item): number {
  let s = 0;
  if (normName(it)) s += 2;
  if (it.company_website) s += 1;
  if (it.opportunity_id) s += 1;
  return s;
}

function groupItems(items: Item[]): Group[] {
  const parent = items.map((_, i) => i);
  const find = (x: number): number => {
    while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; }
    return x;
  };
  const union = (a: number, b: number) => {
    const ra = find(a), rb = find(b);
    if (ra !== rb) parent[ra] = rb;
  };
  const link = (keyFn: (it: Item) => string) => {
    const seen = new Map<string, number>();
    items.forEach((it, i) => {
      const k = keyFn(it);
      if (!k) return;
      const prev = seen.get(k);
      if (prev === undefined) seen.set(k, i);
      else union(prev, i);
    });
  };
  link(normName);
  link(normEmail);
  link(companyDomain);

  const byRoot = new Map<number, Item[]>();
  const order: number[] = [];
  items.forEach((it, i) => {
    const r = find(i);
    if (!byRoot.has(r)) { byRoot.set(r, []); order.push(r); }
    byRoot.get(r)!.push(it);
  });
  return order.map((r) => {
    const rows = byRoot.get(r)!;
    const rep = rows.reduce((best, cur) => (repScore(cur) > repScore(best) ? cur : best), rows[0]);
    const valueSum = rows.reduce((s, x) => s + (x.opportunity_value ?? 0), 0);
    return { key: String(r), rows, rep, count: rows.length, valueSum };
  });
}

/* ── sorting ──────────────────────────────────────────────────────────── */

type SortKey = "name" | "email" | "website" | "source" | "medium" | "bookName" | "content" | "term" | "bookId" | "call1" | "quality" | "value";

function sortVal(g: Group, key: SortKey): string | number {
  const it = g.rep;
  switch (key) {
    case "name": return normName(it) || "￿";
    case "email": return normEmail(it) || "￿";
    case "website": return (it.company_website || "￿").toLowerCase();
    case "source": return (it.book_source || "￿").toLowerCase();
    case "medium": return (it.book_medium || "￿").toLowerCase();
    case "bookName": return (it.book_name || "￿").toLowerCase();
    case "content": return (it.book_content || "￿").toLowerCase();
    case "term": return (it.book_term || "￿").toLowerCase();
    case "bookId": return (it.book_id || "￿").toLowerCase();
    case "call1": return (it.call1_status || "￿").toLowerCase();
    case "quality": return (it.lead_quality || "￿").toLowerCase();
    case "value": return g.valueSum;
  }
}

/* ── chart ────────────────────────────────────────────────────────────── */

const CHART_COLORS = [
  "#8b5cf6", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#ec4899",
  "#3b82f6", "#84cc16", "#a855f7", "#14b8a6", "#f97316", "#64748b",
];

function Donut({ slices, total }: { slices: { label: string; value: number; color: string }[]; total: number }) {
  const size = 132, stroke = 20, r = (size - stroke) / 2, C = 2 * Math.PI * r;
  // Prefix-sum offsets (no render-phase mutation; n is tiny).
  const lens = slices.map((s) => (s.value / (total || 1)) * C);
  const offsets = lens.map((_, i) => -lens.slice(0, i).reduce((a, b) => a + b, 0));
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={stroke}
        className="stroke-zinc-200 dark:stroke-zinc-800" />
      <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
        {slices.map((s, i) => (
          <circle key={i} cx={size / 2} cy={size / 2} r={r} fill="none" stroke={s.color}
            strokeWidth={stroke} strokeDasharray={`${lens[i]} ${C - lens[i]}`} strokeDashoffset={offsets[i]} />
        ))}
      </g>
      <text x="50%" y="45%" textAnchor="middle" dominantBaseline="central"
        className="fill-zinc-900 dark:fill-zinc-100 font-bold" style={{ fontSize: 20 }}>{total}</text>
      <text x="50%" y="59%" textAnchor="middle" dominantBaseline="central"
        className="fill-zinc-500" style={{ fontSize: 9 }}>groups</text>
    </svg>
  );
}

function BookingSourceChart({ groups, itemCount }: { groups: Group[]; itemCount: number }) {
  const slices = useMemo(() => {
    const m = new Map<string, number>();
    for (const g of groups) {
      const k = (g.rep.book_source || "").trim() || "—";
      m.set(k, (m.get(k) ?? 0) + 1);
    }
    return [...m.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([label, value], i) => ({ label, value, color: CHART_COLORS[i % CHART_COLORS.length] }));
  }, [groups]);

  const hasSource = groups.some((g) => g.rep.book_source || g.rep.book_medium);
  if (!hasSource) return null;

  const total = groups.length;
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-zinc-50/60 dark:bg-zinc-900/30 px-4 py-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">Booking source</div>
        <div className="text-[11px] text-zinc-500">{total} unique · {itemCount} bookings</div>
      </div>
      <div className="flex items-center gap-5">
        <Donut slices={slices} total={total} />
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 min-w-0 flex-1">
          {slices.map((s) => (
            <div key={s.label} className="flex items-center gap-2 text-xs min-w-0">
              <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: s.color }} />
              <span className="truncate text-zinc-700 dark:text-zinc-300">{s.label}</span>
              <span className="ml-auto font-mono text-zinc-500">{s.value}</span>
              <span className="font-mono text-zinc-400 w-9 text-right">{Math.round((s.value / (total || 1)) * 100)}%</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ── small cell components ────────────────────────────────────────────── */

function ExternalIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  );
}

function LinkCell({ href, color, title }: { href: string | null | undefined; color: string; title: string }) {
  if (!href) return <span className="text-zinc-500">—</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={`inline-flex ${color}`} title={title}>
      <ExternalIcon />
    </a>
  );
}

function WebsiteCell({ url }: { url: string | null | undefined }) {
  const href = websiteHref(url);
  if (!href) return <span className="text-zinc-500">—</span>;
  const text = (url || "").replace(/^https?:\/\//i, "").replace(/\/$/, "");
  return (
    <a href={href} target="_blank" rel="noopener noreferrer"
      className="text-violet-500 hover:text-violet-400 truncate inline-block max-w-[180px] align-bottom" title={href}>
      {text}
    </a>
  );
}

function SortHeader({ label, k, sort, onSort, align = "left" }: {
  label: string; k: SortKey; sort: { key: SortKey; dir: "asc" | "desc" } | null;
  onSort: (k: SortKey) => void; align?: "left" | "right" | "center";
}) {
  const active = sort?.key === k;
  const arrow = active ? (sort!.dir === "asc" ? "▲" : "▼") : "";
  const alignCls = align === "right" ? "justify-end" : align === "center" ? "justify-center" : "justify-start";
  return (
    <th className="px-3 py-2">
      <button type="button" onClick={() => onSort(k)}
        className={`w-full flex items-center gap-1 text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 font-semibold uppercase tracking-wider text-[10px] ${alignCls}`}>
        <span>{label}</span>
        <span className={`text-[8px] w-2 ${active ? "text-violet-500" : "text-transparent"}`}>{arrow || "▲"}</span>
      </button>
    </th>
  );
}

function PlainHeader({ label }: { label: string }) {
  return <th className="px-3 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] text-center">{label}</th>;
}

/* ── table ────────────────────────────────────────────────────────────── */

/** Contact / opportunity drill-down with duplicate-grouping, a booking-source
 * donut, reorderable links, and sortable columns. Shared by the modal and the
 * full-page view so they render identically. */
export function ContactsDrilldownTable({ data }: { data: ContactDrilldownResponse }) {
  const isOpp = data.unit === "opportunity";
  const groups = useMemo(() => groupItems(data.items), [data.items]);
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" } | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const onSort = (k: SortKey) =>
    setSort((s) => (s && s.key === k ? { key: k, dir: s.dir === "asc" ? "desc" : "asc" } : { key: k, dir: "asc" }));

  const sortedGroups = useMemo(() => {
    if (!sort) return groups;
    const arr = [...groups];
    arr.sort((a, b) => {
      const av = sortVal(a, sort.key), bv = sortVal(b, sort.key);
      const c = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
      return sort.dir === "asc" ? c : -c;
    });
    return arr;
  }, [groups, sort]);

  const toggle = (key: string) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(key)) n.delete(key); else n.add(key);
      return n;
    });

  if (data.items.length === 0) {
    return <p className="text-sm text-zinc-500">No {isOpp ? "opportunities" : "contacts"} found.</p>;
  }

  const money = (v: number | null | undefined) => (v != null && v !== 0 ? `$${v.toLocaleString()}` : "—");

  return (
    <div className="space-y-3">
      <BookingSourceChart groups={groups} itemCount={data.items.length} />
      <div className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/20 overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-zinc-50 dark:bg-zinc-900/50 border-b border-zinc-200 dark:border-zinc-800/40">
              <SortHeader label="Name" k="name" sort={sort} onSort={onSort} />
              <SortHeader label="Email" k="email" sort={sort} onSort={onSort} />
              <PlainHeader label="GHL" />
              {isOpp && <PlainHeader label="Opp" />}
              <SortHeader label="Website" k="website" sort={sort} onSort={onSort} />
              {isOpp && <SortHeader label="Call 1 Status" k="call1" sort={sort} onSort={onSort} />}
              {isOpp && <SortHeader label="Lead Quality" k="quality" sort={sort} onSort={onSort} />}
              {isOpp && <SortHeader label="Value" k="value" sort={sort} onSort={onSort} align="right" />}
              <SortHeader label="Book Source" k="source" sort={sort} onSort={onSort} />
              <SortHeader label="Book Medium" k="medium" sort={sort} onSort={onSort} />
              <SortHeader label="Book Name" k="bookName" sort={sort} onSort={onSort} />
              <SortHeader label="Book Content" k="content" sort={sort} onSort={onSort} />
              <SortHeader label="Book Term" k="term" sort={sort} onSort={onSort} />
              <SortHeader label="Book ID" k="bookId" sort={sort} onSort={onSort} />
            </tr>
          </thead>
          <tbody>
            {sortedGroups.map((g) => {
              const it = g.rep;
              const grouped = g.count > 1;
              const isExp = expanded.has(g.key);
              return (
                <Fragment key={g.key}>
                  <tr className="border-t border-zinc-200 dark:border-zinc-800/20">
                    <td className="px-3 py-2 text-zinc-800 dark:text-zinc-200">
                      <div className="flex items-center gap-1.5">
                        {grouped ? (
                          <button type="button" onClick={() => toggle(g.key)}
                            className="text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 w-3 shrink-0"
                            title={isExp ? "Collapse group" : `Expand ${g.count} grouped bookings`}>
                            {isExp ? "▾" : "▸"}
                          </button>
                        ) : <span className="w-3 shrink-0 inline-block" />}
                        <span className="truncate">{fullName(it) || "—"}</span>
                        {grouped && (
                          <span className="px-1.5 py-0.5 rounded-full text-[9px] font-semibold bg-violet-500/15 text-violet-500 border border-violet-500/30 shrink-0"
                            title={`${g.count} bookings grouped (same name, email, or company domain)`}>×{g.count}</span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400 font-mono">{it.email ?? "—"}</td>
                    <td className="px-3 py-2 text-center"><LinkCell href={it.ghl_url} color="text-violet-500 hover:text-violet-400" title="Open contact in GHL" /></td>
                    {isOpp && <td className="px-3 py-2 text-center"><LinkCell href={it.opportunity_url} color="text-sky-500 hover:text-sky-400" title="Open opportunity in GHL" /></td>}
                    <td className="px-3 py-2"><WebsiteCell url={it.company_website} /></td>
                    {isOpp && <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">{it.call1_status ?? "—"}</td>}
                    {isOpp && <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">{it.lead_quality ?? "—"}</td>}
                    {isOpp && <td className="px-3 py-2 text-right font-mono text-zinc-600 dark:text-zinc-400">{money(grouped ? g.valueSum : it.opportunity_value)}</td>}
                    <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">{it.book_source ?? "—"}</td>
                    <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">{it.book_medium ?? "—"}</td>
                    <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">{it.book_name ?? "—"}</td>
                    <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">{it.book_content ?? "—"}</td>
                    <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">{it.book_term ?? "—"}</td>
                    <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400 font-mono">{it.book_id ?? "—"}</td>
                  </tr>

                  {isExp && grouped && g.rows.map((m, mi) => (
                    <tr key={`${g.key}-m${mi}`} className="border-t border-zinc-100 dark:border-zinc-800/10 bg-zinc-50/50 dark:bg-zinc-900/30">
                      <td className="px-3 py-1.5 text-zinc-600 dark:text-zinc-400">
                        <span className="pl-6 block truncate">{fullName(m) || "—"}</span>
                      </td>
                      <td className="px-3 py-1.5 text-zinc-500 font-mono">{m.email ?? "—"}</td>
                      <td className="px-3 py-1.5 text-center"><LinkCell href={m.ghl_url} color="text-violet-500/80 hover:text-violet-400" title="Open contact in GHL" /></td>
                      {isOpp && <td className="px-3 py-1.5 text-center"><LinkCell href={m.opportunity_url} color="text-sky-500/80 hover:text-sky-400" title="Open opportunity in GHL" /></td>}
                      <td className="px-3 py-1.5"><WebsiteCell url={m.company_website} /></td>
                      {isOpp && <td className="px-3 py-1.5 text-zinc-500">{m.call1_status ?? "—"}</td>}
                      {isOpp && <td className="px-3 py-1.5 text-zinc-500">{m.lead_quality ?? "—"}</td>}
                      {isOpp && <td className="px-3 py-1.5 text-right font-mono text-zinc-500">{money(m.opportunity_value)}</td>}
                      <td className="px-3 py-1.5 text-zinc-500">{m.book_source ?? "—"}</td>
                      <td className="px-3 py-1.5 text-zinc-500">{m.book_medium ?? "—"}</td>
                      <td className="px-3 py-1.5 text-zinc-500">{m.book_name ?? "—"}</td>
                      <td className="px-3 py-1.5 text-zinc-500">{m.book_content ?? "—"}</td>
                      <td className="px-3 py-1.5 text-zinc-500">{m.book_term ?? "—"}</td>
                      <td className="px-3 py-1.5 text-zinc-500 font-mono">{m.book_id ?? "—"}</td>
                    </tr>
                  ))}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
