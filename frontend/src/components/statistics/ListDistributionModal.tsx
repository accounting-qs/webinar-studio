"use client";

import { useEffect, useState } from "react";
import { fetchListDistribution, type ListDistributionResponse } from "@/lib/api";

type DistTab = "list" | "domain";

/** What opens the list-name distribution modal. */
export type ListDistTarget = {
  scope: "assignment" | "webinar";
  assignment?: string | null;
  webinarId?: string | null;
  webinarNumber?: number;
  /** Human label for the header (list description or "Webinar N"). */
  label: string;
};

/** Modal showing how the contacts in an assigned list — or across all assigned
 * lists of a webinar — break down by their source list name
 * (contacts.lead_list_name). Each row is a list name, its contact count, and
 * its percentage share of the scope. */
export function ListDistributionModal({ target, onClose }: { target: ListDistTarget; onClose: () => void }) {
  const [data, setData] = useState<ListDistributionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<DistTab>("list");

  // Escape to close + lock body scroll while open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    (async () => {
      try {
        const res = await fetchListDistribution({
          assignment: target.scope === "assignment" ? target.assignment ?? undefined : undefined,
          webinarId: target.scope === "webinar" ? target.webinarId ?? undefined : undefined,
          webinarNumber: target.scope === "webinar" ? target.webinarNumber ?? undefined : undefined,
        });
        if (!cancelled) setData(res);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [target.scope, target.assignment, target.webinarId, target.webinarNumber]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-2xl w-full max-h-[85vh] overflow-y-auto"
      >
        <div className="flex items-start justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800 sticky top-0 bg-white dark:bg-zinc-900 z-10">
          <div>
            <div className="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold mb-0.5">
              {target.scope === "webinar" ? "Webinar · all assigned lists" : "Assigned list"}
            </div>
            <div className="text-base font-bold text-zinc-900 dark:text-zinc-100">{target.label}</div>
            <div className="text-[11px] text-zinc-500 mt-0.5">
              {tab === "list" ? "List-name distribution" : "Email-domain distribution"}
              {data
                ? tab === "list"
                  ? ` · ${data.total} contact${data.total === 1 ? "" : "s"} · ${data.items.length} list${data.items.length === 1 ? "" : "s"}`
                  : ` · ${data.domains.total} contact${data.domains.total === 1 ? "" : "s"} · ${data.domains.unique_domains} domain${data.domains.unique_domains === 1 ? "" : "s"}`
                : ""}
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
            aria-label="Close"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Tab switcher */}
        <div className="px-6 pt-3 flex items-center gap-1">
          {(["list", "domain"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
                tab === t
                  ? "bg-violet-500/15 text-violet-500 border border-violet-500/30"
                  : "text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 border border-transparent"
              }`}
            >
              {t === "list" ? "By list name" : "By domain"}
            </button>
          ))}
        </div>

        <div className="px-6 py-5 space-y-4">
          <p className="text-[11px] text-zinc-500 dark:text-zinc-400 leading-relaxed">
            {tab === "list" ? (
              <>
                Each contact carries the source list name it was uploaded under. This shows what
                share of the contacts {target.scope === "webinar" ? "across all assigned lists for this webinar" : "in this assigned list"} came from each list name.
              </>
            ) : (
              <>
                Email domains across the contacts {target.scope === "webinar" ? "in all assigned lists for this webinar" : "in this assigned list"}. Free /
                personal providers (Gmail, Outlook, …) are flagged so you can see how many contacts aren&apos;t on a company domain.
              </>
            )}
          </p>

          {loading && (
            <div className="flex items-center gap-3 py-8 justify-center">
              <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
              <span className="text-sm text-zinc-500">Loading distribution…</span>
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-400">{error}</div>
          )}

          {data && !loading && tab === "list" && (
            data.items.length === 0 ? (
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-500">
                No contacts found for this {target.scope === "webinar" ? "webinar" : "list"}.
              </div>
            ) : (
              <DistTable
                head="List name"
                rows={data.items.map((it) => ({
                  label: it.list_name,
                  emptyLabel: "— no list name",
                  count: it.count,
                  pct: it.pct,
                }))}
              />
            )
          )}

          {data && !loading && tab === "domain" && (
            data.domains.total === 0 ? (
              <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-500">
                No contacts with an email address found for this {target.scope === "webinar" ? "webinar" : "list"}.
              </div>
            ) : (
              <>
                {/* Summary stats */}
                <div className="grid grid-cols-3 gap-2">
                  <Stat label="Unique domains" value={data.domains.unique_domains.toLocaleString()} />
                  <Stat
                    label="Free-domain contacts"
                    value={data.domains.free_domain_contacts.toLocaleString()}
                    sub={data.domains.total ? `${((100 * data.domains.free_domain_contacts) / data.domains.total).toFixed(1)}%` : undefined}
                  />
                  <Stat label="Free domains" value={data.domains.free_domain_unique.toLocaleString()} />
                </div>

                <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold pt-1">
                  Top {data.domains.top.length} domain{data.domains.top.length === 1 ? "" : "s"}
                </div>
                <DistTable
                  head="Domain"
                  rows={data.domains.top.map((it) => ({
                    label: it.domain,
                    emptyLabel: "— no domain",
                    count: it.count,
                    pct: it.pct,
                    badge: it.is_free ? "free" : undefined,
                  }))}
                />
              </>
            )
          )}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-zinc-500 font-semibold">{label}</div>
      <div className="text-lg font-bold text-zinc-900 dark:text-zinc-100 tabular-nums leading-tight">
        {value}
        {sub && <span className="ml-1 text-xs font-medium text-zinc-500">{sub}</span>}
      </div>
    </div>
  );
}

type DistRow = { label: string | null; emptyLabel: string; count: number; pct: number; badge?: string };

function DistTable({ head, rows }: { head: string; rows: DistRow[] }) {
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
          <th className="py-1.5 pr-2 font-semibold">{head}</th>
          <th className="py-1.5 px-2 font-semibold text-right w-20">Contacts</th>
          <th className="py-1.5 pl-2 font-semibold text-right w-32">Share</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((it, i) => (
          <tr key={i} className="border-b border-zinc-100 dark:border-zinc-800/40">
            <td className="py-1.5 pr-2 text-zinc-800 dark:text-zinc-200">
              {it.label ?? <span className="italic text-zinc-400">{it.emptyLabel}</span>}
              {it.badge && (
                <span className="ml-1.5 inline-block px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-500/15 text-amber-500 border border-amber-500/30 align-middle">
                  {it.badge}
                </span>
              )}
            </td>
            <td className="py-1.5 px-2 text-right tabular-nums text-zinc-700 dark:text-zinc-300">
              {it.count.toLocaleString()}
            </td>
            <td className="py-1.5 pl-2 text-right">
              <div className="flex items-center justify-end gap-2">
                <div className="flex-1 h-1.5 bg-zinc-100 dark:bg-zinc-800 rounded-full overflow-hidden max-w-[80px]">
                  <div className="h-full bg-violet-500 rounded-full" style={{ width: `${it.pct}%` }} />
                </div>
                <span className="tabular-nums text-zinc-600 dark:text-zinc-400 w-12 text-right">
                  {it.pct.toFixed(1)}%
                </span>
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
