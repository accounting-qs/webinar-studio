"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  createSkarpeCredential,
  deleteSkarpeCredential,
  fetchSkarpeCredentialInfo,
  fetchSkarpeCredentials,
  updateSkarpeCredential,
  type ApiSkarpeCredential,
  type ApiSkarpeCredentialInfo,
} from "@/lib/api";

/** Live whoami result per credential id; null = probe failed. */
type InfoMap = Record<string, ApiSkarpeCredentialInfo | null>;

export function SkarpeConnectorPage() {
  const [rows, setRows] = useState<ApiSkarpeCredential[]>([]);
  const [info, setInfo] = useState<InfoMap>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchSkarpeCredentials();
      setRows(data.credentials);
      // Probe each workspace live, independently — one dead instance must
      // not blank the others' chips.
      data.credentials.forEach((c) => {
        fetchSkarpeCredentialInfo(c.id)
          .then((i) => setInfo((prev) => ({ ...prev, [c.id]: i })))
          .catch(() => setInfo((prev) => ({ ...prev, [c.id]: null })));
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load Skarpe credentials");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleCreate() {
    setError(null);
    setMessage(null);
    setCreating(true);
    try {
      const created = await createSkarpeCredential({
        name: name.trim(),
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
      });
      setMessage(
        created.key_name
          ? `Connected "${created.name}" — workspace "${created.key_name}"${created.timezone ? ` (${created.timezone})` : ""}.`
          : `Connected "${created.name}".`,
      );
      setName("");
      setBaseUrl("");
      setApiKey("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add Skarpe workspace");
    } finally {
      setCreating(false);
    }
  }

  async function handleRename(row: ApiSkarpeCredential) {
    const newName = prompt(`Rename "${row.name}" to:`, row.name);
    if (!newName || newName.trim() === row.name) return;
    setError(null);
    try {
      await updateSkarpeCredential(row.id, { name: newName.trim() });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to rename");
    }
  }

  async function handleReplaceKey(row: ApiSkarpeCredential) {
    const newKey = prompt(
      `Paste the new API key for "${row.name}" (${row.base_url}). It will be verified against Skarpe before saving.`,
    );
    if (!newKey?.trim()) return;
    setError(null);
    try {
      await updateSkarpeCredential(row.id, { api_key: newKey.trim() });
      setMessage(`Replaced the API key for "${row.name}".`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to replace API key");
    }
  }

  async function handleDelete(row: ApiSkarpeCredential) {
    if (
      !confirm(
        `Remove the "${row.name}" Skarpe workspace? Campaigns already created in Skarpe are untouched.`,
      )
    )
      return;
    setError(null);
    try {
      await deleteSkarpeCredential(row.id);
      setMessage(`Removed "${row.name}".`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove workspace");
    }
  }

  if (loading) {
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
        <div className="w-8 h-8 rounded-md bg-indigo-500/15 flex items-center justify-center">
          <svg className="w-4 h-4 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
        </div>
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Skarpe</h1>
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

      <section className="mb-6 rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 p-4">
        <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 mb-1">Add workspace</h2>
        <p className="text-xs text-zinc-500 mb-3">
          One row per Skarpe workspace. Each API key only works on its own backend, so the URL is part
          of the credential — production keys use{" "}
          <code className="font-mono">https://backend.skarpe.io/mcp/user</code>, staging keys{" "}
          <code className="font-mono">https://backend-staging.skarpe.io/mcp/user</code>. The key is
          verified against Skarpe before it is saved.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-[1fr_1.5fr_1.5fr_auto] gap-2 items-center">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Internal name, e.g. Production — Alex"
            className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
          />
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://backend.skarpe.io/mcp/user"
            className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-xs font-mono text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
          />
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="API key (sku_live_…)"
            className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-xs font-mono text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
          />
          <button
            onClick={handleCreate}
            disabled={!name.trim() || !baseUrl.trim() || !apiKey.trim() || creating}
            className="px-3 py-1.5 text-xs rounded-md bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
          >
            {creating ? "Verifying…" : "Add workspace"}
          </button>
        </div>
      </section>

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 p-4">
        <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 mb-3">
          Workspaces{" "}
          {rows.length > 0 && <span className="text-zinc-500 font-normal">({rows.length})</span>}
        </h2>
        {rows.length === 0 ? (
          <p className="text-xs text-zinc-500">
            No workspaces connected yet. Add one above to enable “Create Draft Campaigns in Skarpe”
            on the Planning page.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
                  <th className="py-2 pr-3 font-semibold">Name</th>
                  <th className="py-2 pr-3 font-semibold">Endpoint</th>
                  <th className="py-2 pr-3 font-semibold">API key</th>
                  <th className="py-2 pr-3 font-semibold">Workspace</th>
                  <th className="py-2 font-semibold text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const i = info[row.id];
                  return (
                    <tr key={row.id} className="border-b border-zinc-100 dark:border-zinc-800/60">
                      <td className="py-2 pr-3 font-semibold text-zinc-800 dark:text-zinc-200">
                        {row.name}
                      </td>
                      <td className="py-2 pr-3 font-mono text-zinc-500">{row.base_url}</td>
                      <td className="py-2 pr-3 font-mono text-zinc-500">{row.api_key_masked}</td>
                      <td className="py-2 pr-3">
                        {i === undefined ? (
                          <span className="text-zinc-500">checking…</span>
                        ) : i === null ? (
                          <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-red-500/10 text-red-500 border-red-500/30">
                            Unreachable
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1.5 flex-wrap">
                            <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-emerald-500/15 text-emerald-500 border-emerald-500/30">
                              {i.key_name ?? "Connected"}
                            </span>
                            {i.timezone && <span className="text-zinc-500">{i.timezone}</span>}
                            <span className="text-zinc-500">{i.permissions.join(" · ")}</span>
                          </span>
                        )}
                      </td>
                      <td className="py-2 text-right whitespace-nowrap">
                        <button
                          onClick={() => handleRename(row)}
                          className="px-2 py-1 rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 mr-2"
                        >
                          Rename
                        </button>
                        <button
                          onClick={() => handleReplaceKey(row)}
                          className="px-2 py-1 rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 mr-2"
                        >
                          Replace key
                        </button>
                        <button
                          onClick={() => handleDelete(row)}
                          className="px-2 py-1 rounded-md border border-red-500/40 text-red-500 hover:bg-red-500/10"
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-[11px] text-zinc-500 mt-3">
          The Planning page’s “Create Draft Campaigns in Skarpe” bulk action lets you pick any of
          these workspaces as the target. Drafts never send anything — launching stays a manual step
          inside Skarpe.
        </p>
      </section>
    </div>
  );
}
