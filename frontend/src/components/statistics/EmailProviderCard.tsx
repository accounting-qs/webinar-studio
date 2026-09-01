"use client";

/**
 * Invite response (Yes / Maybe) by recipient mailbox provider.
 *
 * The audience is ~98% company domains, so the domain name says nothing about
 * which mailbox receives the calendar invite — the provider label comes from
 * the domain's MX record, resolved once per domain into a cache. Domains the
 * backfill has not reached yet show as "Not resolved yet" rather than being
 * dropped, so the invited volumes always add up to the real audience.
 *
 * Shared by the Statistics Home page and the per-webinar report, which pass
 * different webinar sets to the same endpoint.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchEmailProviderBreakdown,
  type ProviderBreakdownResponse,
  type ProviderRow,
  type ProviderScopeKey,
} from "@/lib/api";
import { CAT, ChartCard, EmptyChart, fmtValue } from "./charts";

const UNRESOLVED = "Not resolved yet";

const SCOPES: { key: ProviderScopeKey; label: string; hint: string }[] = [
  {
    key: "assigned", label: "Assigned lists",
    hint: "The planned lists for the webinar — the contacts we deliberately invited.",
  },
  {
    key: "nonjoiners", label: "Non-joiners",
    hint: "Registrants of the recent webinars who never joined any of them, re-invited to this one.",
  },
  {
    key: "newJoiners", label: "New joiners",
    hint: "Assigned lists plus NO LIST DATA, with non-joiners excluded.",
  },
  {
    key: "overall", label: "Overall",
    hint: "Everything attributed to the webinar, non-joiners included.",
  },
];

function Pct({ v }: { v: number | null }) {
  if (v == null) return <span className="text-zinc-400">—</span>;
  return <span className="tabular-nums">{(v * 100).toFixed(2)}%</span>;
}

export function EmailProviderCard({
  webinarIds,
  subtitle,
}: {
  webinarIds: string[];
  subtitle?: string;
}) {
  const [scope, setScope] = useState<ProviderScopeKey>("assigned");
  const [data, setData] = useState<ProviderBreakdownResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // The endpoint scans membership per webinar, so it is capped server-side.
  const ids = useMemo(() => webinarIds.slice(0, 12), [webinarIds]);
  const key = ids.join(",");

  const load = useCallback(async () => {
    if (!ids.length) { setLoading(false); return; }
    setError(null);
    try {
      setData(await fetchEmailProviderBreakdown(ids));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
    // `key` is the stable identity of `ids` — depending on the array itself
    // would refetch on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    load();
  }, [load]);

  const rows: ProviderRow[] = data?.totals?.[scope] ?? [];
  const invitedTotal = rows.reduce((s, r) => s + r.invited, 0);
  const maxInvited = Math.max(...rows.map((r) => r.invited), 1);
  const unresolved = rows.find((r) => r.provider === UNRESOLVED);

  return (
    <ChartCard
      title="Invite response by mailbox provider"
      subtitle={
        subtitle ??
        "Where the invite actually landed, from each domain's MX record — Google Workspace, Microsoft 365, a security gateway, and so on."
      }
      right={
        <div className="inline-flex rounded-lg border border-zinc-200 dark:border-zinc-800 p-0.5 bg-zinc-50 dark:bg-zinc-900">
          {SCOPES.map((s) => (
            <button
              key={s.key}
              type="button"
              title={s.hint}
              onClick={() => setScope(s.key)}
              className={`px-2 py-1 text-[11px] font-semibold rounded-md transition-colors ${
                scope === s.key
                  ? "bg-white dark:bg-zinc-800 text-zinc-900 dark:text-zinc-100 shadow-sm"
                  : "text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      }
    >
      {loading ? (
        <div className="h-32 flex items-center justify-center gap-2 text-[12px] text-zinc-500">
          <span className="inline-block w-3.5 h-3.5 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
          Scanning the invited audience…
        </div>
      ) : error ? (
        <div className="h-24 flex items-center justify-center text-[12px] text-red-500">{error}</div>
      ) : !rows.length ? (
        <EmptyChart message="No invited contacts in this cohort." />
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="text-left px-2 py-1.5 text-[10px] font-bold uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-700">
                    Provider
                  </th>
                  <th className="text-right px-2 py-1.5 text-[10px] font-bold uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-700">
                    Invited
                  </th>
                  <th className="text-right px-2 py-1.5 text-[10px] font-bold uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-700">
                    <span className="inline-block w-2 h-2 rounded-[2px] mr-1 align-middle" style={{ background: CAT[0] }} aria-hidden />
                    Yes
                  </th>
                  <th className="text-right px-2 py-1.5 text-[10px] font-bold uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-700">
                    Yes %
                  </th>
                  <th className="text-right px-2 py-1.5 text-[10px] font-bold uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-700">
                    <span className="inline-block w-2 h-2 rounded-[2px] mr-1 align-middle" style={{ background: CAT[1] }} aria-hidden />
                    Maybe
                  </th>
                  <th className="text-right px-2 py-1.5 text-[10px] font-bold uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-700">
                    Maybe %
                  </th>
                  <th
                    title="(Yes + Maybe) ÷ invited"
                    className="text-right px-2 py-1.5 text-[10px] font-bold uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-700"
                  >
                    Responded %
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.provider} className="hover:bg-zinc-50 dark:hover:bg-zinc-800/40">
                    <td className="px-2 py-1.5 border-b border-zinc-100 dark:border-zinc-800/60 min-w-[190px]">
                      <div className="flex items-center justify-between gap-2">
                        <span
                          className={`text-[12px] truncate ${
                            r.provider === UNRESOLVED
                              ? "text-zinc-500 italic"
                              : "text-zinc-800 dark:text-zinc-200"
                          }`}
                        >
                          {r.provider}
                        </span>
                        <span className="text-[10px] text-zinc-400 tabular-nums shrink-0">
                          {invitedTotal ? `${((r.invited / invitedTotal) * 100).toFixed(1)}%` : "—"}
                        </span>
                      </div>
                      <div className="h-1 mt-1 bg-zinc-100 dark:bg-zinc-800 rounded-full">
                        <div
                          className="h-1 rounded-full"
                          style={{
                            width: `${(r.invited / maxInvited) * 100}%`,
                            background: r.provider === UNRESOLVED ? "var(--v-q-unrated)" : "var(--v-cat-1)",
                          }}
                        />
                      </div>
                    </td>
                    <td className="px-2 py-1.5 text-right text-[12px] tabular-nums text-zinc-800 dark:text-zinc-200 border-b border-zinc-100 dark:border-zinc-800/60">
                      {fmtValue(r.invited, "int")}
                    </td>
                    <td className="px-2 py-1.5 text-right text-[12px] tabular-nums text-zinc-800 dark:text-zinc-200 border-b border-zinc-100 dark:border-zinc-800/60">
                      {fmtValue(r.yes, "int")}
                    </td>
                    <td className="px-2 py-1.5 text-right text-[12px] font-semibold text-zinc-900 dark:text-zinc-100 border-b border-zinc-100 dark:border-zinc-800/60">
                      <Pct v={r.yesPct} />
                    </td>
                    <td className="px-2 py-1.5 text-right text-[12px] tabular-nums text-zinc-800 dark:text-zinc-200 border-b border-zinc-100 dark:border-zinc-800/60">
                      {fmtValue(r.maybe, "int")}
                    </td>
                    <td className="px-2 py-1.5 text-right text-[12px] font-semibold text-zinc-900 dark:text-zinc-100 border-b border-zinc-100 dark:border-zinc-800/60">
                      <Pct v={r.maybePct} />
                    </td>
                    <td className="px-2 py-1.5 text-right text-[12px] font-semibold text-zinc-900 dark:text-zinc-100 border-b border-zinc-100 dark:border-zinc-800/60">
                      <Pct v={r.respondedPct} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {unresolved && unresolved.invited > 0 && (
            <p className="mt-2.5 text-[11px] text-amber-600 dark:text-amber-400">
              {fmtValue(unresolved.invited, "int")} of {fmtValue(invitedTotal, "int")} invited
              contacts ({((unresolved.invited / invitedTotal) * 100).toFixed(1)}%) sit on domains the
              MX backfill has not resolved yet — their responses are counted in that row, not
              redistributed. Run{" "}
              <code className="px-1 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800 font-mono text-[10px]">
                scripts/resolve_email_providers.py
              </code>{" "}
              to shrink it.
            </p>
          )}
          {data && (
            <p className="mt-1.5 text-[10px] text-zinc-500">
              {fmtValue(data.resolution.resolved, "int")} of{" "}
              {fmtValue(data.resolution.domains, "int")} cached domains resolved to a live mail
              server. Provider is read from each domain&apos;s MX record; a security gateway
              (Proofpoint, Mimecast, …) fronts the real mailbox and is reported as itself rather
              than guessed at.
            </p>
          )}
        </>
      )}
    </ChartCard>
  );
}
