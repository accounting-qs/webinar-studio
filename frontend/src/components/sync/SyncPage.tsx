"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  fetchGhlSyncStatus,
  fetchGhlSyncHistory,
  triggerGhlSync,
  fetchGhlSyncSettings,
  updateGhlSyncSettings,
  cancelGhlSyncRun,
  recoverStaleGhlSyncs,
  type GhlSyncRun,
  type GhlSyncStatus,
  type GhlSyncSettings,
} from "@/lib/api";

const DAYS = [
  { value: "mon", label: "Mon" },
  { value: "tue", label: "Tue" },
  { value: "wed", label: "Wed" },
  { value: "thu", label: "Thu" },
  { value: "fri", label: "Fri" },
  { value: "sat", label: "Sat" },
  { value: "sun", label: "Sun" },
];

const INTERVAL_OPTIONS = [1, 2, 3, 6, 12, 24];

function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", hour12: true,
  });
}

function formatRelative(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const diffMs = Date.now() - d.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin} min ago`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH} hr${diffH === 1 ? "" : "s"} ago`;
  const diffD = Math.floor(diffH / 24);
  return `${diffD} day${diffD === 1 ? "" : "s"} ago`;
}

/**
 * Friendly label for sync_type:
 *   "incremental" -> "Incremental"
 *   "full" -> "Full"
 *   "webinar:136:narrow" -> "Webinar 136 · Narrow"
 *   "webinar:136:deep"   -> "Webinar 136 · Deep"
 *   "webinar:136"        -> "Webinar 136" (legacy)
 *   "wg:all"             -> "WG · All Broadcasts"
 *   "wg:<broadcast_id>"  -> "WG · Broadcast <id>"
 */
function formatSyncType(raw: string): string {
  if (raw.startsWith("webinar:")) {
    const rest = raw.slice(8); // "136" or "136:narrow" or "136:deep"
    const [n, phase] = rest.split(":");
    if (phase) {
      return `Webinar ${n} · ${phase.charAt(0).toUpperCase() + phase.slice(1)}`;
    }
    return `Webinar ${n}`;
  }
  if (raw.startsWith("wg:")) {
    const id = raw.slice(3);
    if (id === "all") return "WG · All Broadcasts";
    return `WG · Broadcast ${id}`;
  }
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

/** Estimate remaining seconds given elapsed time, synced count, and expected total. */
function estimateEtaSeconds(
  run: GhlSyncRun,
): number | null {
  if (run.status !== "running") return null;
  if (!run.expected_total || run.expected_total <= 0) return null;
  const synced = run.contacts_synced;
  if (synced <= 0) return null;
  const elapsedMs = Date.now() - new Date(run.started_at).getTime();
  if (elapsedMs <= 0) return null;
  const rate = synced / (elapsedMs / 1000); // per second
  const remaining = run.expected_total - synced;
  if (remaining <= 0) return 0;
  return Math.round(remaining / rate);
}

function formatElapsed(startedAt: string): string {
  const diffS = Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000);
  return formatDuration(diffS);
}

function StatusPill({ status, cancelRequested }: { status: string; cancelRequested?: boolean }) {
  const colors: Record<string, string> = {
    running: "bg-amber-500/15 text-amber-500 border-amber-500/30",
    completed: "bg-emerald-500/15 text-emerald-500 border-emerald-500/30",
    failed: "bg-red-500/15 text-red-400 border-red-500/30",
    cancelled: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
  };
  const isCancelling = status === "running" && cancelRequested;
  const label = isCancelling ? "⌛ cancelling"
    : status === "completed" ? "✓ completed"
    : status === "running" ? "• running"
    : status === "failed" ? "✗ failed"
    : status === "cancelled" ? "⊘ cancelled"
    : status;
  const className = isCancelling ? colors.cancelled : (colors[status] ?? "bg-zinc-500/15 text-zinc-400 border-zinc-500/30");
  return (
    <span className={`px-2 py-0.5 rounded text-[10px] font-semibold border uppercase tracking-wider ${className}`}>
      {label}
    </span>
  );
}

const STALE_HEARTBEAT_SECONDS = 600; // matches backend STALE_HEARTBEAT_SECONDS

function isStaleHeartbeat(run: GhlSyncRun): boolean {
  if (run.status !== "running") return false;
  const last = run.last_heartbeat_at ?? run.started_at;
  if (!last) return false;
  const ageSec = (Date.now() - new Date(last).getTime()) / 1000;
  return ageSec > STALE_HEARTBEAT_SECONDS;
}

interface Stats {
  lastSyncRelative: string;
  status: string;
  recentFailures: number;
  avgDurationSeconds: number | null;
  totalSynced24h: number;
}

function computeStats(history: GhlSyncRun[]): Stats {
  const now = Date.now();
  const last24h = now - 24 * 3600 * 1000;

  const latest = history[0];
  const lastSyncRelative = latest ? formatRelative(latest.started_at) : "—";
  const status = latest?.status ?? "idle";

  const runs24h = history.filter((r) => new Date(r.started_at).getTime() >= last24h);
  const recentFailures = runs24h.filter((r) => r.status === "failed").length;
  const completedDurations = runs24h
    .filter((r) => r.status === "completed" && r.duration_seconds !== null)
    .map((r) => r.duration_seconds as number);
  const avgDurationSeconds = completedDurations.length
    ? Math.round(completedDurations.reduce((s, x) => s + x, 0) / completedDurations.length)
    : null;
  const totalSynced24h = runs24h.reduce(
    (s, r) => s + (r.contacts_synced || 0) + (r.opportunities_synced || 0),
    0,
  );

  return { lastSyncRelative, status, recentFailures, avgDurationSeconds, totalSynced24h };
}

function StatCard({ label, value, valueClass }: { label: string; value: string | number; valueClass?: string }) {
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-zinc-50 dark:bg-zinc-900/40 p-4">
      <div className="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold mb-1.5">{label}</div>
      <div className={`text-2xl font-bold font-mono ${valueClass ?? "text-zinc-900 dark:text-zinc-100"}`}>{value}</div>
    </div>
  );
}

export function SyncPage() {
  const [status, setStatus] = useState<GhlSyncStatus | null>(null);
  const [history, setHistory] = useState<GhlSyncRun[]>([]);
  const [settings, setSettings] = useState<GhlSyncSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [triggering, setTriggering] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [expandedErrors, setExpandedErrors] = useState<Set<string>>(new Set());
  const [cancellingIds, setCancellingIds] = useState<Set<string>>(new Set());
  const [recovering, setRecovering] = useState(false);
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [st, h, s] = await Promise.all([
        fetchGhlSyncStatus(),
        fetchGhlSyncHistory(50),
        fetchGhlSyncSettings(),
      ]);
      setStatus(st);
      setHistory(h.runs);
      setSettings(s);
    } catch (err) {
      console.error("Failed to load sync data:", err);
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    (async () => {
      await refresh();
      if (mounted) setLoading(false);
    })();
    return () => { mounted = false; };
  }, [refresh]);

  useEffect(() => {
    if (status?.is_running) {
      pollRef.current = window.setInterval(refresh, 5000);
    } else if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [status?.is_running, refresh]);

  const stats = useMemo(() => computeStats(history), [history]);

  const handleTrigger = async (syncType: "full" | "incremental") => {
    if (triggering) return;
    setTriggering(true);
    try {
      await triggerGhlSync(syncType);
      await refresh();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to trigger sync");
    } finally {
      setTriggering(false);
    }
  };

  const handleCancel = async (run: GhlSyncRun) => {
    if (cancellingIds.has(run.id)) return;
    if (!window.confirm(`Stop ${formatSyncType(run.sync_type)}? The sync will exit at the next batch boundary (usually within seconds).`)) return;
    setCancellingIds((prev) => new Set(prev).add(run.id));
    try {
      await cancelGhlSyncRun(run.id);
      await refresh();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to cancel sync");
    } finally {
      setCancellingIds((prev) => {
        const next = new Set(prev);
        next.delete(run.id);
        return next;
      });
    }
  };

  const handleRecoverStale = async () => {
    if (recovering) return;
    setRecovering(true);
    try {
      const res = await recoverStaleGhlSyncs();
      await refresh();
      alert(`Recovered ${res.recovered} orphan(s) and swept ${res.swept} stale run(s).`);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to recover stale syncs");
    } finally {
      setRecovering(false);
    }
  };

  const handleSettingsChange = async (patch: Partial<GhlSyncSettings>) => {
    if (!settings) return;
    setSavingSettings(true);
    try {
      const updated = await updateGhlSyncSettings(patch);
      setSettings(updated);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to save settings");
    } finally {
      setSavingSettings(false);
    }
  };

  const toggleErrorExpand = (runId: string) => {
    setExpandedErrors((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="flex items-center gap-3">
          <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
          <span className="text-sm text-zinc-500">Loading sync status...</span>
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* Sticky header */}
      <div className="sticky top-12 z-40 bg-white dark:bg-zinc-950/90 backdrop-blur-md border-b border-zinc-200 dark:border-zinc-800/40 px-6 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-baseline gap-3">
            <h1 className="text-lg font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Sync History</h1>
            <span className="text-[11px] text-zinc-500">All times in your local timezone</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleRecoverStale}
              disabled={recovering}
              title="Mark orphaned 'running' rows as failed (one-shot scan; the scheduler also runs this every 2 min)"
              className="px-3 py-1.5 text-xs font-semibold rounded-lg bg-zinc-100 dark:bg-zinc-800 text-zinc-700 dark:text-zinc-200 hover:bg-zinc-200 dark:hover:bg-zinc-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {recovering ? "Recovering…" : "Recover Stale"}
            </button>
            <button
              onClick={() => handleTrigger("incremental")}
              disabled={triggering || status?.is_running}
              className="px-3 py-1.5 text-xs font-semibold rounded-lg bg-zinc-100 dark:bg-zinc-800 text-zinc-700 dark:text-zinc-200 hover:bg-zinc-200 dark:hover:bg-zinc-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Sync Incremental
            </button>
            <button
              onClick={() => handleTrigger("full")}
              disabled={triggering || status?.is_running}
              className="px-3 py-1.5 text-xs font-semibold rounded-lg bg-violet-600 hover:bg-violet-500 text-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Sync Full
            </button>
          </div>
        </div>
      </div>

      <div className="px-6 py-6 space-y-6 max-w-7xl">
        {/* Stats cards */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <StatCard label="Last Sync" value={stats.lastSyncRelative} />
          <StatCard
            label="Status"
            value={stats.status}
            valueClass={
              stats.status === "completed" ? "text-emerald-400"
              : stats.status === "running" ? "text-amber-400"
              : stats.status === "failed" ? "text-red-400"
              : "text-zinc-400"
            }
          />
          <StatCard
            label="Recent Failures (24h)"
            value={stats.recentFailures}
            valueClass={stats.recentFailures > 0 ? "text-red-400" : "text-emerald-400"}
          />
          <StatCard
            label="Avg Duration"
            value={stats.avgDurationSeconds !== null ? formatDuration(stats.avgDurationSeconds) : "—"}
          />
          <StatCard
            label="Total Synced (24h)"
            value={stats.totalSynced24h.toLocaleString()}
            valueClass="text-violet-400"
          />
        </div>

        {/* History table */}
        <div className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/20 overflow-hidden">
          {history.length === 0 ? (
            <p className="p-4 text-sm text-zinc-500">No syncs yet. Click "Sync Incremental" or "Sync Full" above to run the first one.</p>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-zinc-50 dark:bg-zinc-900/50 text-left border-b border-zinc-200 dark:border-zinc-800/40">
                  <th className="px-4 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Started</th>
                  <th className="px-4 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Type</th>
                  <th className="px-4 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Trigger</th>
                  <th className="px-4 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Status</th>
                  <th className="px-4 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] text-right">Duration</th>
                  <th className="px-4 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] text-right">Contacts</th>
                  <th className="px-4 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] text-right">Opportunities</th>
                  <th className="px-4 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] text-right">Errors</th>
                  <th className="px-4 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] text-right"></th>
                </tr>
              </thead>
              <tbody>
                {history.map((r) => {
                  const isRunning = r.status === "running";
                  const eta = isRunning ? estimateEtaSeconds(r) : null;
                  const progressPct = (isRunning && r.expected_total && r.expected_total > 0)
                    ? Math.min(100, Math.round((r.contacts_synced / r.expected_total) * 100))
                    : null;
                  const stale = isStaleHeartbeat(r);
                  const isCancelling = cancellingIds.has(r.id) || (isRunning && r.cancel_requested);
                  return (
                  <tr
                    key={r.id}
                    className={`border-t border-zinc-200 dark:border-zinc-800/20 ${stale ? "bg-red-500/5" : ""} ${r.errors_count > 0 ? "cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-800/30" : ""}`}
                    onClick={r.errors_count > 0 ? () => toggleErrorExpand(r.id) : undefined}
                  >
                    <td className="px-4 py-2.5 text-zinc-700 dark:text-zinc-300 whitespace-nowrap">
                      {formatTimestamp(r.started_at)}
                    </td>
                    <td className="px-4 py-2.5">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-semibold border whitespace-nowrap ${
                        r.sync_type.startsWith("wg:")
                          ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
                          : r.sync_type.startsWith("webinar:")
                          ? "bg-violet-500/15 text-violet-400 border-violet-500/30"
                          : r.sync_type === "full"
                          ? "bg-sky-500/15 text-sky-400 border-sky-500/30"
                          : "bg-zinc-500/15 text-zinc-400 border-zinc-500/30"
                      }`}>
                        {formatSyncType(r.sync_type)}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-zinc-600 dark:text-zinc-400 capitalize">{r.trigger}</td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <StatusPill status={r.status} cancelRequested={r.cancel_requested} />
                        {stale && (
                          <span
                            title={`No heartbeat for >${STALE_HEARTBEAT_SECONDS / 60} min — sweeper will mark this failed shortly`}
                            className="px-1.5 py-0.5 rounded text-[9px] font-semibold border bg-red-500/10 text-red-400 border-red-500/30 uppercase tracking-wider"
                          >
                            stale
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-zinc-700 dark:text-zinc-300 whitespace-nowrap">
                      {isRunning ? (
                        <div className="text-right">
                          <div>{formatElapsed(r.started_at)}</div>
                          {eta !== null && (
                            <div className="text-[9px] text-zinc-500">ETA {formatDuration(eta)}</div>
                          )}
                        </div>
                      ) : formatDuration(r.duration_seconds)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-zinc-700 dark:text-zinc-300 whitespace-nowrap">
                      {isRunning && r.expected_total ? (
                        <div className="text-right">
                          <div>{r.contacts_synced.toLocaleString()} / {r.expected_total.toLocaleString()}</div>
                          {progressPct !== null && (
                            <div className="text-[9px] text-violet-400">{progressPct}%</div>
                          )}
                        </div>
                      ) : r.contacts_synced.toLocaleString()}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-zinc-700 dark:text-zinc-300">{r.opportunities_synced.toLocaleString()}</td>
                    <td className={`px-4 py-2.5 text-right font-mono ${r.errors_count > 0 ? "text-red-400" : "text-zinc-500"}`}>
                      {r.errors_count}
                    </td>
                    <td className="px-4 py-2.5 text-right whitespace-nowrap">
                      {isRunning && (
                        <button
                          onClick={(e) => { e.stopPropagation(); handleCancel(r); }}
                          disabled={isCancelling}
                          className="px-2 py-0.5 text-[10px] font-semibold rounded border border-red-500/40 text-red-400 hover:bg-red-500/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors uppercase tracking-wider"
                        >
                          {isCancelling ? "Stopping…" : "Stop"}
                        </button>
                      )}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          {/* Error details expandable */}
          {[...expandedErrors].map((runId) => {
            const run = history.find((h) => h.id === runId);
            if (!run || !run.error_details) return null;
            return (
              <div key={runId} className="px-4 py-3 bg-red-500/5 border-t border-red-500/20 text-[11px]">
                <div className="text-red-400 font-semibold mb-1">Errors for run {runId.slice(0, 8)}...</div>
                <pre className="text-zinc-600 dark:text-zinc-400 overflow-x-auto whitespace-pre-wrap">
                  {JSON.stringify(run.error_details, null, 2)}
                </pre>
              </div>
            );
          })}
        </div>

        {/* Settings panel (kept as-is per user) */}
        {settings && (
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/20 p-4">
            <h2 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200 mb-3">Schedule Settings</h2>

            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={settings.incremental_enabled}
                    onChange={(e) => handleSettingsChange({ incremental_enabled: e.target.checked })}
                    disabled={savingSettings}
                    className="w-4 h-4"
                  />
                  <span className="font-semibold text-zinc-800 dark:text-zinc-200">Incremental sync</span>
                </label>
                <span className="text-zinc-500 text-xs">every</span>
                <select
                  value={settings.incremental_interval_hours}
                  onChange={(e) => handleSettingsChange({ incremental_interval_hours: parseInt(e.target.value) })}
                  disabled={savingSettings || !settings.incremental_enabled}
                  className="bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 disabled:opacity-50"
                >
                  {INTERVAL_OPTIONS.map((h) => (
                    <option key={h} value={h}>{h}h</option>
                  ))}
                </select>
              </div>

              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={settings.weekly_full_enabled}
                    onChange={(e) => handleSettingsChange({ weekly_full_enabled: e.target.checked })}
                    disabled={savingSettings}
                    className="w-4 h-4"
                  />
                  <span className="font-semibold text-zinc-800 dark:text-zinc-200">Weekly full sync</span>
                </label>
                <span className="text-zinc-500 text-xs">on</span>
                <select
                  value={settings.weekly_full_day_of_week}
                  onChange={(e) => handleSettingsChange({ weekly_full_day_of_week: e.target.value })}
                  disabled={savingSettings || !settings.weekly_full_enabled}
                  className="bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 disabled:opacity-50"
                >
                  {DAYS.map((d) => (
                    <option key={d.value} value={d.value}>{d.label}</option>
                  ))}
                </select>
                <span className="text-zinc-500 text-xs">at</span>
                <select
                  value={settings.weekly_full_hour_local}
                  onChange={(e) => handleSettingsChange({ weekly_full_hour_local: parseInt(e.target.value) })}
                  disabled={savingSettings || !settings.weekly_full_enabled}
                  className="bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 disabled:opacity-50"
                >
                  {Array.from({ length: 24 }).map((_, h) => (
                    <option key={h} value={h}>{h.toString().padStart(2, "0")}:00</option>
                  ))}
                </select>
                <input
                  type="text"
                  value={settings.weekly_full_timezone}
                  onChange={(e) => handleSettingsChange({ weekly_full_timezone: e.target.value })}
                  onBlur={(e) => handleSettingsChange({ weekly_full_timezone: e.target.value })}
                  disabled={savingSettings || !settings.weekly_full_enabled}
                  placeholder="America/Chicago"
                  className="w-40 bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 disabled:opacity-50"
                />
              </div>

              {settings.updated_at && (
                <p className="text-[10px] text-zinc-500">Last updated: {formatTimestamp(settings.updated_at)}</p>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
