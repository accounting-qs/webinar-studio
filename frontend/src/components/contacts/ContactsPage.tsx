"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import {
  downloadAssignmentGroupContactsCsv,
  fetchAssignmentContacts,
  fetchAssignmentGroupContacts,
  markContactsUsed,
  markGroupContactsUsed,
  releaseContactsById,
  type ApiContact,
} from "@/lib/api";

const GROUP_PAGE_SIZE = 1000;

/* ─── Types ───────────────────────────────────────────────────────────────── */

type StatusFilter = "assigned" | "used" | "all";

type NormalizedHeader = {
  title: string;
  webinarNumber: number | null;
  webinarDate: string | null;
  volume: number;
  fileBase: string;
};

type NormalizedData = {
  header: NormalizedHeader;
  contacts: ApiContact[];
  counts: { assigned: number; used: number; total: number };
  // For group mode: how many rows match the current filter on the server, and
  // whether we've loaded all of them locally yet. Single mode leaves filteredTotal
  // equal to contacts.length and hasMore false.
  filteredTotal: number;
  hasMore: boolean;
};

type ContactsPageProps =
  | { assignmentId: string; groupAssignmentIds?: undefined; initialTab?: StatusFilter }
  | { assignmentId?: undefined; groupAssignmentIds: string[]; initialTab?: StatusFilter };

/* ─── Main Component ──────────────────────────────────────────────────────── */

export function ContactsPage(props: ContactsPageProps) {
  const { initialTab = "assigned" } = props;
  // Stable key for the group/assignment so useCallback deps don't see a new
  // array identity on every render of the parent.
  const groupKey = useMemo(
    () => (props.groupAssignmentIds ? props.groupAssignmentIds.join(",") : ""),
    [props.groupAssignmentIds],
  );
  const isGroup = !!props.groupAssignmentIds;

  const [data, setData] = useState<NormalizedData | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [downloadingCsv, setDownloadingCsv] = useState(false);
  const [filter, setFilter] = useState<StatusFilter>(initialTab);
  const [selectCount, setSelectCount] = useState<number>(0);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [copying, setCopying] = useState(false);
  const [copiedBtn, setCopiedBtn] = useState<"emails" | "names" | null>(null);
  const [marking, setMarking] = useState(false);
  const [releasing, setReleasing] = useState(false);

  /* ── Fetch contacts ─────────────────────────────────────────────────── */

  const load = useCallback(async (status: StatusFilter) => {
    try {
      if (isGroup) {
        const ids = groupKey ? groupKey.split(",") : [];
        const r = await fetchAssignmentGroupContacts(ids, status, { limit: GROUP_PAGE_SIZE, offset: 0 });
        const title = r.group.bucket_name
          ? (r.group.webinar_number != null
            ? `W${r.group.webinar_number} — ${r.group.bucket_name}`
            : r.group.bucket_name)
          : `${r.group.list_count} lists combined`;
        const fileBase = (r.group.bucket_name || `group_${r.group.list_count}_lists`)
          .replace(/[^a-zA-Z0-9_-]/g, "_");
        setData({
          header: {
            title,
            webinarNumber: r.group.webinar_number,
            webinarDate: r.group.webinar_date,
            volume: r.group.volume,
            fileBase,
          },
          contacts: r.contacts,
          counts: r.counts,
          filteredTotal: r.pagination.filtered_total,
          hasMore: r.contacts.length < r.pagination.filtered_total,
        });
      } else {
        const r = await fetchAssignmentContacts(props.assignmentId!, status);
        const title = r.assignment.list_name
          || (r.assignment.bucket_name
            ? `W${r.assignment.webinar_number} — ${r.assignment.bucket_name}`
            : `Assignment`);
        const fileBase = (r.assignment.list_name || r.assignment.bucket_name || "contacts")
          .replace(/[^a-zA-Z0-9_-]/g, "_");
        setData({
          header: {
            title,
            webinarNumber: r.assignment.webinar_number,
            webinarDate: r.assignment.webinar_date,
            volume: r.assignment.volume,
            fileBase,
          },
          contacts: r.contacts,
          counts: r.counts,
          filteredTotal: r.contacts.length,
          hasMore: false,
        });
      }
    } catch (err) {
      console.error("Failed to load contacts:", err);
    } finally {
      setLoading(false);
    }
  }, [isGroup, groupKey, props.assignmentId]);

  const loadMore = useCallback(async () => {
    if (!isGroup || !data || !data.hasMore || loadingMore) return;
    setLoadingMore(true);
    try {
      const ids = groupKey ? groupKey.split(",") : [];
      const r = await fetchAssignmentGroupContacts(ids, filter, {
        limit: GROUP_PAGE_SIZE,
        offset: data.contacts.length,
      });
      setData((prev) => prev ? ({
        ...prev,
        contacts: [...prev.contacts, ...r.contacts],
        counts: r.counts,
        filteredTotal: r.pagination.filtered_total,
        hasMore: prev.contacts.length + r.contacts.length < r.pagination.filtered_total,
      }) : prev);
    } catch (err) {
      console.error("Failed to load more contacts:", err);
    } finally {
      setLoadingMore(false);
    }
  }, [isGroup, groupKey, filter, data, loadingMore]);

  const downloadAllCsv = useCallback(async () => {
    if (!isGroup || !groupKey || downloadingCsv) return;
    setDownloadingCsv(true);
    try {
      const ids = groupKey.split(",");
      const { blob, filename } = await downloadAssignmentGroupContactsCsv(ids, filter);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Failed to download CSV:", err);
    } finally {
      setDownloadingCsv(false);
    }
  }, [isGroup, groupKey, filter, downloadingCsv]);

  useEffect(() => {
    setLoading(true);
    setSelectedIds(new Set());
    setSelectCount(0);
    load(filter);
  }, [filter, load]);

  /* ── Select N contacts ──────────────────────────────────────────────── */

  const applySelectCount = useCallback((n: number) => {
    if (!data) return;
    const ids = new Set<string>();
    const available = data.contacts.filter((c) => c.outreach_status === "assigned");
    const pool = filter === "assigned" ? available : data.contacts;
    for (let i = 0; i < Math.min(n, pool.length); i++) {
      ids.add(pool[i].id);
    }
    setSelectedIds(ids);
  }, [data, filter]);

  /* ── Copy to clipboard helper ────────────────────────────────────────── */

  const copyText = useCallback(async (text: string, btn: "emails" | "names") => {
    setCopying(true);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopiedBtn(btn);
    setTimeout(() => setCopiedBtn((prev) => (prev === btn ? null : prev)), 2000);
    setCopying(false);
  }, []);

  const copyEmails = useCallback(() => {
    if (!data || selectedIds.size === 0) return;
    const text = data.contacts.filter((c) => selectedIds.has(c.id)).map((c) => c.email).join("\n");
    copyText(text, "emails");
  }, [data, selectedIds, copyText]);

  const copyEmailsAndNames = useCallback(() => {
    if (!data || selectedIds.size === 0) return;
    const text = data.contacts.filter((c) => selectedIds.has(c.id)).map((c) => `${c.email}\t${c.first_name || ""}`).join("\n");
    copyText(text, "names");
  }, [data, selectedIds, copyText]);

  /* ── Mark selected as used ──────────────────────────────────────────── */

  const handleMarkUsed = useCallback(async () => {
    if (selectedIds.size === 0) return;
    setMarking(true);
    try {
      if (isGroup) {
        await markGroupContactsUsed(Array.from(selectedIds));
      } else {
        await markContactsUsed(props.assignmentId!, Array.from(selectedIds));
      }
      // Reload to get fresh counts and filter out used ones
      setSelectedIds(new Set());
      setSelectCount(0);
      await load(filter);
    } catch (err) {
      console.error("Failed to mark contacts:", err);
    } finally {
      setMarking(false);
    }
  }, [isGroup, props.assignmentId, selectedIds, filter, load]);

  /* ── Release selected contacts back to the bucket ────────────────────── */

  const handleRelease = useCallback(async () => {
    if (selectedIds.size === 0 || releasing) return;
    const n = selectedIds.size;
    if (!confirm(`Release ${n.toLocaleString()} contact${n === 1 ? "" : "s"}? They'll be returned to the bucket as available and can be re-assigned to future webinars.`)) {
      return;
    }
    setReleasing(true);
    try {
      // Scope guard: the server will refuse any contact_id whose current
      // assignment_id isn't in this list. Single-assignment page → one id;
      // group page → the group's full id list.
      const scopeAssignmentIds = isGroup
        ? (groupKey ? groupKey.split(",") : [])
        : [props.assignmentId!];
      const result = await releaseContactsById(Array.from(selectedIds), scopeAssignmentIds);
      setSelectedIds(new Set());
      setSelectCount(0);
      await load(filter);
      const skippedNotFound = result.not_found.length;
      const skippedAvailable = result.already_available.length;
      const skippedOutOfScope = result.out_of_scope?.length ?? 0;
      if (skippedNotFound > 0 || skippedAvailable > 0 || skippedOutOfScope > 0) {
        console.warn("Release skipped some:", {
          not_found: skippedNotFound,
          already_available: skippedAvailable,
          out_of_scope: skippedOutOfScope,
        });
        if (skippedOutOfScope > 0) {
          alert(`Released ${result.released.toLocaleString()} contact${result.released === 1 ? "" : "s"}. ${skippedOutOfScope.toLocaleString()} were skipped because they weren't in the visible scope (likely a UI bug — please report).`);
        }
      }
    } catch (err) {
      console.error("Failed to release contacts:", err);
      alert(err instanceof Error ? err.message : "Failed to release contacts");
    } finally {
      setReleasing(false);
    }
  }, [selectedIds, releasing, filter, load, isGroup, groupKey, props.assignmentId]);

  /* ── Export selected contacts as CSV ─────────────────────────────────── */

  const exportCsv = useCallback(() => {
    if (!data || selectedIds.size === 0) return;
    const selected = data.contacts.filter((c) => selectedIds.has(c.id));
    const rows = [["email", "first_name"]];
    for (const c of selected) {
      rows.push([c.email, c.first_name || ""]);
    }
    const csv = rows.map((r) => r.map((v) => `"${v.replace(/"/g, '""')}"`).join(",")).join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${data.header.fileBase}_${filter}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [data, filter, selectedIds]);

  /* ── Toggle single row ──────────────────────────────────────────────── */

  const toggleContact = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  /* ── Render ─────────────────────────────────────────────────────────── */

  if (loading && !data) {
    return (
      <main className="flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0 flex items-center justify-center">
        <div className="flex items-center gap-3 text-zinc-400">
          <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
          Loading contacts...
        </div>
      </main>
    );
  }

  if (!data) {
    return (
      <main className="flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0 flex items-center justify-center">
        <p className="text-zinc-500">{isGroup ? "Group not found" : "Assignment not found"}</p>
      </main>
    );
  }

  const { header, contacts, counts } = data;

  const hasAssignedSelected = contacts.some(
    (c) => selectedIds.has(c.id) && c.outreach_status === "assigned"
  );

  return (
    <main className="flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0">
      <div className="px-6 py-5 max-w-[1000px] mx-auto">

        {/* ── Header ────────────────────────────────────────────────── */}
        <div className="mb-5">
          <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">
            {header.title}
          </h1>
          <div className="flex items-center gap-2 mt-1 text-sm text-zinc-500">
            {header.webinarDate && (
              <span>{new Date(header.webinarDate + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}</span>
            )}
            {header.webinarDate && <span className="text-zinc-300 dark:text-zinc-600">·</span>}
            <span>{header.volume.toLocaleString()} total contacts</span>
          </div>
        </div>

        {/* ── Status Filter ─────────────────────────────────────────── */}
        <div className="flex items-center gap-2 mb-4">
          {(["assigned", "used", "all"] as StatusFilter[]).map((s) => {
            const label = s === "all" ? "All" : s.charAt(0).toUpperCase() + s.slice(1);
            const count = s === "assigned" ? counts.assigned : s === "used" ? counts.used : counts.total;
            const isActive = filter === s;
            return (
              <button
                key={s}
                onClick={() => setFilter(s)}
                className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                  isActive
                    ? s === "assigned"
                      ? "bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400 border border-violet-200 dark:border-violet-500/25"
                      : s === "used"
                        ? "bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-500/25"
                        : "bg-zinc-200 dark:bg-zinc-700 text-zinc-800 dark:text-zinc-200 border border-zinc-300 dark:border-zinc-600"
                    : "text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800/50 border border-transparent"
                }`}
              >
                {label} ({count})
              </button>
            );
          })}
        </div>

        {/* ── Action Bar ────────────────────────────────────────────── */}
        <div className="flex items-center gap-3 mb-4 bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800/40 rounded-xl px-4 py-3 shadow-sm">
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium">Select</label>
            <input
              type="number"
              min={0}
              max={contacts.length}
              value={selectCount}
              onChange={(e) => {
                const v = Math.max(0, Math.min(contacts.length, parseInt(e.target.value) || 0));
                setSelectCount(v);
                applySelectCount(v);
              }}
              className="w-20 bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2 py-1.5 text-sm text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500"
            />
            <span className="text-xs text-zinc-400">of {contacts.length}</span>
            <button
              onClick={() => { setSelectCount(Math.min(300, contacts.length)); applySelectCount(Math.min(300, contacts.length)); }}
              className="px-2.5 py-1.5 text-xs font-semibold rounded-md bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400 border border-violet-200 dark:border-violet-500/25 hover:bg-violet-200 dark:hover:bg-violet-500/25 transition-colors"
            >
              300
            </button>
          </div>

          <div className="w-px h-5 bg-zinc-200 dark:bg-zinc-700" />

          <span className="text-sm font-semibold text-zinc-700 dark:text-zinc-300">
            {selectedIds.size} selected
          </span>

          <div className="flex-1" />

          <button
            onClick={copyEmails}
            disabled={selectedIds.size === 0 || copying}
            className={`px-4 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 ${
              copiedBtn === "emails"
                ? "bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-500/25"
                : "bg-zinc-900 dark:bg-zinc-100 hover:bg-zinc-800 dark:hover:bg-zinc-200 text-white dark:text-zinc-900 disabled:opacity-40 disabled:cursor-not-allowed"
            }`}
          >
            {copiedBtn === "emails" ? "Copied!" : "Copy Emails"}
          </button>

          <button
            onClick={copyEmailsAndNames}
            disabled={selectedIds.size === 0 || copying}
            className={`px-4 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 ${
              copiedBtn === "names"
                ? "bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-500/25"
                : "bg-zinc-900 dark:bg-zinc-100 hover:bg-zinc-800 dark:hover:bg-zinc-200 text-white dark:text-zinc-900 disabled:opacity-40 disabled:cursor-not-allowed"
            }`}
          >
            {copiedBtn === "names" ? "Copied!" : "Copy Emails + Names"}
          </button>

          <button
            onClick={exportCsv}
            disabled={selectedIds.size === 0}
            className="px-4 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-300 border border-zinc-200 dark:border-zinc-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Export CSV
          </button>

          {isGroup && (
            <button
              onClick={downloadAllCsv}
              disabled={downloadingCsv || data.filteredTotal === 0}
              className="px-4 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 disabled:cursor-wait text-white"
              title={`Download all ${data.filteredTotal.toLocaleString()} contacts as CSV`}
            >
              {downloadingCsv && (
                <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
              )}
              Download All ({data.filteredTotal.toLocaleString()})
            </button>
          )}

          {hasAssignedSelected && (
            <button
              onClick={handleMarkUsed}
              disabled={marking}
              className="px-4 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 disabled:cursor-wait text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5"
            >
              {marking && (
                <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
              )}
              Mark as Used
            </button>
          )}

          {selectedIds.size > 0 && (
            <button
              onClick={handleRelease}
              disabled={releasing}
              className="px-4 py-1.5 bg-amber-600 hover:bg-amber-500 disabled:opacity-50 disabled:cursor-wait text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5"
              title="Release selected contacts back to the bucket as available"
            >
              {releasing && (
                <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
              )}
              Release
            </button>
          )}
        </div>

        {/* ── Table ─────────────────────────────────────────────────── */}
        <div className="rounded-xl border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900 overflow-hidden shadow-sm">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-zinc-50 dark:bg-zinc-800/30 border-b border-zinc-200 dark:border-zinc-800/40">
                <th className="w-10 px-3 py-2.5">
                  <input
                    type="checkbox"
                    checked={selectedIds.size > 0 && selectedIds.size === contacts.length}
                    onChange={() => {
                      if (selectedIds.size === contacts.length) {
                        setSelectedIds(new Set());
                        setSelectCount(0);
                      } else {
                        setSelectedIds(new Set(contacts.map((c) => c.id)));
                        setSelectCount(contacts.length);
                      }
                    }}
                    className="w-3.5 h-3.5 rounded border-zinc-300 dark:border-zinc-600 text-violet-600 focus:ring-violet-500 cursor-pointer accent-violet-600"
                  />
                </th>
                <th className="text-left px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium">Email</th>
                <th className="text-left px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium w-[180px]">First Name</th>
                {filter !== "assigned" && (
                  <th className="text-left px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium w-[120px]">Status</th>
                )}
              </tr>
            </thead>
            <tbody>
              {contacts.length === 0 ? (
                <tr>
                  <td colSpan={filter !== "assigned" ? 4 : 3} className="px-3 py-12 text-center text-zinc-400 text-sm">
                    No {filter === "all" ? "" : filter} contacts found
                  </td>
                </tr>
              ) : (
                contacts.map((c) => {
                  const isSelected = selectedIds.has(c.id);
                  return (
                    <tr
                      key={c.id}
                      onClick={() => toggleContact(c.id)}
                      className={`border-b border-zinc-100 dark:border-zinc-800/30 transition-colors cursor-pointer ${
                        isSelected ? "bg-violet-50/50 dark:bg-violet-500/5" : "hover:bg-zinc-50 dark:hover:bg-zinc-800/20"
                      }`}
                    >
                      <td className="px-3 py-2.5">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggleContact(c.id)}
                          onClick={(e) => e.stopPropagation()}
                          className="w-3.5 h-3.5 rounded border-zinc-300 dark:border-zinc-600 text-violet-600 focus:ring-violet-500 cursor-pointer accent-violet-600"
                        />
                      </td>
                      <td className="px-3 py-2.5 font-mono text-xs text-zinc-800 dark:text-zinc-200">
                        {c.email}
                      </td>
                      <td className="px-3 py-2.5 text-zinc-600 dark:text-zinc-400 text-xs">
                        {c.first_name || "—"}
                      </td>
                      {filter !== "assigned" && (
                        <td className="px-3 py-2.5">
                          {c.outreach_status === "used" ? (
                            <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-500/20">
                              Used
                              {c.used_at && (
                                <span className="font-normal normal-case ml-1 text-emerald-500/70">
                                  {new Date(c.used_at).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                                </span>
                              )}
                            </span>
                          ) : (
                            <span className="text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400 border border-violet-200 dark:border-violet-500/20">
                              Assigned
                            </span>
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {/* ── Footer count + Load more ─────────────────────────────── */}
        <div className="mt-3 flex items-center justify-between gap-3">
          {data.hasMore ? (
            <button
              onClick={loadMore}
              disabled={loadingMore}
              className="px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5 bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-300 border border-zinc-200 dark:border-zinc-700 disabled:opacity-50 disabled:cursor-wait"
            >
              {loadingMore && (
                <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
              )}
              Load next {Math.min(GROUP_PAGE_SIZE, data.filteredTotal - contacts.length).toLocaleString()}
            </button>
          ) : <div />}
          <div className="text-xs text-zinc-400">
            Showing {contacts.length.toLocaleString()} of {data.filteredTotal.toLocaleString()} contact{data.filteredTotal !== 1 ? "s" : ""}
          </div>
        </div>

      </div>
    </main>
  );
}
