"use client";

import { useEffect, useRef, useState } from "react";
import { fetchContactsDirectory, type ApiContactSummary } from "@/lib/api";
import { BlocklistedBadge, StatusBadge, fmtDate } from "./ContactBadges";
import { ContactDetailDrawer } from "./ContactDetailDrawer";

const PAGE_SIZE = 100;
const MIN_SEARCH_LEN = 3;

// One object so a new search atomically resets paging (no stale-offset fetch).
type PageState = {
  search: string;
  pageIdx: number;
  // Browse mode: keyset cursor that produced page i ("" = first page).
  cursors: string[];
};

export function ContactsDirectoryPage() {
  const [query, setQuery] = useState("");
  const [pageState, setPageState] = useState<PageState>({ search: "", pageIdx: 0, cursors: [""] });
  const [rows, setRows] = useState<ApiContactSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [totalKind, setTotalKind] = useState<"estimated" | "exact" | "capped">("estimated");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const reqRef = useRef(0);

  /* ── Debounced search → page state ──────────────────────────────────── */

  useEffect(() => {
    const trimmed = query.trim();
    const target = trimmed.length >= MIN_SEARCH_LEN ? trimmed : "";
    const t = setTimeout(() => {
      setPageState((s) => (s.search === target ? s : { search: target, pageIdx: 0, cursors: [""] }));
    }, 400);
    return () => clearTimeout(t);
  }, [query]);

  /* ── Fetch ──────────────────────────────────────────────────────────── */

  useEffect(() => {
    const id = ++reqRef.current;
    setLoading(true);
    setError(null);
    const { search, pageIdx, cursors } = pageState;
    const opts = search
      ? { search, limit: PAGE_SIZE, offset: pageIdx * PAGE_SIZE }
      : { limit: PAGE_SIZE, cursor: cursors[pageIdx] || undefined };
    fetchContactsDirectory(opts)
      .then((r) => {
        if (reqRef.current !== id) return;
        setRows(r.contacts);
        setTotal(r.total);
        setTotalKind(r.total_kind);
        setNextCursor(r.next_cursor);
        setLoading(false);
        setLoadedOnce(true);
      })
      .catch((e) => {
        if (reqRef.current !== id) return;
        setError(e instanceof Error ? e.message : "Failed to load contacts");
        setLoading(false);
        setLoadedOnce(true);
      });
  }, [pageState]);

  /* ── Pagination ─────────────────────────────────────────────────────── */

  const isSearch = pageState.search.length > 0;
  const rangeStart = pageState.pageIdx * PAGE_SIZE + 1;
  const rangeEnd = pageState.pageIdx * PAGE_SIZE + rows.length;
  const hasPrev = pageState.pageIdx > 0;
  const hasNext = isSearch ? rangeEnd < total : nextCursor != null;

  const goNext = () => {
    if (!hasNext || loading) return;
    setPageState((s) => {
      if (s.search) return { ...s, pageIdx: s.pageIdx + 1 };
      if (!nextCursor) return s;
      const cursors = s.cursors.slice(0, s.pageIdx + 1);
      cursors.push(nextCursor);
      return { ...s, pageIdx: s.pageIdx + 1, cursors };
    });
  };
  const goPrev = () => {
    if (!hasPrev || loading) return;
    setPageState((s) => ({ ...s, pageIdx: Math.max(0, s.pageIdx - 1) }));
  };

  /* ── Render ─────────────────────────────────────────────────────────── */

  const trimmedLen = query.trim().length;
  const tooShort = trimmedLen > 0 && trimmedLen < MIN_SEARCH_LEN;

  const totalLabel = totalKind === "estimated"
    ? `≈ ${total.toLocaleString()} contacts`
    : totalKind === "capped"
      ? `${total.toLocaleString()}+ matches`
      : `${total.toLocaleString()} ${total === 1 ? "match" : "matches"}`;

  if (!loadedOnce) {
    return (
      <main className="flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0 flex items-center justify-center">
        <div className="flex items-center gap-3 text-zinc-400">
          <Spinner className="w-4 h-4" />
          Loading contacts...
        </div>
      </main>
    );
  }

  return (
    // While the drawer is open, pad the content out of the covered half so
    // every row stays fully visible and clickable for one-by-one browsing.
    <main className={`flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0 transition-[padding] duration-200 ${selectedId ? "pr-[max(50vw,600px)]" : ""}`}>
      <div className="px-6 py-5 max-w-[1400px] mx-auto">

        {/* ── Header ────────────────────────────────────────────────── */}
        <div className="mb-5">
          <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Contacts</h1>
          <div className="mt-1 text-sm text-zinc-500">{totalLabel}</div>
        </div>

        {/* ── Search ────────────────────────────────────────────────── */}
        <div className="mb-4 flex items-center gap-3">
          <div className="relative flex-1 max-w-md">
            <svg
              className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-zinc-400 pointer-events-none"
              viewBox="0 0 16 16" fill="none"
            >
              <circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.5" />
              <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            <input
              type="text"
              autoFocus
              placeholder="Search email, name, company, bucket, lead list…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="w-full bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700/60 rounded-lg pl-8 pr-8 py-2 text-sm text-zinc-800 dark:text-zinc-200 shadow-sm focus:outline-none focus:ring-1 focus:ring-violet-500"
            />
            {query && (
              <button
                onClick={() => setQuery("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300"
                aria-label="Clear search"
              >
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                  <path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              </button>
            )}
          </div>
          {tooShort && (
            <span className="text-xs text-zinc-400">Type at least {MIN_SEARCH_LEN} characters to search</span>
          )}
          {loading && <Spinner className="w-3.5 h-3.5 text-zinc-400" />}
        </div>

        {/* ── Table ─────────────────────────────────────────────────── */}
        {error ? (
          <div className="rounded-xl border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900 px-4 py-12 text-center">
            <p className="text-sm text-rose-600 dark:text-rose-400">{error}</p>
            <button
              onClick={() => setPageState((s) => ({ ...s }))}
              className="mt-3 px-3 py-1.5 text-xs font-semibold rounded-lg bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-300 border border-zinc-200 dark:border-zinc-700"
            >
              Retry
            </button>
          </div>
        ) : (
          <div className={`rounded-xl border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900 overflow-hidden shadow-sm transition-opacity ${loading ? "opacity-60" : ""}`}>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-zinc-50 dark:bg-zinc-800/30 border-b border-zinc-200 dark:border-zinc-800/40">
                    <Th>Email</Th>
                    <Th>Name</Th>
                    <Th>Company</Th>
                    <Th>Bucket / Lead list</Th>
                    <Th>Country</Th>
                    <Th>Employees</Th>
                    <Th>Invites</Th>
                    <Th>Status</Th>
                  </tr>
                </thead>
                <tbody>
                  {rows.length === 0 ? (
                    <tr>
                      <td colSpan={8} className="px-3 py-12 text-center text-zinc-400 text-sm">
                        {isSearch ? "No contacts match this search" : "No contacts yet"}
                      </td>
                    </tr>
                  ) : (
                    rows.map((c) => (
                      <tr
                        key={c.id}
                        onClick={() => setSelectedId(c.id)}
                        className={`border-b border-zinc-100 dark:border-zinc-800/30 transition-colors cursor-pointer ${
                          selectedId === c.id
                            ? "bg-violet-50/60 dark:bg-violet-500/10"
                            : "hover:bg-zinc-50 dark:hover:bg-zinc-800/20"
                        }`}
                      >
                        <td className="px-3 py-2.5 font-mono text-xs text-zinc-800 dark:text-zinc-200 max-w-[260px] truncate" title={c.email ?? undefined}>
                          {c.email || "—"}
                        </td>
                        <td className="px-3 py-2.5 text-xs text-zinc-600 dark:text-zinc-400 max-w-[160px] truncate">
                          {[c.first_name, c.last_name].filter(Boolean).join(" ") || "—"}
                        </td>
                        <td className="px-3 py-2.5 text-xs text-zinc-600 dark:text-zinc-400 max-w-[180px] truncate" title={c.company_website ?? undefined}>
                          {displayWebsite(c.company_website) || "—"}
                        </td>
                        <td className="px-3 py-2.5 max-w-[220px]">
                          <div className="text-xs text-zinc-700 dark:text-zinc-300 truncate" title={c.bucket_name ?? undefined}>
                            {c.bucket_name || "—"}
                          </div>
                          {c.lead_list_name && (
                            <div className="text-[11px] text-zinc-400 truncate" title={c.lead_list_name}>
                              {c.lead_list_name}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2.5 text-xs text-zinc-600 dark:text-zinc-400 whitespace-nowrap">
                          {c.country || "—"}
                        </td>
                        <td className="px-3 py-2.5 text-xs text-zinc-600 dark:text-zinc-400 whitespace-nowrap">
                          {c.employee_range || "—"}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          <span className="text-xs text-zinc-700 dark:text-zinc-300 font-semibold">{c.times_invited}×</span>
                          {c.last_invited_at && (
                            <span className="text-[11px] text-zinc-400 ml-1.5">{fmtDate(c.last_invited_at)}</span>
                          )}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          <span className="inline-flex items-center gap-1.5">
                            <StatusBadge status={c.outreach_status} />
                            {c.is_blocklisted && <BlocklistedBadge />}
                          </span>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ── Pagination ────────────────────────────────────────────── */}
        {!error && rows.length > 0 && (
          <div className="mt-3 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <PageButton onClick={goPrev} disabled={!hasPrev || loading}>← Prev</PageButton>
              <PageButton onClick={goNext} disabled={!hasNext || loading}>Next →</PageButton>
            </div>
            <div className="text-xs text-zinc-400">
              Showing {rangeStart.toLocaleString()}–{rangeEnd.toLocaleString()} of {totalLabel}
            </div>
          </div>
        )}

      </div>

      <ContactDetailDrawer contactId={selectedId} onClose={() => setSelectedId(null)} />
    </main>
  );
}

/* ── Small building blocks ─────────────────────────────────────────────── */

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="text-left px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium whitespace-nowrap">
      {children}
    </th>
  );
}

function PageButton({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-300 border border-zinc-200 dark:border-zinc-700 disabled:opacity-40 disabled:cursor-not-allowed"
    >
      {children}
    </button>
  );
}

function Spinner({ className }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className ?? ""}`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

function displayWebsite(site: string | null): string | null {
  if (!site) return null;
  return site.replace(/^https?:\/\//i, "").replace(/^www\./i, "").replace(/\/$/, "") || null;
}
