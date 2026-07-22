"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  fetchReportSettings,
  updateReportSettings,
  sendTestReport,
  fetchResendStatus,
  type ReportSettings,
  type TestReportResult,
  type ResendCredentialStatus,
} from "@/lib/api";

const DAYS = [
  { value: "mon", label: "Monday" },
  { value: "tue", label: "Tuesday" },
  { value: "wed", label: "Wednesday" },
  { value: "thu", label: "Thursday" },
  { value: "fri", label: "Friday" },
  { value: "sat", label: "Saturday" },
  { value: "sun", label: "Sunday" },
];

const MINUTES = [0, 15, 30, 45];

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

const inputCls =
  "bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 disabled:opacity-50";

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function ReportsPage() {
  const [settings, setSettings] = useState<ReportSettings | null>(null);
  const [resend, setResend] = useState<ResendCredentialStatus | null>(null);
  const [recipientsInput, setRecipientsInput] = useState("");
  const [fromInput, setFromInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [sending, setSending] = useState(false);
  const [testResult, setTestResult] = useState<TestReportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchReportSettings()
      .then((s) => {
        setSettings(s);
        setRecipientsInput(s.recipients.join(", "));
        setFromInput(s.from_address);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load report settings"))
      .finally(() => setLoading(false));
    fetchResendStatus()
      .then(setResend)
      .catch(() => setResend({ configured: false }));
  }, []);

  async function applyPatch(patch: Partial<ReportSettings>) {
    setError(null);
    setMessage(null);
    setSaving(true);
    try {
      const s = await updateReportSettings(patch);
      setSettings(s);
      setRecipientsInput(s.recipients.join(", "));
      setFromInput(s.from_address);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update report settings");
    } finally {
      setSaving(false);
    }
  }

  function handleRecipientsBlur() {
    if (!settings) return;
    const emails = recipientsInput
      .split(/[,;\n]/)
      .map((e) => e.trim().toLowerCase())
      .filter(Boolean);
    if (emails.length === 0) {
      setError("At least one recipient email is required.");
      setRecipientsInput(settings.recipients.join(", "));
      return;
    }
    const invalid = emails.filter((e) => !EMAIL_RE.test(e));
    if (invalid.length > 0) {
      setError(`Invalid email(s): ${invalid.join(", ")}`);
      return;
    }
    const deduped = Array.from(new Set(emails));
    if (deduped.join(",") === settings.recipients.join(",")) return;
    applyPatch({ recipients: deduped });
  }

  function handleFromBlur() {
    if (!settings) return;
    const v = fromInput.trim();
    if (!v || v === settings.from_address) {
      setFromInput(settings.from_address);
      return;
    }
    applyPatch({ from_address: v });
  }

  async function handleSendTest() {
    setError(null);
    setMessage(null);
    setTestResult(null);
    setSending(true);
    try {
      const r = await sendTestReport();
      setTestResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to send test report");
    } finally {
      setSending(false);
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
    <div className="px-6 py-6 max-w-[900px]">
      <header className="mb-6">
        <div className="flex items-center gap-2 mb-1">
          <svg className="w-5 h-5 text-zinc-700 dark:text-zinc-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 17v-6m4 6V7m4 10v-3M5 21h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v14a2 2 0 002 2z" />
          </svg>
          <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Weekly Reports</h1>
        </div>
        <p className="text-sm text-zinc-500">
          Emails a summary of the latest webinar — headline stats vs the previous webinar, segment,
          A/B, copy and sender performance, plus AI highlights and recommendations.
        </p>
      </header>

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

      {/* Resend status chip */}
      <div className="mb-4 flex items-center gap-2 text-xs">
        <span className="text-zinc-500">Email delivery:</span>
        {resend === null ? (
          <span className="text-zinc-500">…</span>
        ) : resend.configured ? (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-emerald-500/15 text-emerald-500 border-emerald-500/30">
            Resend connected
          </span>
        ) : (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-amber-500/15 text-amber-600 border-amber-500/30">
            Resend not connected
          </span>
        )}
        <Link href="/connectors/resend" className="text-violet-500 hover:text-violet-400">
          Manage key →
        </Link>
      </div>

      {settings && (
        <div className="space-y-4">
          {/* Schedule */}
          <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/20 p-4">
            <h2 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200 mb-3">Schedule</h2>
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={settings.enabled}
                  onChange={(e) => applyPatch({ enabled: e.target.checked })}
                  disabled={saving}
                  className="w-4 h-4"
                />
                <span className="font-semibold text-zinc-800 dark:text-zinc-200">Weekly report</span>
              </label>
              <span className="text-zinc-500 text-xs">on</span>
              <select
                value={settings.day_of_week}
                onChange={(e) => applyPatch({ day_of_week: e.target.value })}
                disabled={saving || !settings.enabled}
                className={inputCls}
              >
                {DAYS.map((d) => (
                  <option key={d.value} value={d.value}>{d.label}</option>
                ))}
              </select>
              <span className="text-zinc-500 text-xs">at</span>
              <select
                value={settings.hour_local}
                onChange={(e) => applyPatch({ hour_local: parseInt(e.target.value) })}
                disabled={saving || !settings.enabled}
                className={inputCls}
              >
                {Array.from({ length: 24 }).map((_, h) => (
                  <option key={h} value={h}>{h.toString().padStart(2, "0")}</option>
                ))}
              </select>
              <span className="text-zinc-500 text-xs">:</span>
              <select
                value={settings.minute_local}
                onChange={(e) => applyPatch({ minute_local: parseInt(e.target.value) })}
                disabled={saving || !settings.enabled}
                className={inputCls}
              >
                {MINUTES.map((m) => (
                  <option key={m} value={m}>{m.toString().padStart(2, "0")}</option>
                ))}
              </select>
              <input
                type="text"
                defaultValue={settings.timezone}
                key={`tz-${settings.timezone}`}
                onBlur={(e) => {
                  const v = e.target.value.trim();
                  if (v && v !== settings.timezone) applyPatch({ timezone: v });
                }}
                disabled={saving || !settings.enabled}
                placeholder="America/Chicago"
                className={`w-40 ${inputCls}`}
              />
            </div>
            <p className="mt-2 text-[11px] text-zinc-500">
              Default: Wednesday 14:00 America/Chicago — the day after your Tuesday webinars, once
              stats have settled.
            </p>
          </section>

          {/* Email */}
          <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/20 p-4">
            <h2 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200 mb-3">Email</h2>
            <div className="space-y-3">
              <div>
                <label className="block text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">
                  Recipients (comma-separated)
                </label>
                <input
                  type="text"
                  value={recipientsInput}
                  onChange={(e) => setRecipientsInput(e.target.value)}
                  onBlur={handleRecipientsBlur}
                  disabled={saving}
                  placeholder="geri@quantum-scaling.com"
                  className={`w-full ${inputCls}`}
                />
              </div>
              <div>
                <label className="block text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">
                  From address
                </label>
                <input
                  type="text"
                  value={fromInput}
                  onChange={(e) => setFromInput(e.target.value)}
                  onBlur={handleFromBlur}
                  disabled={saving}
                  placeholder="reports@qs-institutes.com"
                  className={`w-full ${inputCls}`}
                />
                <p className="mt-1 text-[11px] text-zinc-500">
                  Must be on a domain verified in Resend (e.g. qs-institutes.com).
                </p>
              </div>
            </div>
          </section>

          {/* Test send + status */}
          <section className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/20 p-4">
            <h2 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200 mb-3">Send now</h2>
            <div className="flex items-center gap-3">
              <button
                onClick={handleSendTest}
                disabled={sending}
                className="px-3 py-1.5 text-xs rounded-md bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {sending ? "Generating & sending… (can take a minute)" : "Send test report now"}
              </button>
              {sending && (
                <div className="w-3.5 h-3.5 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
              )}
            </div>
            {testResult && (
              <div
                className={`mt-3 px-3 py-2 rounded-md border text-xs ${
                  testResult.ok
                    ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-500"
                    : "border-red-500/30 bg-red-500/10 text-red-500"
                }`}
              >
                {testResult.ok
                  ? `Test report for W${testResult.webinar_number} sent${
                      testResult.narrative_included === false ? " (AI summary unavailable — sent without it)" : ""
                    }.`
                  : `Test send failed: ${testResult.error}`}
              </div>
            )}
            <div className="mt-3 space-y-1 text-[11px] text-zinc-500">
              <p>
                Last scheduled send:{" "}
                {settings.last_sent_at ? formatTimestamp(settings.last_sent_at) : "never"}
              </p>
              {settings.last_error && (
                <p className="text-red-500">Last error: {settings.last_error}</p>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
