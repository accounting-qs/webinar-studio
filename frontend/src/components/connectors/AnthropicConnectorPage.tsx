"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  fetchAnthropicStatus,
  saveAnthropicApiKey,
  deleteAnthropicApiKey,
  type AnthropicCredentialStatus,
} from "@/lib/api";

export function AnthropicConnectorPage() {
  const [status, setStatus] = useState<AnthropicCredentialStatus | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(true);

  useEffect(() => {
    fetchAnthropicStatus()
      .then(setStatus)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load Anthropic status"))
      .finally(() => setLoadingStatus(false));
  }, []);

  async function handleSave() {
    setError(null);
    setMessage(null);
    setSaving(true);
    try {
      const s = await saveAnthropicApiKey(apiKeyInput.trim());
      setStatus(s);
      setApiKeyInput("");
      setMessage("Anthropic API key saved.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save Anthropic key");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!confirm("Remove Anthropic API key? The Statistics chat assistant will stop working until you reconnect.")) return;
    try {
      await deleteAnthropicApiKey();
      setStatus({ configured: false });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete Anthropic key");
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
        <div className="w-8 h-8 rounded-md bg-orange-500/15 flex items-center justify-center">
          <svg className="w-4 h-4 text-orange-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
          </svg>
        </div>
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">
          Anthropic (Claude)
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
        <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 mb-1">Anthropic API</h2>
        <p className="text-xs text-zinc-500 mb-4">
          Powers the chat assistant on the Statistics page. The assistant uses{" "}
          <span className="font-mono">claude-opus-4-7</span> with adaptive thinking and prompt caching
          so follow-up questions about the same loaded data are cheap. Your key is stored on the
          server and used only for chat requests.
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
              Anthropic API Key (sk-ant-…)
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
                href="https://console.anthropic.com/settings/keys"
                target="_blank"
                rel="noreferrer noopener"
                className="text-violet-500 hover:text-violet-400"
              >
                console.anthropic.com/settings/keys
              </a>
              .
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
