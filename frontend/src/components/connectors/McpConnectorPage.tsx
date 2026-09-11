"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  createMcpConnector,
  deleteMcpConnector,
  fetchMcpConnectors,
  mcpEndpointUrl,
  rotateMcpConnector,
  updateMcpConnector,
  type McpConnectorList,
  type McpConnectorRow,
} from "@/lib/api";

/** A freshly generated token, held in component state only. The server stores
 *  just its sha256, so this is the one and only time it can be shown. */
type NewToken = { name: string; token: string };

export function McpConnectorPage() {
  const [data, setData] = useState<McpConnectorList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [allowWrites, setAllowWrites] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newToken, setNewToken] = useState<NewToken | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await fetchMcpConnectors());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load MCP connectors");
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
      const res = await createMcpConnector(name.trim(), allowWrites);
      setNewToken({ name: res.connector.name, token: res.token });
      setName("");
      setAllowWrites(false);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create connector");
    } finally {
      setCreating(false);
    }
  }

  async function handlePatch(row: McpConnectorRow, patch: Partial<McpConnectorRow>) {
    setError(null);
    try {
      await updateMcpConnector(row.id, patch);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update connector");
    }
  }

  async function handleRotate(row: McpConnectorRow) {
    if (
      !confirm(
        `Issue a new token for "${row.name}"? The current token stops working immediately and ` +
          `whatever is using it will need the new one.`,
      )
    )
      return;
    setError(null);
    try {
      const res = await rotateMcpConnector(row.id);
      setNewToken({ name: res.connector.name, token: res.token });
      setMessage(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to rotate token");
    }
  }

  async function handleDelete(row: McpConnectorRow) {
    if (!confirm(`Revoke "${row.name}" permanently? Its token stops working immediately.`)) return;
    setError(null);
    try {
      await deleteMcpConnector(row.id);
      setMessage(`Revoked "${row.name}".`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to revoke connector");
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  const endpoint = mcpEndpointUrl();
  const rows = data?.connectors ?? [];

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
        <div className="w-8 h-8 rounded-md bg-teal-500/15 flex items-center justify-center">
          <svg className="w-4 h-4 text-teal-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
        </div>
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">MCP Server</h1>
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

      {newToken && (
        <section className="mb-6 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4">
          <h2 className="text-sm font-bold text-amber-600 dark:text-amber-400 mb-1">
            Copy this token now — it will not be shown again
          </h2>
          <p className="text-xs text-zinc-600 dark:text-zinc-400 mb-3">
            Only a hash is stored on the server, so this is the one and only time the token for{" "}
            <span className="font-semibold">{newToken.name}</span> is readable. If you lose it, rotate
            the connector for a new one.
          </p>
          <div className="flex gap-2 items-center mb-3">
            <code className="flex-1 font-mono text-xs bg-white dark:bg-zinc-900 border border-amber-500/30 rounded-md px-3 py-2 text-zinc-800 dark:text-zinc-200 break-all">
              {newToken.token}
            </code>
            <CopyButton value={newToken.token} label="Copy token" />
          </div>
          <details className="text-xs text-zinc-600 dark:text-zinc-400">
            <summary className="cursor-pointer font-semibold">Connector config (Grok / xAI)</summary>
            <div className="mt-2 flex gap-2 items-start">
              <pre className="flex-1 font-mono text-[11px] bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-md p-3 overflow-x-auto">
{`{
  "type": "mcp",
  "server_url": "${endpoint}",
  "server_label": "webinarstudio",
  "authorization": "${newToken.token}"
}`}
              </pre>
              <CopyButton
                value={`{\n  "type": "mcp",\n  "server_url": "${endpoint}",\n  "server_label": "webinarstudio",\n  "authorization": "${newToken.token}"\n}`}
                label="Copy config"
              />
            </div>
          </details>
          <button
            onClick={() => setNewToken(null)}
            className="mt-3 px-3 py-1.5 text-xs rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            I&apos;ve saved it
          </button>
        </section>
      )}

      <section className="mb-6 rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 p-4">
        <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 mb-1">Endpoint</h2>
        <p className="text-xs text-zinc-500 mb-3">
          Give an agent this URL plus a token below. It exposes {data?.tool_count ?? 0} read tools over
          your statistics, planning, contacts and calendar data — the same numbers the app&apos;s own
          pages show. The endpoint must be reachable from the public internet: xAI and Anthropic call
          it from their servers, not from your browser.
        </p>
        <div className="flex gap-2 items-center">
          <code className="flex-1 font-mono text-xs bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-md px-3 py-2 text-zinc-800 dark:text-zinc-200">
            {endpoint}
          </code>
          <CopyButton value={endpoint} label="Copy URL" />
          <span
            className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${
              data?.configured
                ? "bg-emerald-500/15 text-emerald-500 border-emerald-500/30"
                : "bg-zinc-100 dark:bg-zinc-800/60 text-zinc-500 border-zinc-200 dark:border-zinc-700/60"
            }`}
          >
            {data?.configured ? "Live" : "No token — returns 503"}
          </span>
        </div>
      </section>

      <section className="mb-6 rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 p-4">
        <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 mb-1">New connector</h2>
        <p className="text-xs text-zinc-500 mb-3">
          One token per agent, so you can revoke one without knocking out the others. The name is
          just a label for you — the server has no way to know what is on the far end of a token.
        </p>
        <div className="flex flex-wrap gap-2 items-center">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. grok, claude-desktop, reporting-bot"
            className="flex-1 min-w-[220px] bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
          />
          <label className="flex items-center gap-1.5 text-xs text-zinc-600 dark:text-zinc-300">
            <input
              type="checkbox"
              checked={allowWrites}
              onChange={(e) => setAllowWrites(e.target.checked)}
              className="accent-violet-600"
            />
            Allow actions that cost money or send email
          </label>
          <button
            onClick={handleCreate}
            disabled={!name.trim() || creating}
            className="px-3 py-1.5 text-xs rounded-md bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {creating ? "Generating…" : "Generate token"}
          </button>
        </div>
        <p className="text-[11px] text-zinc-500 mt-2">
          Leave the checkbox off unless you want this agent to be able to generate AI webinar reports
          (~$0.14 each), trigger a statistics recompute, or send the report email. Those tools also
          require the agent to pass an explicit confirmation, but this switch is the gate that
          matters — leave it off and they cannot run at all.
        </p>
      </section>

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 p-4">
        <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 mb-3">
          Connectors {rows.length > 0 && <span className="text-zinc-500 font-normal">({rows.length})</span>}
        </h2>
        {rows.length === 0 ? (
          <p className="text-xs text-zinc-500">
            No connectors yet. Until one exists, <code className="font-mono">/mcp</code> answers 503
            to everyone.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
                  <th className="py-2 pr-3 font-semibold">Name</th>
                  <th className="py-2 pr-3 font-semibold">Token</th>
                  <th className="py-2 pr-3 font-semibold">Status</th>
                  <th className="py-2 pr-3 font-semibold">Writes</th>
                  <th className="py-2 pr-3 font-semibold">Last used</th>
                  <th className="py-2 pr-3 font-semibold text-right">Calls</th>
                  <th className="py-2 font-semibold text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="border-b border-zinc-100 dark:border-zinc-800/60">
                    <td className="py-2 pr-3 font-semibold text-zinc-800 dark:text-zinc-200">{row.name}</td>
                    <td className="py-2 pr-3 font-mono text-zinc-500">{row.token_prefix}…</td>
                    <td className="py-2 pr-3">
                      <button
                        onClick={() => handlePatch(row, { enabled: !row.enabled })}
                        className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${
                          row.enabled
                            ? "bg-emerald-500/15 text-emerald-500 border-emerald-500/30"
                            : "bg-zinc-100 dark:bg-zinc-800/60 text-zinc-500 border-zinc-200 dark:border-zinc-700/60"
                        }`}
                        title={row.enabled ? "Click to disable" : "Click to enable"}
                      >
                        {row.enabled ? "Enabled" : "Disabled"}
                      </button>
                    </td>
                    <td className="py-2 pr-3">
                      <button
                        onClick={() => handlePatch(row, { allow_writes: !row.allow_writes })}
                        className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${
                          row.allow_writes
                            ? "bg-amber-500/15 text-amber-500 border-amber-500/30"
                            : "bg-zinc-100 dark:bg-zinc-800/60 text-zinc-500 border-zinc-200 dark:border-zinc-700/60"
                        }`}
                        title={row.allow_writes ? "Click to make read-only" : "Click to allow write tools"}
                      >
                        {row.allow_writes ? "Allowed" : "Read-only"}
                      </button>
                    </td>
                    <td className="py-2 pr-3 text-zinc-500">
                      {row.last_used_at ? new Date(row.last_used_at).toLocaleString() : "never"}
                    </td>
                    <td className="py-2 pr-3 text-right text-zinc-500 tabular-nums">{row.call_count}</td>
                    <td className="py-2 text-right whitespace-nowrap">
                      <button
                        onClick={() => handleRotate(row)}
                        className="px-2 py-1 rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 mr-2"
                      >
                        Rotate
                      </button>
                      <button
                        onClick={() => handleDelete(row)}
                        className="px-2 py-1 rounded-md border border-red-500/40 text-red-500 hover:bg-red-500/10"
                      >
                        Revoke
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-[11px] text-zinc-500 mt-3">
          <span className="font-semibold">Last used</span> is the only honest signal that a connector
          is actually live — a token that has never been used is either not configured on the far
          end or not working.
        </p>
      </section>
    </div>
  );
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* clipboard blocked — the value is selectable on screen */
        }
      }}
      className="px-3 py-1.5 text-xs rounded-md border border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 whitespace-nowrap"
    >
      {copied ? "Copied" : label}
    </button>
  );
}
