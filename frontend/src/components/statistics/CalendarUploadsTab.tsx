"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelCalendarImport,
  confirmCalendarUpload,
  deleteCalendarUpload,
  fetchCalendarUploads,
  fetchSenders,
  fetchWebinars,
  pauseCalendarImport,
  presignCalendarUpload,
  resumeCalendarImport,
  startCalendarImport,
  uploadToStorage,
  type ApiCalendarUpload,
  type ApiSender,
  type ApiWebinar,
  type CalendarConfirmResponse,
} from "@/lib/api";

const POLL_MS = 3000;

const TERMINAL_STATUSES = new Set(["complete", "failed", "cancelled"]);

function webinarLabel(w: ApiWebinar): string {
  const base = `E${w.number}`;
  const variant = w.variant_label ? ` — ${w.variant_label}` : "";
  const date = w.date ? ` · ${new Date(w.date).toLocaleDateString()}` : "";
  return `${base}${variant}${date}`;
}

function statusBadge(status: string): { className: string; label: string } {
  switch (status) {
    case "complete":
      return { className: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30", label: "Complete" };
    case "failed":
      return { className: "bg-red-500/15 text-red-400 border-red-500/30", label: "Failed" };
    case "cancelled":
      return { className: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30", label: "Cancelled" };
    case "paused":
      return { className: "bg-amber-500/15 text-amber-400 border-amber-500/30", label: "Paused" };
    case "processing":
      return { className: "bg-violet-500/15 text-violet-400 border-violet-500/30", label: "Processing" };
    case "uploading":
      return { className: "bg-sky-500/15 text-sky-400 border-sky-500/30", label: "Uploading" };
    default:
      return { className: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30", label: status };
  }
}

export function CalendarUploadsTab() {
  const [uploads, setUploads] = useState<ApiCalendarUpload[]>([]);
  const [webinars, setWebinars] = useState<ApiWebinar[]>([]);
  const [senders, setSenders] = useState<ApiSender[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  const loadAll = useCallback(async () => {
    setLoadError(null);
    try {
      const [u, w, s] = await Promise.all([
        fetchCalendarUploads(),
        fetchWebinars(),
        fetchSenders(),
      ]);
      setUploads(u.uploads);
      setWebinars(w.webinars);
      setSenders(s.senders);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // Poll while anything is in-flight
  useEffect(() => {
    const inFlight = uploads.some((u) => !TERMINAL_STATUSES.has(u.status));
    if (!inFlight) return;
    const t = setInterval(() => {
      fetchCalendarUploads().then((r) => setUploads(r.uploads)).catch(() => {});
    }, POLL_MS);
    return () => clearInterval(t);
  }, [uploads]);

  return (
    <div className="px-6 py-5">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">Calendar Uploads</h2>
          <p className="text-xs text-zinc-500 mt-0.5">
            Upload Added-to-Calendar CSVs per webinar. Re-uploading the same email for the same webinar updates the row.
          </p>
        </div>
        <button
          onClick={() => setModalOpen(true)}
          disabled={webinars.length === 0}
          className="px-3 py-1.5 text-xs rounded-lg bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Upload CSV
        </button>
      </div>

      {loadError && (
        <div className="mb-3 px-3 py-2 rounded-md bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
          {loadError}
        </div>
      )}

      {loading ? (
        <div className="text-xs text-zinc-500">Loading…</div>
      ) : uploads.length === 0 ? (
        <div className="text-xs text-zinc-500 py-8 text-center border border-dashed border-zinc-300 dark:border-zinc-800 rounded-lg">
          No calendar uploads yet. Click <span className="font-semibold">Upload CSV</span> to add one.
        </div>
      ) : (
        <UploadsTable uploads={uploads} onChanged={loadAll} />
      )}

      {modalOpen && (
        <UploadModal
          webinars={webinars}
          senders={senders}
          onClose={() => setModalOpen(false)}
          onCreated={() => {
            setModalOpen(false);
            loadAll();
          }}
        />
      )}
    </div>
  );
}

/* ── Uploads Table ─────────────────────────────────────────────────────── */

function UploadsTable({
  uploads,
  onChanged,
}: {
  uploads: ApiCalendarUpload[];
  onChanged: () => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);

  const handlePause = async (id: string) => {
    setBusyId(id);
    try { await pauseCalendarImport(id); onChanged(); } finally { setBusyId(null); }
  };
  const handleResume = async (id: string) => {
    setBusyId(id);
    try { await resumeCalendarImport(id); onChanged(); } finally { setBusyId(null); }
  };
  const handleCancel = async (id: string) => {
    setBusyId(id);
    try { await cancelCalendarImport(id); onChanged(); } finally { setBusyId(null); }
  };
  const handleDelete = async (id: string) => {
    if (!confirm("Delete this upload and all its calendar invite rows?")) return;
    setBusyId(id);
    try { await deleteCalendarUpload(id); onChanged(); } finally { setBusyId(null); }
  };

  return (
    <div className="border border-zinc-200 dark:border-zinc-800 rounded-lg overflow-hidden">
      <table className="w-full text-xs">
        <thead className="bg-zinc-50 dark:bg-zinc-900 text-[10px] uppercase tracking-wider text-zinc-500">
          <tr>
            <th className="text-left px-3 py-2 font-semibold">Webinar</th>
            <th className="text-left px-3 py-2 font-semibold">Sender</th>
            <th className="text-left px-3 py-2 font-semibold">File</th>
            <th className="text-left px-3 py-2 font-semibold">Uploaded</th>
            <th className="text-right px-3 py-2 font-semibold">Rows</th>
            <th className="text-right px-3 py-2 font-semibold">Matched</th>
            <th className="text-right px-3 py-2 font-semibold">No List Data</th>
            <th className="text-center px-3 py-2 font-semibold">Responses</th>
            <th className="text-left px-3 py-2 font-semibold">Status</th>
            <th className="text-right px-3 py-2 font-semibold">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {uploads.map((u) => {
            const badge = statusBadge(u.status);
            const inFlight = !TERMINAL_STATUSES.has(u.status);
            const dateLabel = u.created_at ? new Date(u.created_at).toLocaleString() : "—";
            return (
              <tr key={u.id} className="bg-white dark:bg-zinc-950 hover:bg-zinc-50 dark:hover:bg-zinc-900/60">
                <td className="px-3 py-2 font-mono text-zinc-800 dark:text-zinc-200">{u.webinar_label ?? u.webinar_id.slice(0, 8)}</td>
                <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">
                  {u.sender_name ?? <span className="text-zinc-500">—</span>}
                </td>
                <td className="px-3 py-2 max-w-[260px] truncate text-zinc-700 dark:text-zinc-300" title={u.file_name}>{u.file_name}</td>
                <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">{dateLabel}</td>
                <td className="px-3 py-2 text-right font-mono">{u.total_rows.toLocaleString()}</td>
                <td className="px-3 py-2 text-right font-mono text-emerald-500">{u.matched_count.toLocaleString()}</td>
                <td className="px-3 py-2 text-right font-mono text-amber-500">{u.unmatched_count.toLocaleString()}</td>
                <td className="px-3 py-2 text-center">
                  {u.has_responses ? <span className="text-emerald-500">✓</span> : <span className="text-zinc-500">—</span>}
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-col gap-1">
                    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold border ${badge.className} w-fit`}>
                      {badge.label}
                    </span>
                    {inFlight && (
                      <div className="w-32 h-1 bg-zinc-200 dark:bg-zinc-800 rounded overflow-hidden">
                        <div
                          className="h-full bg-violet-500 transition-all"
                          style={{ width: `${u.progress}%` }}
                        />
                      </div>
                    )}
                    {u.status === "failed" && u.error_message && (
                      <span className="text-[10px] text-red-400 max-w-[260px] truncate" title={u.error_message}>
                        {u.error_message}
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2 text-right">
                  <div className="flex justify-end gap-1.5">
                    {u.status === "processing" && (
                      <button
                        onClick={() => handlePause(u.id)}
                        disabled={busyId === u.id}
                        className="px-2 py-0.5 text-[10px] rounded bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-200 disabled:opacity-50"
                      >
                        Pause
                      </button>
                    )}
                    {u.status === "paused" && (
                      <button
                        onClick={() => handleResume(u.id)}
                        disabled={busyId === u.id}
                        className="px-2 py-0.5 text-[10px] rounded bg-violet-600 hover:bg-violet-500 text-white disabled:opacity-50"
                      >
                        Resume
                      </button>
                    )}
                    {(u.status === "processing" || u.status === "paused") && (
                      <button
                        onClick={() => handleCancel(u.id)}
                        disabled={busyId === u.id}
                        className="px-2 py-0.5 text-[10px] rounded bg-amber-600 hover:bg-amber-500 text-white disabled:opacity-50"
                      >
                        Cancel
                      </button>
                    )}
                    {TERMINAL_STATUSES.has(u.status) && (
                      <button
                        onClick={() => handleDelete(u.id)}
                        disabled={busyId === u.id}
                        className="px-2 py-0.5 text-[10px] rounded bg-red-600/80 hover:bg-red-600 text-white disabled:opacity-50"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ── Upload Modal ──────────────────────────────────────────────────────── */

type ModalStep = "pick" | "uploading" | "confirmed" | "starting";

function UploadModal({
  webinars,
  senders,
  onClose,
  onCreated,
}: {
  webinars: ApiWebinar[];
  senders: ApiSender[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [webinarId, setWebinarId] = useState<string>("");
  const [senderId, setSenderId] = useState<string>("");
  const [file, setFile] = useState<File | null>(null);
  const [step, setStep] = useState<ModalStep>("pick");
  const [progress, setProgress] = useState(0);
  const [confirmed, setConfirmed] = useState<CalendarConfirmResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const sortedWebinars = useMemo(
    () => [...webinars].sort((a, b) => b.number - a.number),
    [webinars],
  );
  const sortedSenders = useMemo(
    () => [...senders].sort((a, b) => a.display_order - b.display_order || a.name.localeCompare(b.name)),
    [senders],
  );

  const handleUpload = async () => {
    if (!file || !webinarId) return;
    setError(null);
    setStep("uploading");
    setProgress(0);
    try {
      const { upload_id, signed_url } = await presignCalendarUpload(
        file.name,
        file.size,
        webinarId,
        senderId || null,
      );
      await uploadToStorage(signed_url, file, setProgress);
      const conf = await confirmCalendarUpload(upload_id, file.size);
      setConfirmed(conf);
      setStep("confirmed");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStep("pick");
    }
  };

  const handleStart = async () => {
    if (!confirmed) return;
    setError(null);
    setStep("starting");
    try {
      await startCalendarImport(confirmed.id);
      onCreated();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStep("confirmed");
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-2xl w-full max-h-[85vh] overflow-y-auto"
      >
        <div className="px-6 py-4 border-b border-zinc-200 dark:border-zinc-800 flex justify-between items-center">
          <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">Upload Added-to-Calendar CSV</h3>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-200 text-lg leading-none">×</button>
        </div>

        <div className="px-6 py-5 space-y-4">
          {error && (
            <div className="px-3 py-2 rounded-md bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
              {error}
            </div>
          )}

          {step === "pick" && (
            <>
              <div>
                <label className="block text-xs font-semibold text-zinc-700 dark:text-zinc-300 mb-1.5">Webinar</label>
                <select
                  value={webinarId}
                  onChange={(e) => setWebinarId(e.target.value)}
                  className="w-full bg-zinc-50 dark:bg-zinc-950 border border-zinc-300 dark:border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
                >
                  <option value="">Select webinar…</option>
                  {sortedWebinars.map((w) => (
                    <option key={w.id} value={w.id}>{webinarLabel(w)}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-xs font-semibold text-zinc-700 dark:text-zinc-300 mb-1.5">
                  Sender <span className="text-zinc-500 font-normal">(optional)</span>
                </label>
                <select
                  value={senderId}
                  onChange={(e) => setSenderId(e.target.value)}
                  className="w-full bg-zinc-50 dark:bg-zinc-950 border border-zinc-300 dark:border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
                >
                  <option value="">No sender (leave accounts unmapped)</option>
                  {sortedSenders.map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
                <div className="mt-1 text-[11px] text-zinc-500">
                  Every calendar_account in this file will be mapped to the chosen sender. Re-uploading overrides existing mappings.
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-zinc-700 dark:text-zinc-300 mb-1.5">CSV file</label>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".csv,text/csv"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                  className="block w-full text-xs text-zinc-700 dark:text-zinc-300 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:text-xs file:font-semibold file:bg-violet-600 file:text-white hover:file:bg-violet-500"
                />
                {file && (
                  <div className="mt-1.5 text-[11px] text-zinc-500">
                    {file.name} — {(file.size / 1024 / 1024).toFixed(2)} MB
                  </div>
                )}
              </div>

              <div className="text-[11px] text-zinc-500 leading-relaxed">
                Auto-mapped columns: <span className="font-mono">Email</span>, <span className="font-mono">Calendar_invited_date</span>,{" "}
                <span className="font-mono">Calendar_account</span>, <span className="font-mono">Calendar account prefix</span>,{" "}
                <span className="font-mono">Calendar_webinar_series</span>,{" "}
                <span className="font-mono">Calendar_invite_response</span> (optional). All other columns are ignored.
              </div>
            </>
          )}

          {step === "uploading" && (
            <div className="py-4">
              <div className="text-xs text-zinc-600 dark:text-zinc-400 mb-2">Uploading {file?.name}…</div>
              <div className="w-full h-2 bg-zinc-200 dark:bg-zinc-800 rounded overflow-hidden">
                <div className="h-full bg-violet-500 transition-all" style={{ width: `${progress}%` }} />
              </div>
              <div className="text-[11px] text-zinc-500 mt-1.5">{progress}%</div>
            </div>
          )}

          {step === "confirmed" && confirmed && (
            <>
              <div className="grid grid-cols-3 gap-3">
                <Stat label="Rows" value={confirmed.total_rows.toLocaleString()} />
                <Stat
                  label="Response col"
                  value={confirmed.has_responses ? "Present ✓" : "Absent"}
                  tone={confirmed.has_responses ? "good" : "muted"}
                />
                <Stat label="Headers" value={confirmed.headers.length.toString()} />
              </div>

              <div>
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">Detected headers</div>
                <div className="flex flex-wrap gap-1">
                  {confirmed.headers.map((h, i) => {
                    const hl = h.toLowerCase();
                    const recognised = [
                      "email", "calendar_invited_date", "calendar_account",
                      "calendar account prefix", "calendar_account_prefix",
                      "calendar_webinar_series", "calendar_invite_response",
                    ].includes(hl);
                    return (
                      <span
                        key={i}
                        className={`px-1.5 py-0.5 rounded text-[10px] font-mono border ${
                          recognised
                            ? "bg-violet-500/10 text-violet-400 border-violet-500/30"
                            : "bg-zinc-500/10 text-zinc-500 border-zinc-500/20"
                        }`}
                        title={recognised ? "Will be imported" : "Ignored"}
                      >
                        {h}
                      </span>
                    );
                  })}
                </div>
              </div>

              {confirmed.preview_rows.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">Preview (first {confirmed.preview_rows.length} rows)</div>
                  <div className="overflow-x-auto border border-zinc-200 dark:border-zinc-800 rounded">
                    <table className="text-[11px] w-full">
                      <thead className="bg-zinc-50 dark:bg-zinc-900 text-zinc-500">
                        <tr>
                          {confirmed.headers.map((h, i) => (
                            <th key={i} className="text-left px-2 py-1 font-mono font-normal whitespace-nowrap">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                        {confirmed.preview_rows.map((row, ri) => (
                          <tr key={ri} className="bg-white dark:bg-zinc-950">
                            {row.map((cell, ci) => (
                              <td key={ci} className="px-2 py-1 font-mono text-zinc-700 dark:text-zinc-300 whitespace-nowrap max-w-[160px] truncate">{cell}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}

          {step === "starting" && (
            <div className="text-xs text-zinc-600 dark:text-zinc-400 py-4">Starting import…</div>
          )}
        </div>

        <div className="px-6 py-4 border-t border-zinc-200 dark:border-zinc-800 flex justify-end gap-2">
          <button onClick={onClose} className="px-3 py-1.5 text-xs rounded-lg bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 text-zinc-700 dark:text-zinc-200">
            Cancel
          </button>
          {step === "pick" && (
            <button
              onClick={handleUpload}
              disabled={!file || !webinarId}
              className="px-3 py-1.5 text-xs rounded-lg bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Upload
            </button>
          )}
          {step === "confirmed" && (
            <button
              onClick={handleStart}
              className="px-3 py-1.5 text-xs rounded-lg bg-violet-600 hover:bg-violet-500 text-white font-semibold"
            >
              Start Import
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "good" | "muted" }) {
  const color =
    tone === "good"
      ? "text-emerald-500"
      : tone === "muted"
      ? "text-zinc-500"
      : "text-zinc-800 dark:text-zinc-200";
  return (
    <div className="px-3 py-2 rounded-md bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">{label}</div>
      <div className={`text-sm font-bold font-mono ${color}`}>{value}</div>
    </div>
  );
}
