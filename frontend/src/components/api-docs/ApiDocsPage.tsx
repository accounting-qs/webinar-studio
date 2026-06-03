"use client";

import { useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const ENDPOINT = `${API_BASE}/public/contact-counts`;

const RESPONSE_EXAMPLE = `{
  "total_contacts": 1726111,
  "available_contacts": 291763,
  "disqualified_contacts": 10850
}`;

const CURL_EXAMPLE = `curl -s "${ENDPOINT}" \\
  -H "X-API-Key: <YOUR_API_KEY>"`;

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* clipboard unavailable */
        }
      }}
      className="px-2 py-1 text-[11px] font-semibold rounded-md text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 hover:bg-zinc-100 dark:hover:bg-zinc-800/60 transition-colors"
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function CodeBlock({ code }: { code: string }) {
  return (
    <div className="relative group">
      <div className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity">
        <CopyButton text={code} />
      </div>
      <pre className="bg-zinc-900 text-zinc-100 rounded-lg p-4 overflow-x-auto text-[13px] leading-relaxed font-mono ring-1 ring-zinc-800">
        <code>{code}</code>
      </pre>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 tracking-tight uppercase tracking-wider mt-8 mb-3">
      {children}
    </h2>
  );
}

export function ApiDocsPage() {
  return (
    <main className="flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0 overflow-auto">
      <div className="px-6 py-5 max-w-[860px] mx-auto">
        {/* ── Header ─────────────────────────────────────────────────── */}
        <div className="mb-2">
          <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">
            API Docs
          </h1>
          <p className="text-sm text-zinc-500 mt-1">
            Read-only endpoints other apps can use to fetch numbers from Webinar Studio.
          </p>
        </div>

        {/* ── Contact Counts endpoint ────────────────────────────────── */}
        <div className="mt-6 rounded-xl bg-white dark:bg-zinc-900 ring-1 ring-zinc-200 dark:ring-zinc-800 p-6">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-100 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-400">
              GET
            </span>
            <span className="font-mono text-sm text-zinc-800 dark:text-zinc-200">
              /public/contact-counts
            </span>
          </div>
          <p className="text-sm text-zinc-600 dark:text-zinc-400 mt-3">
            Returns the total number of contacts, the number available for outreach,
            and the number in the Disqualified bucket. All values are non-negative
            integers.
          </p>

          <SectionTitle>Full URL</SectionTitle>
          <CodeBlock code={ENDPOINT} />

          <SectionTitle>Authentication</SectionTitle>
          <p className="text-sm text-zinc-600 dark:text-zinc-400 mb-3">
            Send the API key in the <code className="font-mono text-[13px] px-1 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800">X-API-Key</code> header.
            Keep the key secret — call this endpoint from your server, not from
            browser JavaScript. Do not put the key in the URL or query string.
          </p>
          <CodeBlock code={`X-API-Key: <YOUR_API_KEY>`} />
          <p className="text-xs text-zinc-500 mt-2">
            The key is configured in the backend environment as{" "}
            <code className="font-mono px-1 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800">STATS_API_KEY</code>.
            To rotate it, change that value and update the consuming app — no code change needed.
          </p>

          <SectionTitle>Response (200)</SectionTitle>
          <CodeBlock code={RESPONSE_EXAMPLE} />

          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="text-left text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
                  <th className="py-2 pr-4 font-semibold">Field</th>
                  <th className="py-2 font-semibold">Meaning</th>
                </tr>
              </thead>
              <tbody className="text-zinc-700 dark:text-zinc-300">
                <tr className="border-b border-zinc-100 dark:border-zinc-800/60">
                  <td className="py-2 pr-4 font-mono text-[13px] whitespace-nowrap">total_contacts</td>
                  <td className="py-2">All contacts in the system (every status and bucket, including disqualified).</td>
                </tr>
                <tr className="border-b border-zinc-100 dark:border-zinc-800/60">
                  <td className="py-2 pr-4 font-mono text-[13px] whitespace-nowrap">available_contacts</td>
                  <td className="py-2">Contacts available for outreach, excluding the Disqualified bucket. Matches the Planning page &quot;available&quot; number.</td>
                </tr>
                <tr>
                  <td className="py-2 pr-4 font-mono text-[13px] whitespace-nowrap">disqualified_contacts</td>
                  <td className="py-2">Contacts in the Disqualified bucket.</td>
                </tr>
              </tbody>
            </table>
          </div>

          <SectionTitle>Example</SectionTitle>
          <CodeBlock code={CURL_EXAMPLE} />

          <SectionTitle>Errors</SectionTitle>
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="text-left text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
                  <th className="py-2 pr-4 font-semibold">Status</th>
                  <th className="py-2 pr-4 font-semibold">Body</th>
                  <th className="py-2 font-semibold">Cause</th>
                </tr>
              </thead>
              <tbody className="text-zinc-700 dark:text-zinc-300">
                <tr className="border-b border-zinc-100 dark:border-zinc-800/60">
                  <td className="py-2 pr-4 font-mono text-[13px]">401</td>
                  <td className="py-2 pr-4 font-mono text-[12px]">{`{"detail":"Invalid or missing API key"}`}</td>
                  <td className="py-2">The X-API-Key header is missing or incorrect.</td>
                </tr>
                <tr>
                  <td className="py-2 pr-4 font-mono text-[13px]">503</td>
                  <td className="py-2 pr-4 font-mono text-[12px]">{`{"detail":"Stats API key not configured"}`}</td>
                  <td className="py-2">The server has no API key configured yet.</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </main>
  );
}
