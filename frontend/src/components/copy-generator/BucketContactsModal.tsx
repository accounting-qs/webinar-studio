"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchBucketContacts,
  downloadBucketContactsCsv,
  type BucketContact,
  type BucketContactsScope,
} from "@/lib/api";

const PAGE_SIZE = 1000;

export type BucketContactsTarget = {
  bucketId: string;
  bucketName: string;
  scope: BucketContactsScope;
};

function fullName(c: BucketContact): string {
  return [c.first_name, c.last_name].filter(Boolean).join(" ").trim();
}

/** Read-only contact list for a bucket (Total or Remaining), with copy + CSV
 * export. Paginates so it stays responsive on tens of thousands of contacts. */
export function BucketContactsModal({
  target,
  onClose,
}: {
  target: BucketContactsTarget;
  onClose: () => void;
}) {
  const [contacts, setContacts] = useState<BucketContact[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [downloading, setDownloading] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const backdropRef = useRef<HTMLDivElement>(null);

  const hasMore = contacts.length < total;

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", handler); document.body.style.overflow = prev; };
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const r = await fetchBucketContacts(target.bucketId, target.scope, { limit: PAGE_SIZE, offset: 0 });
        if (cancelled) return;
        setContacts(r.contacts);
        setTotal(r.pagination.filtered_total);
      } catch {
        if (!cancelled) setError("Failed to load contacts.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [target.bucketId, target.scope]);

  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    try {
      const r = await fetchBucketContacts(target.bucketId, target.scope, { limit: PAGE_SIZE, offset: contacts.length });
      setContacts((prev) => [...prev, ...r.contacts]);
      setTotal(r.pagination.filtered_total);
    } catch {
      setError("Failed to load more contacts.");
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, hasMore, target.bucketId, target.scope, contacts.length]);

  // Copy acts on the selection, or every loaded contact when nothing is selected.
  const effectiveRows = selected.size > 0 ? contacts.filter((c) => selected.has(c.id)) : contacts;

  const flash = (key: string) => { setCopied(key); window.setTimeout(() => setCopied(null), 1500); };

  const copyEmails = async () => {
    const text = effectiveRows.map((c) => c.email || "").filter(Boolean).join("\n");
    await navigator.clipboard.writeText(text);
    flash("emails");
  };

  const copyNamesAndEmails = async () => {
    const text = effectiveRows.map((c) => `${fullName(c)}\t${c.email || ""}`).join("\n");
    await navigator.clipboard.writeText(text);
    flash("names");
  };

  const downloadCsv = async () => {
    if (downloading) return;
    setDownloading(true);
    try {
      const { blob, filename } = await downloadBucketContactsCsv(target.bucketId, target.scope);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      setError("Failed to export CSV.");
    } finally {
      setDownloading(false);
    }
  };

  const allLoadedSelected = contacts.length > 0 && contacts.every((c) => selected.has(c.id));
  const toggleAll = () => {
    setSelected((prev) => {
      if (contacts.every((c) => prev.has(c.id)) && contacts.length > 0) return new Set();
      return new Set(contacts.map((c) => c.id));
    });
  };
  const toggleOne = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const scopeLabel = target.scope === "remaining" ? "Remaining contacts" : "All contacts";
  const copyCountLabel = selected.size > 0 ? selected.size : contacts.length;

  return (
    <div
      ref={backdropRef}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8"
      onClick={(e) => { if (e.target === backdropRef.current) onClose(); }}
    >
      <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-2xl shadow-2xl w-[80vw] max-w-3xl max-h-[85vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-start justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800/60 shrink-0">
          <div className="min-w-0">
            <div className="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold mb-0.5">
              Bucket · {scopeLabel}
            </div>
            <h2 className="text-base font-bold text-zinc-900 dark:text-zinc-100 truncate">{target.bucketName}</h2>
            <p className="text-[11px] text-zinc-500 mt-0.5">
              {loading ? "Loading…" : `${total.toLocaleString()} contact${total === 1 ? "" : "s"}`}
              {!loading && hasMore && ` · ${contacts.length.toLocaleString()} loaded`}
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors"
            aria-label="Close"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        {/* Toolbar */}
        <div className="flex items-center flex-wrap gap-2 px-6 py-3 border-b border-zinc-200 dark:border-zinc-800/40 shrink-0">
          <span className="text-[11px] text-zinc-500">
            {selected.size > 0 ? `${selected.size.toLocaleString()} selected` : "Acting on all loaded"}
          </span>
          <div className="flex-1" />
          <button
            onClick={copyEmails}
            disabled={contacts.length === 0}
            className="px-3 py-1.5 text-xs font-semibold rounded-lg border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-40 transition-colors"
          >
            {copied === "emails" ? "Copied ✓" : `Copy emails (${copyCountLabel.toLocaleString()})`}
          </button>
          <button
            onClick={copyNamesAndEmails}
            disabled={contacts.length === 0}
            className="px-3 py-1.5 text-xs font-semibold rounded-lg border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-40 transition-colors"
          >
            {copied === "names" ? "Copied ✓" : "Copy name + email"}
          </button>
          <button
            onClick={downloadCsv}
            disabled={downloading || total === 0}
            className="px-3 py-1.5 text-xs font-semibold rounded-lg bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white transition-colors inline-flex items-center gap-1.5"
            title={`Download all ${total.toLocaleString()} contacts as CSV`}
          >
            {downloading && <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>}
            {downloading ? "Exporting…" : `Download CSV (${total.toLocaleString()})`}
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="flex items-center gap-3 py-12 justify-center">
              <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
              <span className="text-sm text-zinc-500">Loading contacts…</span>
            </div>
          ) : error ? (
            <div className="m-6 rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-400">{error}</div>
          ) : contacts.length === 0 ? (
            <div className="m-6 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-500">No contacts found.</div>
          ) : (
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-zinc-50 dark:bg-zinc-900/95 backdrop-blur z-10">
                <tr className="border-b border-zinc-200 dark:border-zinc-800/40 text-left text-[10px] uppercase tracking-wider text-zinc-500">
                  <th className="w-10 px-3 py-2">
                    <input type="checkbox" checked={allLoadedSelected} onChange={toggleAll} className="accent-violet-600" />
                  </th>
                  <th className="px-3 py-2 font-semibold">Name</th>
                  <th className="px-3 py-2 font-semibold">Email</th>
                </tr>
              </thead>
              <tbody>
                {contacts.map((c) => (
                  <tr key={c.id} className="border-b border-zinc-100 dark:border-zinc-800/30 hover:bg-zinc-50 dark:hover:bg-zinc-800/20">
                    <td className="px-3 py-1.5">
                      <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggleOne(c.id)} className="accent-violet-600" />
                    </td>
                    <td className="px-3 py-1.5 text-zinc-800 dark:text-zinc-200">{fullName(c) || <span className="text-zinc-400">—</span>}</td>
                    <td className="px-3 py-1.5 text-zinc-600 dark:text-zinc-400 font-mono text-xs">{c.email || <span className="text-zinc-400">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {!loading && hasMore && (
            <div className="flex justify-center py-4">
              <button
                onClick={loadMore}
                disabled={loadingMore}
                className="px-4 py-1.5 text-xs font-semibold rounded-lg border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-50 transition-colors"
              >
                {loadingMore ? "Loading…" : `Load more (${(total - contacts.length).toLocaleString()} left)`}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
