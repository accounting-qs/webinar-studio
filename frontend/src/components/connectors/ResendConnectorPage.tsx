"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  fetchResendStatus,
  saveResendApiKey,
  deleteResendApiKey,
  type ResendCredentialStatus,
} from "@/lib/api";

export function ResendConnectorPage() {
  const [status, setStatus] = useState<ResendCredentialStatus | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(true);

  useEffect(() => {
    fetchResendStatus()
      .then(setStatus)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load Resend status"))
      .finally(() => setLoadingStatus(false));
  }, []);

  async function handleSave() {
    setError(null);
    setMessage(null);
    setSaving(true);
    try {
      const s = await saveResendApiKey(apiKeyInput.trim());
      setStatus(s);
      setApiKeyInput("");
      setMessage("Resend API key saved.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save Resend key");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!confirm("Remove Resend API key? Weekly report emails will stop sending until you reconnect.")) return;
    try {
      await deleteResendApiKey();
      setStatus({ configured: false });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete Resend key");
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
        <div className="w-8 h-8 rounded-md bg-sky-500/15 flex items-center justify-center">
          <svg className="w-4 h-4 text-sky-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
          </svg>
        </div>
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">
          Resend
        </h1>
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

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 p-4">
        <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 mb-1">Resend API</h2>
        <p className="text-xs text-zinc-500 mb-4">
          Sends the weekly webinar report emails configured on the{" "}
          <Link href="/reports" className="text-violet-500 hover:text-violet-400">Reports</Link>{" "}
          page. The from address must be on a domain verified in your Resend account. Your key is
          stored on the server and used only for report sends.
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
              onClick={handleDelete}
              className="px-3 py-1.5 text-xs rounded-md border border-red-500/40 text-red-500 hover:bg-red-500/10"
            >
              Remove
            </button>
          </div>
        ) : (
          <div className="space-y-2">
            <label className="block text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">
              Resend API Key (re_…)
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
                onClick={handleSave}
                disabled={!apiKeyInput.trim() || saving}
                className="px-3 py-1.5 text-xs rounded-md bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {saving ? "Saving..." : "Save & Verify"}
              </button>
            </div>
            <p className="text-[11px] text-zinc-500">
              Get a key from{" "}
              <a
                href="https://resend.com/api-keys"
                target="_blank"
                rel="noreferrer noopener"
                className="text-violet-500 hover:text-violet-400"
              >
                resend.com/api-keys
              </a>
              .
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
