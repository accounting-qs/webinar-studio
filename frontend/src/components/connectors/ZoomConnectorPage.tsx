"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  deleteZoomCredential,
  fetchZoomRegistrants,
  fetchZoomStatus,
  fetchZoomWebinars,
  refreshZoomWebinars,
  saveZoomCredential,
  syncAllZoomWebinars,
  syncZoomWebinar,
  testZoomConnection,
  zoomRegistrantsCsvUrl,
  type ZoomCredentialStatus,
  type ZoomRegistrant,
  type ZoomWebinar,
} from "@/lib/api";

type Tab = "config" | "webinars" | "registrants";

/** Copy button that confirms in place — on a setup page you are pasting from,
 *  silent copying leaves you unsure whether it worked. */
function CopyButton({ value, label = "Copy", className = "" }: {
  value: string; label?: string; className?: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
        } catch {
          return; // clipboard blocked (insecure context) — say nothing rather than lie
        }
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
      className={
        "px-2 py-0.5 text-[10px] rounded border transition-colors whitespace-nowrap " +
        (copied
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-500"
          : "border-zinc-300 dark:border-zinc-700 text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-800") +
        (className ? " " + className : "")
      }
    >
      {copied ? "Copied" : label}
    </button>
  );
}

function ZoomIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
    </svg>
  );
}

export function ZoomConnectorPage() {
  const [tab, setTab] = useState<Tab>("config");
  const [status, setStatus] = useState<ZoomCredentialStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await fetchZoomStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load Zoom status");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="w-4 h-4 border-2 border-sky-500 border-t-transparent rounded-full animate-spin" />
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
        <div className="w-8 h-8 rounded-md bg-sky-500/15 flex items-center justify-center">
          <ZoomIcon className="w-4 h-4 text-sky-500" />
        </div>
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Zoom</h1>
        <span
          className={`text-[10px] px-2 py-0.5 rounded-full border ${
            status?.configured
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-500"
              : "border-zinc-400/30 bg-zinc-400/10 text-zinc-500"
          }`}
        >
          {status?.configured ? "Connected" : "Not connected"}
        </span>
      </div>

      {error && (
        <div className="mb-4 px-3 py-2 rounded-md border border-red-500/30 bg-red-500/10 text-xs text-red-500 whitespace-pre-wrap">
          {error}
        </div>
      )}
      {message && (
        <div className="mb-4 px-3 py-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 text-xs text-emerald-500">
          {message}
        </div>
      )}

      <div className="flex gap-1 mb-5 border-b border-zinc-200 dark:border-zinc-800">
        {([
          ["config", "Setup"],
          ["webinars", "Webinars"],
          ["registrants", "Registrants"],
        ] as [Tab, string][]).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-3 py-2 text-xs font-medium -mb-px border-b-2 transition-colors ${
              tab === key
                ? "border-sky-500 text-sky-600 dark:text-sky-400"
                : "border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "config" && (
        <ConfigTab
          status={status}
          onSaved={(s, email) => {
            setStatus(s);
            setError(null);
            setMessage(
              email ? `Connected to Zoom account ${email}.` : "Zoom credentials saved.",
            );
          }}
          onError={(m) => {
            setError(m);
            setMessage(null);
          }}
          onDisconnected={() => {
            setMessage("Zoom disconnected.");
            setError(null);
            load();
          }}
        />
      )}
      {tab === "webinars" && (
        <WebinarsTab
          configured={!!status?.configured}
          onError={(m) => setError(m)}
          onMessage={(m) => setMessage(m)}
        />
      )}
      {tab === "registrants" && <RegistrantsTab onError={(m) => setError(m)} />}
    </div>
  );
}

/* ── Setup ─────────────────────────────────────────────────────────────── */

function ConfigTab({
  status,
  onSaved,
  onError,
  onDisconnected,
}: {
  status: ZoomCredentialStatus | null;
  onSaved: (s: ZoomCredentialStatus, email?: string | null) => void;
  onError: (m: string) => void;
  onDisconnected: () => void;
}) {
  const [accountId, setAccountId] = useState(status?.account_id ?? "");
  const [clientId, setClientId] = useState(status?.client_id ?? "");
  const [clientSecret, setClientSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  // Result of the last save/test — what drives the per-field and per-scope
  // status. Falls back to `status` so a page reload still shows the basics.
  const [result, setResult] = useState<ZoomCredentialStatus | null>(null);

  const view = result ?? status;
  const scopes = view?.scopes ?? [];
  const scopeList = scopes.map((s) => s.scope).join("\n");
  const missingScopes = view?.missing_scopes ?? [];
  const tested = !!view?.tested;
  const credsOk = view?.credentials_ok === true;
  const credStatus: FieldStatus = !tested ? "unknown" : credsOk ? "ok" : "bad";

  /** Everything Zoom needs to work: secrets accepted AND every probe passed. */
  function allGood(s: ZoomCredentialStatus): boolean {
    return s.credentials_ok === true
      && (s.missing_scopes?.length ?? 0) === 0
      && (s.checks?.length ?? 0) > 0
      && s.checks.every((c) => c.ok);
  }

  async function handleSave() {
    setSaving(true);
    try {
      const res = await saveZoomCredential({
        account_id: accountId.trim(),
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
      });
      setClientSecret("");
      setResult(res);
      // Saving now succeeds even when a scope is missing (the secrets are
      // valid, so they are worth keeping) — so the banner must not call that
      // "connected". The status panel below carries the detail either way.
      if (allGood(res)) onSaved(res, res.account_email);
      else onError("Credentials accepted, but Zoom is not fully connected yet — see the status below.");
    } catch (e) {
      setResult(null);
      onError(e instanceof Error ? e.message : "Failed to save Zoom credentials");
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    setTesting(true);
    try {
      const res = await testZoomConnection();
      setResult(res);
      // The detail lives in the status panel below; the banner just says which
      // way it went, so a partial pass is not announced as a flat success.
      if (allGood(res)) onSaved(res, res.account_email);
      else onError("Zoom is not fully connected yet — see the status below.");
    } catch (e) {
      onError(e instanceof Error ? e.message : "Zoom test failed");
    } finally {
      setTesting(false);
    }
  }

  async function handleDisconnect() {
    if (!confirm("Disconnect Zoom? Webinars already synced stay, but nothing new will sync.")) return;
    try {
      await deleteZoomCredential();
      setAccountId("");
      setClientId("");
      setClientSecret("");
      onDisconnected();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to disconnect Zoom");
    }
  }

  return (
    <div className="space-y-5 max-w-3xl">
      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4">
        <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 mb-1">
          1 · Create the Zoom app
        </h2>
        <p className="text-xs text-zinc-500 mb-3">
          Zoom needs an app before it will hand out webinar data. This is a one-off, and needs
          admin rights on the Zoom account.
        </p>
        <ol className="text-xs text-zinc-600 dark:text-zinc-400 space-y-1.5 list-decimal pl-4">
          <li>
            Go to{" "}
            <a
              href="https://marketplace.zoom.us/develop/create"
              target="_blank"
              rel="noreferrer"
              className="text-sky-600 dark:text-sky-400 hover:underline"
            >
              marketplace.zoom.us
            </a>{" "}
            → Develop → Build App → <strong>Server-to-Server OAuth</strong>.
          </li>
          <li>Give it any name (e.g. &ldquo;Webinar Studio&rdquo;).</li>
          <li>
            On <strong>App Credentials</strong>, copy the Account ID, Client ID and Client Secret
            into the form below.
          </li>
          <li>On the <strong>Scopes</strong> tab, add every scope in step 2.</li>
          <li>
            On <strong>Activation</strong>, click <strong>Activate your app</strong>. Scopes added
            later need the app re-activated.
          </li>
        </ol>
      </section>

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4">
        <div className="flex items-start justify-between gap-3 mb-1">
          <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            2 · Add these scopes
          </h2>
          <CopyButton value={scopeList} label="Copy all" className="text-[11px] px-2 py-1" />
        </div>
        <p className="text-xs text-zinc-500 mb-3">
          Zoom shows either the granular or the classic names depending on how old the app is —
          add whichever set your Scopes tab offers.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
                <th className="py-1.5 pr-3 font-medium">Scope</th>
                <th className="py-1.5 pr-3 font-medium">Classic</th>
                <th className="py-1.5 font-medium">What it&apos;s for</th>
              </tr>
            </thead>
            <tbody>
              {scopes.map((s) => {
                // Zoom names the scopes it wanted; flag those rows so the fix is
                // obvious instead of leaving the user to diff two lists by eye.
                const missing = missingScopes.includes(s.scope) || missingScopes.includes(s.classic);
                return (
                  <tr
                    key={s.scope}
                    className={
                      "border-b border-zinc-100 dark:border-zinc-900 " +
                      (missing ? "bg-red-500/5" : "")
                    }
                  >
                    <td className="py-1.5 pr-3 font-mono text-[11px] text-zinc-800 dark:text-zinc-200 whitespace-nowrap">
                      <span className="inline-flex items-center gap-1.5">
                        {missing && <span className="text-red-500" title="Zoom says this one is missing">●</span>}
                        {s.scope}
                        <CopyButton value={s.scope} />
                      </span>
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-[11px] text-zinc-500 whitespace-nowrap">
                      <span className="inline-flex items-center gap-1.5">
                        {s.classic}
                        <CopyButton value={s.classic} />
                      </span>
                    </td>
                    <td className="py-1.5 text-zinc-600 dark:text-zinc-400">{s.why}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="mt-3 text-[11px] text-zinc-500 space-y-1">
          <p>
            The account needs a plan that includes <strong>Webinars</strong>, and the report scopes
            need Pro or above. Without <code className="font-mono">report:read:*</code> everything
            still syncs, but watch time comes back empty and the 10 / 30 minute metrics stay at zero.
          </p>
          <p>
            Zoom has no API for recording views, so <strong>replay is not tracked</strong> for Zoom
            webinars — those columns stay blank rather than showing a misleading zero.
          </p>
        </div>
      </section>

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4">
        <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 mb-3">
          3 · Connect
        </h2>
        <div className="space-y-3">
          <Field label="Account ID" value={accountId} onChange={setAccountId}
            placeholder="abc123XYZ_defGHI" status={credStatus} />
          <Field label="Client ID" value={clientId} onChange={setClientId}
            placeholder="AbCdEfGhIjKlMnOp" status={credStatus} />
          <Field
            label="Client Secret"
            value={clientSecret}
            onChange={setClientSecret}
            type="password"
            placeholder={view?.client_secret_masked ?? "••••••••"}
            status={credStatus}
            hint={
              view?.configured
                ? "Stored. Leave blank to keep the current secret — type a new one to replace it."
                : undefined
            }
          />
        </div>

        {tested && (
          <div className="mt-4 rounded-md border border-zinc-200 dark:border-zinc-800 overflow-hidden">
            <StatusRow
              ok={credsOk}
              label="Credentials"
              detail={
                credsOk
                  ? "Account ID, Client ID and Client Secret all accepted by Zoom."
                  : view?.credential_error
                    || "Zoom rejected them. One of the three is wrong, or the app is not Activated."
              }
            />
            {/* Zoom mints a token from all three secrets at once, so it cannot
                say which single value is wrong — but a successful mint does
                prove all three are right. */}
            {credsOk && (view?.checks ?? []).map((c) => (
              <StatusRow
                key={c.endpoint}
                ok={c.ok}
                label={c.name}
                detail={
                  c.ok
                    ? c.endpoint
                    : c.missing_scopes.length
                      ? `Missing scope: ${c.missing_scopes.join(" or ")}`
                      : c.error || "Failed"
                }
                extra={c.missing_scopes.length ? c.missing_scopes[0] : undefined}
              />
            ))}
            {credsOk && missingScopes.length > 0 && (
              <div className="px-3 py-2 text-[11px] text-zinc-600 dark:text-zinc-400 bg-amber-500/5 border-t border-zinc-200 dark:border-zinc-800">
                Add the flagged scope{missingScopes.length === 1 ? "" : "s"} above in the Zoom
                Marketplace, click <strong>Activate your app</strong> again, then hit{" "}
                <strong>Test connection</strong>. No need to re-enter the secret.
              </div>
            )}
          </div>
        )}
        <div className="flex items-center gap-2 mt-4">
          <button
            onClick={handleSave}
            disabled={saving || !accountId.trim() || !clientId.trim() || !clientSecret.trim()}
            className="px-3 py-1.5 text-xs font-medium rounded-md bg-sky-600 text-white hover:bg-sky-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {saving ? "Verifying…" : status?.configured ? "Update credentials" : "Connect Zoom"}
          </button>
          {view?.configured && (
            <button
              onClick={handleTest}
              disabled={testing || saving}
              className="px-3 py-1.5 text-xs font-medium rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-40"
            >
              {testing ? "Testing…" : "Test connection"}
            </button>
          )}
          {view?.configured && (
            <button
              onClick={handleDisconnect}
              className="px-3 py-1.5 text-xs font-medium rounded-md border border-red-500/30 text-red-500 hover:bg-red-500/10"
            >
              Disconnect
            </button>
          )}
        </div>
        <p className="mt-2 text-[11px] text-zinc-500">
          Saving checks the credentials against Zoom first, so a typo or an un-activated app is
          caught here rather than showing up later as an empty webinar list.
        </p>
      </section>
    </div>
  );
}

/** "unknown" until a check has run — an untested field must not claim to be
 *  good, and must not be accused of being wrong either. */
type FieldStatus = "unknown" | "ok" | "bad";

function StatusRow({ ok, label, detail }: {
  ok: boolean; label: string; detail: string; extra?: string;
}) {
  return (
    <div className="flex items-start gap-2 px-3 py-2 border-b last:border-b-0 border-zinc-200 dark:border-zinc-800">
      <span className={"mt-0.5 text-xs " + (ok ? "text-emerald-500" : "text-red-500")}>
        {ok ? "✓" : "✕"}
      </span>
      <div className="min-w-0">
        <div className="text-xs text-zinc-800 dark:text-zinc-200">{label}</div>
        <div className="text-[11px] text-zinc-500 break-words">{detail}</div>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
  hint,
  status = "unknown",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  hint?: string;
  status?: FieldStatus;
}) {
  return (
    <div>
      <label className="block text-[10px] uppercase tracking-wide text-zinc-500 mb-1">
        <span className="inline-flex items-center gap-1.5">
          {label}
          {status === "ok" && (
            <span className="text-emerald-500 normal-case tracking-normal" title="Accepted by Zoom">
              ✓ accepted
            </span>
          )}
          {status === "bad" && (
            <span className="text-red-500 normal-case tracking-normal" title="Zoom rejected the credentials">
              ✕ check this
            </span>
          )}
        </span>
      </label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={
          "w-full px-2.5 py-1.5 text-xs rounded-md border bg-white dark:bg-zinc-900 text-zinc-900 dark:text-zinc-100 font-mono " +
          (status === "ok"
            ? "border-emerald-500/40"
            : status === "bad"
              ? "border-red-500/40"
              : "border-zinc-300 dark:border-zinc-700")
        }
      />
      {hint && <p className="mt-1 text-[10px] text-zinc-500">{hint}</p>}
    </div>
  );
}

/* ── Webinars ──────────────────────────────────────────────────────────── */

function WebinarsTab({
  configured,
  onError,
  onMessage,
}: {
  configured: boolean;
  onError: (m: string) => void;
  onMessage: (m: string) => void;
}) {
  const [rows, setRows] = useState<ZoomWebinar[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { broadcasts } = await fetchZoomWebinars({ limit: 500 });
      setRows(broadcasts);
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to load Zoom webinars");
    } finally {
      setLoading(false);
    }
    // onError is a fresh closure each render; depending on it would reload in a loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleRefresh() {
    setBusy("refresh");
    try {
      const { count } = await refreshZoomWebinars();
      onMessage(`Refreshed ${count} Zoom webinar${count === 1 ? "" : "s"}.`);
      await load();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to refresh");
    } finally {
      setBusy(null);
    }
  }

  async function handleSync(id: string) {
    setBusy(id);
    try {
      await syncZoomWebinar(id);
      onMessage("Sync started — progress is on the Sync page.");
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to start sync");
    } finally {
      setBusy(null);
    }
  }

  async function handleSyncAll() {
    setBusy("all");
    try {
      const { broadcasts_queued } = await syncAllZoomWebinars();
      onMessage(`Queued ${broadcasts_queued} webinar${broadcasts_queued === 1 ? "" : "s"}.`);
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to start sync-all");
    } finally {
      setBusy(null);
    }
  }

  if (!configured) {
    return <p className="text-xs text-zinc-500">Connect Zoom on the Setup tab first.</p>;
  }

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <button
          onClick={handleRefresh}
          disabled={busy !== null}
          className="px-3 py-1.5 text-xs font-medium rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-40"
        >
          {busy === "refresh" ? "Refreshing…" : "Refresh webinar list"}
        </button>
        <button
          onClick={handleSyncAll}
          disabled={busy !== null || rows.length === 0}
          className="px-3 py-1.5 text-xs font-medium rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-40"
        >
          {busy === "all" ? "Queueing…" : "Sync all registrants"}
        </button>
      </div>

      {loading ? (
        <p className="text-xs text-zinc-500">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-zinc-500">
          No Zoom webinars cached yet — hit &ldquo;Refresh webinar list&rdquo;.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
                <th className="py-1.5 pr-3 font-medium">Webinar</th>
                <th className="py-1.5 pr-3 font-medium">Starts</th>
                <th className="py-1.5 pr-3 font-medium text-right">Registered</th>
                <th className="py-1.5 pr-3 font-medium text-right">Attended</th>
                <th className="py-1.5 pr-3 font-medium text-right">Synced</th>
                <th className="py-1.5 pr-3 font-medium">Last sync</th>
                <th className="py-1.5 font-medium" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.broadcast_id} className="border-b border-zinc-100 dark:border-zinc-900">
                  <td className="py-1.5 pr-3">
                    <div className="text-zinc-800 dark:text-zinc-200">{r.internal_title || r.name}</div>
                    <div className="font-mono text-[10px] text-zinc-500">{r.broadcast_id}</div>
                  </td>
                  <td className="py-1.5 pr-3 text-zinc-600 dark:text-zinc-400 whitespace-nowrap">
                    {r.starts_at ? new Date(r.starts_at).toLocaleString() : "—"}
                  </td>
                  <td className="py-1.5 pr-3 text-right text-zinc-600 dark:text-zinc-400">
                    {r.subscriptions_count}
                  </td>
                  <td className="py-1.5 pr-3 text-right text-zinc-600 dark:text-zinc-400">
                    {r.live_viewers_count}
                  </td>
                  <td className="py-1.5 pr-3 text-right text-zinc-600 dark:text-zinc-400">
                    {r.synced_subscriber_count}
                  </td>
                  <td className="py-1.5 pr-3 text-zinc-500 whitespace-nowrap">
                    {r.last_synced_at ? new Date(r.last_synced_at).toLocaleString() : "Never"}
                  </td>
                  <td className="py-1.5 text-right">
                    <button
                      onClick={() => handleSync(r.broadcast_id)}
                      disabled={busy !== null}
                      className="px-2 py-1 text-[11px] rounded border border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-40"
                    >
                      {busy === r.broadcast_id ? "…" : "Sync"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ── Registrants ───────────────────────────────────────────────────────── */

function RegistrantsTab({ onError }: { onError: (m: string) => void }) {
  const [rows, setRows] = useState<ZoomRegistrant[]>([]);
  const [total, setTotal] = useState(0);
  const [q, setQ] = useState("");
  const [broadcastId, setBroadcastId] = useState("");
  const [webinars, setWebinars] = useState<ZoomWebinar[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (search: string, bid: string) => {
    setLoading(true);
    try {
      const res = await fetchZoomRegistrants({
        q: search || undefined,
        broadcast_id: bid || undefined,
        limit: 200,
      });
      setRows(res.subscribers);
      setTotal(res.total);
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to load registrants");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    load("", "");
    // Populate the filter dropdown; a failure here only costs the filter, so it
    // must not blank the table.
    fetchZoomWebinars({ limit: 500 })
      .then((res) => setWebinars(res.broadcasts))
      .catch(() => setWebinars([]));
  }, [load]);

  return (
    <div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          load(q.trim(), broadcastId);
        }}
        className="flex flex-wrap gap-2 mb-3 items-center"
      >
        <select
          value={broadcastId}
          onChange={(e) => {
            setBroadcastId(e.target.value);
            load(q.trim(), e.target.value);
          }}
          className="px-2.5 py-1.5 text-xs rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 text-zinc-900 dark:text-zinc-100 max-w-xs"
        >
          <option value="">All webinars</option>
          {webinars.map((w) => (
            <option key={w.broadcast_id} value={w.broadcast_id}>
              {(w.internal_title || w.name)}
              {w.starts_at ? ` · ${new Date(w.starts_at).toLocaleDateString()}` : ""}
            </option>
          ))}
        </select>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search name or email…"
          className="px-2.5 py-1.5 text-xs rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 text-zinc-900 dark:text-zinc-100 w-64"
        />
        <button
          type="submit"
          className="px-3 py-1.5 text-xs font-medium rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800"
        >
          Search
        </button>
        <a
          href={zoomRegistrantsCsvUrl({
            broadcast_id: broadcastId || undefined,
            q: q.trim() || undefined,
          })}
          className="px-3 py-1.5 text-xs font-medium rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800"
        >
          Export CSV
        </a>
      </form>

      {loading ? (
        <p className="text-xs text-zinc-500">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-zinc-500">No Zoom registrants synced yet.</p>
      ) : (
        <>
          <p className="text-[11px] text-zinc-500 mb-2">
            Showing {rows.length} of {total}
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
                  <th className="py-1.5 pr-3 font-medium">Email</th>
                  <th className="py-1.5 pr-3 font-medium">Name</th>
                  <th className="py-1.5 pr-3 font-medium">Registered</th>
                  <th className="py-1.5 pr-3 font-medium">Attendance</th>
                  <th className="py-1.5 pr-3 font-medium text-right">Watched</th>
                  <th className="py-1.5 font-medium">Webinar</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b border-zinc-100 dark:border-zinc-900">
                    <td className="py-1.5 pr-3 text-zinc-800 dark:text-zinc-200">{r.email}</td>
                    <td className="py-1.5 pr-3 text-zinc-600 dark:text-zinc-400">
                      {[r.first_name, r.last_name].filter(Boolean).join(" ") || "—"}
                    </td>
                    <td className="py-1.5 pr-3 text-zinc-500 whitespace-nowrap">
                      {r.subscribed_at ? new Date(r.subscribed_at).toLocaleDateString() : "—"}
                    </td>
                    <td className="py-1.5 pr-3">
                      {r.watched_live ? (
                        <span className="text-emerald-500">✓ Live</span>
                      ) : (
                        <span className="text-zinc-500">No-show</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-right text-zinc-600 dark:text-zinc-400">
                      {r.minutes_viewing != null ? `${r.minutes_viewing}m` : "—"}
                    </td>
                    <td className="py-1.5 font-mono text-[10px] text-zinc-500">{r.broadcast_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
