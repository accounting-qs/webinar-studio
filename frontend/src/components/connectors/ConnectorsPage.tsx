"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  fetchWgStatus,
  saveWgApiKey,
  deleteWgApiKey,
  fetchWgWebinars,
  refreshWgWebinars,
  syncWgSubscribers,
  syncAllWgSubscribers,
  fetchWgSubscribers,
  wgSubscribersCsvUrl,
  fetchWgCredentials,
  createWgCredential,
  updateWgCredential,
  deleteWgCredential,
  type ApiWgCredential,
  type WgCredentialStatus,
  type WgWebinar,
  type WgSubscriber,
} from "@/lib/api";

type Tab = "config" | "broadcasts" | "subscribers";

function formatDuration(sec: number | null | undefined): string {
  if (sec == null || sec <= 0) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString();
}

function StatusPill({ w }: { w: WgWebinar }) {
  let label = "Ended";
  let cls = "bg-sky-500/15 text-sky-500 border-sky-500/30";
  if (w.cancelled) { label = "Cancelled"; cls = "bg-red-500/15 text-red-400 border-red-500/30"; }
  else if (!w.has_ended) { label = "Active"; cls = "bg-emerald-500/15 text-emerald-500 border-emerald-500/30"; }
  return <span className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${cls}`}>{label}</span>;
}

export function ConnectorsPage() {
  const [tab, setTab] = useState<Tab>("config");
  const [status, setStatus] = useState<WgCredentialStatus | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(true);

  useEffect(() => {
    fetchWgStatus()
      .then((s) => {
        setStatus(s);
        if (s.configured) setTab("broadcasts");
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoadingStatus(false));
  }, []);

  async function handleSave() {
    setError(null); setMessage(null); setSaving(true);
    try {
      const s = await saveWgApiKey(apiKeyInput.trim());
      setStatus(s);
      setApiKeyInput("");
      setMessage("API key saved.");
      setTab("broadcasts");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!confirm("Remove WebinarGeek API key? Synced data will be preserved.")) return;
    try {
      await deleteWgApiKey();
      setStatus({ configured: false });
      setTab("config");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete");
    }
  }

  if (loadingStatus) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="px-6 py-6 max-w-[1400px]">
      <div className="flex items-center gap-3 mb-6">
        <Link
          href="/connectors"
          className="text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 text-lg"
          aria-label="Back to connectors"
        >
          ←
        </Link>
        <div className="w-8 h-8 rounded-md bg-violet-500/15 flex items-center justify-center">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-violet-500">
            <polygon points="5 3 19 12 5 21 5 3" />
          </svg>
        </div>
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">
          WebinarGeek
        </h1>
      </div>

      {/* Tabs — segmented control with purple ring on active */}
      <div className="grid grid-cols-3 p-1.5 rounded-xl bg-zinc-100 dark:bg-zinc-900/60 mb-6">
        {([
          { key: "config", label: "Configuration" },
          { key: "broadcasts", label: "Broadcasts" },
          { key: "subscribers", label: "Subscribers" },
        ] as const).map((t) => {
          const active = tab === t.key;
          const disabled = !status?.configured && t.key !== "config";
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              disabled={disabled}
              className={`py-3 rounded-lg text-sm font-semibold transition-all ${
                active
                  ? "bg-white dark:bg-zinc-800 text-zinc-900 dark:text-zinc-100 ring-2 ring-violet-500 shadow-sm"
                  : "text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 disabled:opacity-50 disabled:cursor-not-allowed"
              }`}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {error && (
        <div className="mb-4 px-3 py-2 rounded-md border border-red-500/30 bg-red-500/10 text-xs text-red-500">
          {error}
        </div>
      )}
      {message && (
        <div className="mb-4 px-3 py-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 text-xs text-emerald-500">
          {message}
        </div>
      )}

      {tab === "config" && (
        <ConfigTab
          status={status}
          apiKeyInput={apiKeyInput}
          setApiKeyInput={setApiKeyInput}
          onSave={handleSave}
          onDelete={handleDelete}
          saving={saving}
        />
      )}
      {tab === "broadcasts" && status?.configured && <BroadcastsTab onMessage={setMessage} onError={setError} />}
      {tab === "subscribers" && status?.configured && <SubscribersTab />}
    </div>
  );
}

/* ─── Configuration tab ──────────────────────────────────────────────── */
function ConfigTab(props: {
  status: WgCredentialStatus | null;
  apiKeyInput: string;
  setApiKeyInput: (v: string) => void;
  onSave: () => void;
  onDelete: () => void;
  saving: boolean;
}) {
  const { status, apiKeyInput, setApiKeyInput, onSave, onDelete, saving } = props;
  return (
    <div className="space-y-4">
      <DefaultWgConfigCard {...{ status, apiKeyInput, setApiKeyInput, onSave, onDelete, saving }} />
      <AdditionalWgCredentialsCard />
    </div>
  );
}

/* ─── Default WG credential — legacy single-credential editor ─────────── */
function DefaultWgConfigCard(props: {
  status: WgCredentialStatus | null;
  apiKeyInput: string;
  setApiKeyInput: (v: string) => void;
  onSave: () => void;
  onDelete: () => void;
  saving: boolean;
}) {
  const { status, apiKeyInput, setApiKeyInput, onSave, onDelete, saving } = props;
  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 p-4">
      <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 mb-1">WebinarGeek API (default)</h2>
      <p className="text-xs text-zinc-500 mb-4">
        The default credential — used by every webinar that hasn't picked a specific account.
      </p>

      {status?.configured ? (
        <div className="flex items-center gap-3">
          <div className="flex-1">
            <label className="block text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">API Key</label>
            <div className="font-mono text-xs text-zinc-700 dark:text-zinc-300">{status.api_key_masked}</div>
          </div>
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-emerald-500/15 text-emerald-500 border-emerald-500/30">
            Connected
          </span>
          <button
            onClick={onDelete}
            className="px-3 py-1.5 text-xs rounded-md border border-red-500/40 text-red-500 hover:bg-red-500/10"
          >
            Remove
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          <label className="block text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">
            WebinarGeek API Key
          </label>
          <div className="flex gap-2">
            <input
              type="password"
              value={apiKeyInput}
              onChange={(e) => setApiKeyInput(e.target.value)}
              placeholder="Paste your API key"
              className="flex-1 bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
            />
            <button
              onClick={onSave}
              disabled={!apiKeyInput.trim() || saving}
              className="px-3 py-1.5 text-xs rounded-md bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {saving ? "Saving..." : "Save & Verify"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

/* ─── Additional WG credentials (multi-account) ───────────────────────
 * Each row is a named WebinarGeek API key. Webinar variants pick one of
 * these on the new-webinar form so an A/B test can target two different
 * WebinarGeek workspaces. The 'default' row stays managed by the legacy
 * card above and is hidden here. */
function AdditionalWgCredentialsCard() {
  const [rows, setRows] = useState<ApiWgCredential[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newKey, setNewKey] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    try {
      const { credentials } = await fetchWgCredentials();
      setRows(credentials.filter((c) => c.name !== "default"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load credentials");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleCreate() {
    setError(null);
    if (!newName.trim() || !newKey.trim()) return;
    setCreating(true);
    try {
      await createWgCredential({ name: newName.trim(), api_key: newKey.trim() });
      setNewName("");
      setNewKey("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add credential");
    } finally {
      setCreating(false);
    }
  }

  async function handleRename(c: ApiWgCredential) {
    const next = prompt(`Rename "${c.name}" to:`, c.name);
    if (!next || next.trim() === c.name) return;
    setBusyId(c.id);
    setError(null);
    try {
      await updateWgCredential(c.id, { name: next.trim() });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to rename");
    } finally {
      setBusyId(null);
    }
  }

  async function handleReplaceKey(c: ApiWgCredential) {
    const next = prompt(`Paste new API key for "${c.name}":`);
    if (!next || !next.trim()) return;
    setBusyId(c.id);
    setError(null);
    try {
      await updateWgCredential(c.id, { api_key: next.trim() });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update key");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(c: ApiWgCredential) {
    if (!confirm(`Delete credential "${c.name}"? Variants using it will fall back to the default credential.`)) return;
    setBusyId(c.id);
    setError(null);
    try {
      await deleteWgCredential(c.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 p-4">
      <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 mb-1">Additional WebinarGeek accounts</h2>
      <p className="text-xs text-zinc-500 mb-4">
        Add one more credential per WebinarGeek workspace you want to A/B test against. Each variant on the
        Planning page picks a credential from this list. The webinar's broadcast must live in the chosen
        account, otherwise sync will return 404.
      </p>

      {error && (
        <div className="mb-3 px-3 py-2 rounded-md bg-red-500/10 border border-red-500/30 text-xs text-red-500">{error}</div>
      )}

      {loading ? (
        <div className="text-xs text-zinc-500">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="text-xs text-zinc-500 italic mb-3">No additional accounts yet.</div>
      ) : (
        <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800/60 mb-4">
          <table className="w-full text-xs">
            <thead className="bg-zinc-50 dark:bg-zinc-900/60">
              <tr>
                <th className="text-left px-3 py-2 font-semibold text-zinc-600 dark:text-zinc-400">Name</th>
                <th className="text-left px-3 py-2 font-semibold text-zinc-600 dark:text-zinc-400">API key</th>
                <th className="text-right px-3 py-2 font-semibold text-zinc-600 dark:text-zinc-400">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id} className="border-t border-zinc-200 dark:border-zinc-800/40">
                  <td className="px-3 py-2 font-medium text-zinc-800 dark:text-zinc-200">{c.name}</td>
                  <td className="px-3 py-2 font-mono text-zinc-600 dark:text-zinc-400">{c.api_key_masked}</td>
                  <td className="px-3 py-2 text-right">
                    <div className="inline-flex gap-1.5">
                      <button
                        disabled={busyId === c.id}
                        onClick={() => handleRename(c)}
                        className="px-2 py-1 rounded text-[10px] font-semibold border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-50"
                      >Rename</button>
                      <button
                        disabled={busyId === c.id}
                        onClick={() => handleReplaceKey(c)}
                        className="px-2 py-1 rounded text-[10px] font-semibold border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-50"
                      >Replace key</button>
                      <button
                        disabled={busyId === c.id}
                        onClick={() => handleDelete(c)}
                        className="px-2 py-1 rounded text-[10px] font-semibold border border-red-500/40 text-red-500 hover:bg-red-500/10 disabled:opacity-50"
                      >Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Add form */}
      <div className="grid grid-cols-1 sm:grid-cols-[1fr_2fr_auto] gap-2">
        <input
          type="text"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder='Account name (e.g. "Account B")'
          className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
        />
        <input
          type="password"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder="WebinarGeek API key"
          className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
        />
        <button
          onClick={handleCreate}
          disabled={creating || !newName.trim() || !newKey.trim()}
          className="px-3 py-1.5 text-xs rounded-md bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
        >
          {creating ? "Adding…" : "Add account"}
        </button>
      </div>
    </section>
  );
}

/* ─── Broadcasts tab ─────────────────────────────────────────────────── */
function BroadcastsTab(props: { onMessage: (m: string) => void; onError: (e: string) => void }) {
  const [rows, setRows] = useState<WgWebinar[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [syncAllRunning, setSyncAllRunning] = useState(false);
  const [syncingId, setSyncingId] = useState<string | null>(null);

  async function load(q?: string) {
    setLoading(true);
    try {
      const { broadcasts, total } = await fetchWgWebinars({ limit: 500, q });
      setRows(broadcasts);
      setTotal(total);
    } catch (e) {
      props.onError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load(); }, []);

  const filtered = useMemo(() => {
    if (!filter.trim()) return rows;
    const q = filter.toLowerCase();
    return rows.filter((r) =>
      r.broadcast_id.includes(q) ||
      r.name.toLowerCase().includes(q) ||
      (r.internal_title ?? "").toLowerCase().includes(q)
    );
  }, [rows, filter]);

  async function handleRefresh() {
    setRefreshing(true); props.onError(""); props.onMessage("");
    try {
      const { count } = await refreshWgWebinars();
      props.onMessage(`Refreshed — ${count} broadcasts loaded.`);
      await load();
    } catch (e) {
      props.onError(e instanceof Error ? e.message : "Refresh failed");
    } finally {
      setRefreshing(false);
    }
  }

  async function handleSyncAll() {
    if (!confirm(`Queue subscriber sync for all ${rows.length} broadcasts? Runs in the background — track progress on the Sync page.`)) return;
    setSyncAllRunning(true);
    try {
      const res = await syncAllWgSubscribers();
      props.onMessage(`Queued sync for ${res.broadcasts_queued} broadcasts. Track progress on the Sync page.`);
    } catch (e) {
      props.onError(e instanceof Error ? e.message : "Sync all failed to start");
    } finally {
      setSyncAllRunning(false);
    }
  }

  async function handleSync(id: string) {
    setSyncingId(id);
    try {
      await syncWgSubscribers(id);
      props.onMessage(`Sync started for broadcast ${id}. Track progress on the Sync page.`);
    } catch (e) {
      props.onError(e instanceof Error ? e.message : "Sync failed to start");
    } finally {
      setSyncingId(null);
    }
  }

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 overflow-hidden">
      <header className="px-4 py-3 border-b border-zinc-200 dark:border-zinc-800/60 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">Broadcasts</h2>
          <p className="text-xs text-zinc-500 mt-0.5">
            {total} broadcasts cached. Click a row action to sync its subscribers.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className="px-3 py-1.5 text-xs rounded-md bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50"
          >
            {refreshing ? "Refreshing..." : "Sync Broadcasts"}
          </button>
          <button
            onClick={handleSyncAll}
            disabled={syncAllRunning || rows.length === 0}
            className="px-3 py-1.5 text-xs rounded-md bg-sky-600 hover:bg-sky-500 text-white font-semibold disabled:opacity-50"
          >
            {syncAllRunning ? "Syncing all..." : "Sync All Subscriber Data"}
          </button>
        </div>
      </header>

      <div className="p-4">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter broadcasts..."
          className="w-64 mb-3 bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
        />

        {loading ? (
          <div className="py-8 text-center text-xs text-zinc-500">Loading...</div>
        ) : filtered.length === 0 ? (
          <div className="py-8 text-center text-xs text-zinc-500">
            No broadcasts cached. Click "Sync Broadcasts" to pull from WebinarGeek.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-zinc-200 dark:border-zinc-800/60 text-zinc-500 text-[10px] uppercase tracking-wider">
                  <th className="text-left py-2 px-2 font-semibold">ID</th>
                  <th className="text-left py-2 px-2 font-semibold">Internal #</th>
                  <th className="text-left py-2 px-2 font-semibold">Date &amp; Time</th>
                  <th className="text-left py-2 px-2 font-semibold">Duration</th>
                  <th className="text-right py-2 px-2 font-semibold">Subscribers</th>
                  <th className="text-right py-2 px-2 font-semibold">Live Viewers</th>
                  <th className="text-right py-2 px-2 font-semibold">Replay Viewers</th>
                  <th className="text-left py-2 px-2 font-semibold">Status</th>
                  <th className="text-right py-2 px-2 font-semibold">Synced</th>
                  <th className="text-right py-2 px-2 font-semibold">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((w) => (
                  <tr key={w.broadcast_id} className="border-b border-zinc-100 dark:border-zinc-800/30 hover:bg-zinc-50 dark:hover:bg-zinc-900/40">
                    <td className="py-2 px-2 font-mono text-zinc-700 dark:text-zinc-300">{w.broadcast_id}</td>
                    <td className="py-2 px-2 text-zinc-600 dark:text-zinc-400">{w.internal_title || "—"}</td>
                    <td className="py-2 px-2 text-zinc-600 dark:text-zinc-400">{formatDateTime(w.starts_at)}</td>
                    <td className="py-2 px-2 text-zinc-600 dark:text-zinc-400">{formatDuration(w.duration_seconds)}</td>
                    <td className="py-2 px-2 text-right font-mono">{w.subscriptions_count}</td>
                    <td className="py-2 px-2 text-right font-mono">{w.live_viewers_count}</td>
                    <td className="py-2 px-2 text-right font-mono">{w.replay_viewers_count}</td>
                    <td className="py-2 px-2"><StatusPill w={w} /></td>
                    <td className="py-2 px-2 text-right font-mono text-zinc-500">{w.synced_subscriber_count}</td>
                    <td className="py-2 px-2 text-right">
                      <button
                        onClick={() => handleSync(w.broadcast_id)}
                        disabled={syncingId === w.broadcast_id}
                        className="px-2.5 py-1 text-[11px] rounded-md border border-zinc-300 dark:border-zinc-700/60 hover:bg-zinc-100 dark:hover:bg-zinc-800/50 disabled:opacity-50"
                      >
                        {syncingId === w.broadcast_id ? "Syncing..." : "Get Subscribers"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

/* ─── Subscribers tab ────────────────────────────────────────────────── */
function SubscribersTab() {
  const PAGE = 100;
  const [broadcasts, setBroadcasts] = useState<WgWebinar[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [search, setSearch] = useState("");
  const [rows, setRows] = useState<WgSubscriber[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    fetchWgWebinars({ limit: 500 })
      .then(({ broadcasts }) => setBroadcasts(broadcasts))
      .catch(() => {});
  }, []);

  useEffect(() => {
    setLoading(true); setOffset(0);
    fetchWgSubscribers({ broadcast_id: selected || undefined, q: search || undefined, limit: PAGE, offset: 0 })
      .then(({ subscribers, total }) => {
        setRows(subscribers);
        setTotal(total);
      })
      .finally(() => setLoading(false));
  }, [selected, search]);

  async function loadMore() {
    const next = offset + PAGE;
    const { subscribers } = await fetchWgSubscribers({
      broadcast_id: selected || undefined,
      q: search || undefined,
      limit: PAGE,
      offset: next,
    });
    setRows((prev) => [...prev, ...subscribers]);
    setOffset(next);
  }

  const csvUrl = wgSubscribersCsvUrl({ broadcast_id: selected || undefined, q: search || undefined });

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 overflow-hidden">
      <header className="px-4 py-3 border-b border-zinc-200 dark:border-zinc-800/60 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">Subscribers</h2>
          <p className="text-xs text-zinc-500 mt-0.5">{total.toLocaleString()} total subscribers</p>
        </div>
        <a
          href={csvUrl}
          className="px-3 py-1.5 text-xs rounded-md border border-zinc-300 dark:border-zinc-700/60 hover:bg-zinc-100 dark:hover:bg-zinc-800/50"
        >
          ⬇ Export CSV
        </a>
      </header>

      <div className="p-4">
        <div className="flex gap-3 mb-3">
          <div className="flex items-center gap-2">
            <label className="text-xs text-zinc-500">Broadcast:</label>
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 min-w-[240px]"
            >
              <option value="">All Broadcasts</option>
              {broadcasts.map((b) => (
                <option key={b.broadcast_id} value={b.broadcast_id}>
                  {b.internal_title ? `${b.internal_title} · ` : ""}{formatDate(b.starts_at)} · {b.broadcast_id}
                </option>
              ))}
            </select>
          </div>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by email or name..."
            className="flex-1 max-w-md bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
          />
        </div>

        {loading ? (
          <div className="py-8 text-center text-xs text-zinc-500">Loading...</div>
        ) : rows.length === 0 ? (
          <div className="py-8 text-center text-xs text-zinc-500">
            No subscribers synced yet. Use the Broadcasts tab to sync a broadcast.
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-zinc-200 dark:border-zinc-800/60 text-zinc-500 text-[10px] uppercase tracking-wider">
                    <th className="text-left py-2 px-2 font-semibold">Email</th>
                    <th className="text-left py-2 px-2 font-semibold">Name</th>
                    <th className="text-left py-2 px-2 font-semibold">Broadcast ID</th>
                    <th className="text-left py-2 px-2 font-semibold">Registered</th>
                    <th className="text-left py-2 px-2 font-semibold">Source</th>
                    <th className="text-left py-2 px-2 font-semibold">Watched</th>
                    <th className="text-right py-2 px-2 font-semibold">Duration</th>
                    <th className="text-left py-2 px-2 font-semibold">Device</th>
                    <th className="text-left py-2 px-2 font-semibold">Country</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={s.id} className="border-b border-zinc-100 dark:border-zinc-800/30">
                      <td className="py-2 px-2 text-zinc-800 dark:text-zinc-200">{s.email}</td>
                      <td className="py-2 px-2 text-zinc-700 dark:text-zinc-300">{[s.first_name, s.last_name].filter(Boolean).join(" ") || "—"}</td>
                      <td className="py-2 px-2 font-mono text-zinc-500">{s.broadcast_id}</td>
                      <td className="py-2 px-2 text-zinc-600 dark:text-zinc-400">{formatDate(s.subscribed_at)}</td>
                      <td className="py-2 px-2 text-zinc-600 dark:text-zinc-400">{s.registration_source || "—"}</td>
                      <td className="py-2 px-2">
                        {s.watched_live ? <span className="text-emerald-500">✓ Live</span>
                          : s.watched_replay ? <span className="text-sky-500">✓ Replay</span>
                          : <span className="text-zinc-500">✕</span>}
                      </td>
                      <td className="py-2 px-2 text-right font-mono text-zinc-600 dark:text-zinc-400">
                        {s.minutes_viewing != null ? `${s.minutes_viewing}m` : "—"}
                      </td>
                      <td className="py-2 px-2 text-zinc-600 dark:text-zinc-400">{s.viewing_device || "—"}</td>
                      <td className="py-2 px-2 text-zinc-600 dark:text-zinc-400">{s.viewing_country || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {rows.length < total && (
              <div className="pt-3 text-center">
                <button
                  onClick={loadMore}
                  className="px-4 py-1.5 text-xs rounded-md border border-zinc-300 dark:border-zinc-700/60 hover:bg-zinc-100 dark:hover:bg-zinc-800/50"
                >
                  Load more ({rows.length} / {total})
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
