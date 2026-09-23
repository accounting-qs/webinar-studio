"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchSkarpeCredentials,
  fetchSkarpeJob,
  prepareSkarpeCampaigns,
  startSkarpeCampaigns,
  type ApiSkarpeCredential,
  type ApiSkarpeJob,
  type ApiSkarpePrepareResponse,
} from "@/lib/api";

/** One selected Planning row, with the display name PlanningPage would show
 *  (list_name or the generated default) as the campaign-title fallback. */
export interface SkarpeModalList {
  assignmentId: string;
  fallbackName: string;
}

interface ItemDraft {
  title: string;
  eventStart: string;
  eventEnd: string;
  /** Once the user edits the end manually it stops following start+60min. */
  endTouched: boolean;
}

function plusOneHour(start: string): string {
  const d = new Date(start);
  if (isNaN(d.getTime())) return "";
  d.setHours(d.getHours() + 1);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function SkarpeCampaignModal({
  lists,
  onClose,
  onDone,
}: {
  lists: SkarpeModalList[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [credentials, setCredentials] = useState<ApiSkarpeCredential[] | null>(null);
  const [credError, setCredError] = useState<string | null>(null);
  const [credId, setCredId] = useState<string | null>(null);

  const [prepare, setPrepare] = useState<ApiSkarpePrepareResponse | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [prepareError, setPrepareError] = useState<string | null>(null);

  const [drafts, setDrafts] = useState<Record<string, ItemDraft>>({});

  const [attachAccounts, setAttachAccounts] = useState(false);
  const [accountIds, setAccountIds] = useState<Set<string>>(new Set());
  const [accountFilter, setAccountFilter] = useState("");
  const [dailyLimit, setDailyLimit] = useState("");

  const [pushContacts, setPushContacts] = useState(false);
  const [policyConfirmed, setPolicyConfirmed] = useState(false);
  const [confirmedBy, setConfirmedBy] = useState("Gergo Nagy");

  const [submitError, setSubmitError] = useState<string | null>(null);
  const [job, setJob] = useState<ApiSkarpeJob | null>(null);
  const [polling, setPolling] = useState(false);
  const startedRef = useRef(false);

  // Guards against a slow prepare for workspace A landing after the user
  // already switched to workspace B.
  const prepareSeq = useRef(0);

  function runPrepare(id: string) {
    const seq = ++prepareSeq.current;
    setCredId(id);
    setPreparing(true);
    setPrepare(null);
    setPrepareError(null);
    // The workspace changed: policy text and sending accounts differ per
    // workspace, so any prior confirmation/selection no longer applies.
    setPolicyConfirmed(false);
    setAccountIds(new Set());
    prepareSkarpeCampaigns({ credential_id: id, assignment_ids: lists.map((l) => l.assignmentId) })
      .then((r) => {
        if (seq !== prepareSeq.current) return;
        setPrepare(r);
        const next: Record<string, ItemDraft> = {};
        for (const item of r.items) {
          const fallback = lists.find((l) => l.assignmentId === item.assignment_id)?.fallbackName ?? "";
          const start = item.event_start ?? "";
          next[item.assignment_id] = {
            title: item.list_name || fallback,
            eventStart: start,
            eventEnd: item.event_end ?? (start ? plusOneHour(start) : ""),
            endTouched: false,
          };
        }
        setDrafts(next);
      })
      .catch((e) => {
        if (seq === prepareSeq.current)
          setPrepareError(e instanceof Error ? e.message : "Failed to reach Skarpe");
      })
      .finally(() => {
        if (seq === prepareSeq.current) setPreparing(false);
      });
  }

  useEffect(() => {
    fetchSkarpeCredentials()
      .then((r) => {
        setCredentials(r.credentials);
        if (r.credentials.length === 1) runPrepare(r.credentials[0].id);
      })
      .catch((e) => setCredError(e instanceof Error ? e.message : "Failed to load Skarpe workspaces"));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only load
  }, []);

  const activeAccounts = useMemo(() => {
    const all = prepare?.sending_accounts ?? [];
    const q = accountFilter.trim().toLowerCase();
    return q ? all.filter((a) => a.email.toLowerCase().includes(q)) : all;
  }, [prepare, accountFilter]);

  const canSubmit =
    !!prepare &&
    !job &&
    Object.values(drafts).every((d) => d.title.trim()) &&
    (!attachAccounts || accountIds.size > 0) &&
    (!pushContacts || (policyConfirmed && confirmedBy.trim()));

  async function handleSubmit() {
    if (!prepare || !credId || startedRef.current) return;
    startedRef.current = true;
    setSubmitError(null);
    try {
      const res = await startSkarpeCampaigns({
        credential_id: credId,
        items: prepare.items.map((item) => {
          const d = drafts[item.assignment_id];
          return {
            assignment_id: item.assignment_id,
            title: d.title.trim(),
            event_title: item.event_title,
            event_description: item.event_description,
            event_location: item.event_location,
            event_start: d.eventStart || null,
            event_end: d.eventEnd || null,
            webinar_number: item.matched_webinar?.webinar_number ?? null,
          };
        }),
        account_ids: attachAccounts ? [...accountIds] : [],
        daily_limit: attachAccounts && dailyLimit.trim() ? Number(dailyLimit) : null,
        push_contacts: pushContacts,
        policy: pushContacts
          ? {
              policy_version: prepare.policy.policy_version,
              policy_hash: prepare.policy.policy_hash,
              confirmed: policyConfirmed,
              confirmed_by: confirmedBy.trim(),
            }
          : null,
      });
      setPolling(true);
      let current = await fetchSkarpeJob(res.job_id);
      setJob(current);
      while (current.status === "running") {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          current = await fetchSkarpeJob(res.job_id);
        } catch {
          // Server restarted mid-job: the drafts already created are safe in
          // Skarpe and in skarpe_campaigns; re-running converges.
          setSubmitError(
            "Lost the job (server restarted?). Campaigns created so far are kept — re-run on the same lists to finish; already-created drafts are reused.",
          );
          break;
        }
        setJob({ ...current });
      }
      setPolling(false);
      if (current.status !== "running") setJob({ ...current });
      onDone();
    } catch (e) {
      startedRef.current = false;
      setPolling(false);
      setSubmitError(e instanceof Error ? e.message : "Failed to create campaigns");
    }
  }

  const jobFinished = job && job.status !== "running" && !polling;
  const doneCount = job ? Object.values(job.items).filter((i) => i.status === "done").length : 0;
  const errorCount = job ? Object.values(job.items).filter((i) => i.status === "error").length : 0;

  return (
    <div className="fixed inset-0 z-[60] bg-black/60 backdrop-blur-sm flex items-start justify-center pt-12 pb-12 overflow-y-auto">
      <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700/60 rounded-xl shadow-2xl w-full max-w-3xl mx-4">
        {/* Header */}
        <div className="px-5 py-4 border-b border-zinc-200 dark:border-zinc-800 flex items-center justify-between">
          <div>
            <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">
              Create Draft Campaigns in Skarpe
            </h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              One draft per selected list ({lists.length}). Drafts never send anything — launching
              stays a manual step inside Skarpe.
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 text-lg leading-none"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="px-5 py-4 space-y-5 max-h-[70vh] overflow-y-auto">
          {/* 1 — Workspace picker */}
          <section>
            <h3 className="text-xs font-bold text-zinc-700 dark:text-zinc-300 uppercase tracking-wider mb-2">
              Skarpe account
            </h3>
            {credError && (
              <div className="px-3 py-2 rounded-md border border-red-500/30 bg-red-500/10 text-xs text-red-500">
                {credError}
              </div>
            )}
            {credentials && credentials.length === 0 && (
              <p className="text-xs text-zinc-500">
                No Skarpe workspaces connected. Add one under Connectors → Skarpe first.
              </p>
            )}
            <div className="flex flex-col gap-1.5">
              {(credentials ?? []).map((c) => (
                <label
                  key={c.id}
                  className={`flex items-center gap-2.5 px-3 py-2 rounded-md border cursor-pointer text-xs ${
                    credId === c.id
                      ? "border-violet-500/60 bg-violet-500/5"
                      : "border-zinc-200 dark:border-zinc-700/60 hover:bg-zinc-50 dark:hover:bg-zinc-800/40"
                  } ${job ? "opacity-60 pointer-events-none" : ""}`}
                >
                  <input
                    type="radio"
                    name="skarpe-cred"
                    checked={credId === c.id}
                    onChange={() => runPrepare(c.id)}
                    className="accent-violet-600"
                  />
                  <span className="font-semibold text-zinc-800 dark:text-zinc-200">{c.name}</span>
                  <span className="font-mono text-zinc-500">{c.base_url}</span>
                </label>
              ))}
            </div>
            {preparing && (
              <div className="flex items-center gap-2 mt-3 text-xs text-zinc-500">
                <div className="w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
                Fetching workspace details from Skarpe…
              </div>
            )}
            {prepareError && (
              <div className="mt-3 px-3 py-2 rounded-md border border-red-500/30 bg-red-500/10 text-xs text-red-500 flex items-center justify-between gap-3">
                <span>{prepareError}</span>
                <button
                  onClick={() => credId && runPrepare(credId)}
                  className="px-2 py-1 rounded-md border border-red-500/40 hover:bg-red-500/10 whitespace-nowrap"
                >
                  Retry
                </button>
              </div>
            )}
            {prepare && (
              <div className="mt-3 flex items-center gap-2 flex-wrap text-xs">
                <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-emerald-500/15 text-emerald-500 border-emerald-500/30">
                  {prepare.workspace.key_name ?? "Connected"}
                </span>
                {prepare.workspace.permissions.map((p) => (
                  <span
                    key={p}
                    className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-zinc-100 dark:bg-zinc-800/60 text-zinc-500 border-zinc-200 dark:border-zinc-700/60"
                  >
                    {p}
                  </span>
                ))}
                {prepare.workspace.timezone && (
                  <span className="text-zinc-500">
                    All event times are in <span className="font-semibold">{prepare.workspace.timezone}</span>
                  </span>
                )}
              </div>
            )}
          </section>

          {/* 2 — Per-list campaigns */}
          {prepare && (
            <section>
              <h3 className="text-xs font-bold text-zinc-700 dark:text-zinc-300 uppercase tracking-wider mb-2">
                Campaigns
              </h3>
              <div className="space-y-3">
                {prepare.items.map((item) => {
                  const d = drafts[item.assignment_id];
                  if (!d) return null;
                  const ji = job?.items[item.assignment_id];
                  return (
                    <div
                      key={item.assignment_id}
                      className="rounded-lg border border-zinc-200 dark:border-zinc-700/60 p-3"
                    >
                      <div className="flex items-center gap-2 mb-2">
                        <input
                          value={d.title}
                          disabled={!!job}
                          onChange={(e) =>
                            setDrafts((prev) => ({
                              ...prev,
                              [item.assignment_id]: { ...prev[item.assignment_id], title: e.target.value },
                            }))
                          }
                          placeholder="Campaign name"
                          className="flex-1 bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2.5 py-1.5 text-xs font-semibold text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 disabled:opacity-60"
                        />
                        {ji && (
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-semibold border whitespace-nowrap ${
                              ji.status === "done"
                                ? "bg-emerald-500/15 text-emerald-500 border-emerald-500/30"
                                : ji.status === "error"
                                  ? "bg-red-500/10 text-red-500 border-red-500/30"
                                  : "bg-violet-500/10 text-violet-500 border-violet-500/30"
                            }`}
                          >
                            {ji.status === "pending" && "Waiting"}
                            {ji.status === "creating" && "Creating draft…"}
                            {ji.status === "attaching" && "Attaching accounts…"}
                            {ji.status === "pushing" &&
                              `Pushing contacts ${ji.contacts_pushed}/${ji.contacts_total}…`}
                            {ji.status === "done" && "Done"}
                            {ji.status === "error" && "Failed"}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-3 flex-wrap text-xs text-zinc-500">
                        <label className="flex items-center gap-1.5">
                          Start
                          <input
                            type="datetime-local"
                            value={d.eventStart}
                            disabled={!!job}
                            onChange={(e) =>
                              setDrafts((prev) => {
                                const cur = prev[item.assignment_id];
                                const start = e.target.value;
                                return {
                                  ...prev,
                                  [item.assignment_id]: {
                                    ...cur,
                                    eventStart: start,
                                    eventEnd: cur.endTouched ? cur.eventEnd : start ? plusOneHour(start) : "",
                                  },
                                };
                              })
                            }
                            className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 disabled:opacity-60"
                          />
                        </label>
                        <label className="flex items-center gap-1.5">
                          End
                          <input
                            type="datetime-local"
                            value={d.eventEnd}
                            disabled={!!job}
                            onChange={(e) =>
                              setDrafts((prev) => ({
                                ...prev,
                                [item.assignment_id]: {
                                  ...prev[item.assignment_id],
                                  eventEnd: e.target.value,
                                  endTouched: true,
                                },
                              }))
                            }
                            className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 disabled:opacity-60"
                          />
                        </label>
                        {item.matched_webinar ? (
                          <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-sky-500/10 text-sky-500 border-sky-500/30">
                            Skarpe webinar #{item.matched_webinar.webinar_number}
                          </span>
                        ) : (
                          <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-amber-500/10 text-amber-500 border-amber-500/30">
                            No Skarpe webinar match
                          </span>
                        )}
                        <span className="tabular-nums">{item.assigned_contacts.toLocaleString()} contacts</span>
                        {item.existing_campaign && (
                          <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-amber-500/10 text-amber-500 border-amber-500/30">
                            Already created ({item.existing_campaign.status}) — will be reused
                          </span>
                        )}
                        {(ji?.status === "done" && ji.app_url) || item.existing_campaign?.app_url ? (
                          <a
                            href={(ji?.app_url ?? item.existing_campaign?.app_url) as string}
                            target="_blank"
                            rel="noreferrer"
                            className="text-violet-500 hover:text-violet-400 font-semibold"
                          >
                            Open in Skarpe ↗
                          </a>
                        ) : null}
                      </div>
                      {item.event_title ? (
                        <p className="mt-2 text-xs text-zinc-600 dark:text-zinc-400 truncate" title={item.event_title}>
                          <span className="text-zinc-500 font-semibold">Event title:</span> {item.event_title}
                        </p>
                      ) : null}
                      {item.warnings.length > 0 && !job && (
                        <ul className="mt-2 space-y-0.5">
                          {item.warnings.map((w) => (
                            <li key={w} className="text-[11px] text-amber-600 dark:text-amber-500">
                              ⚠ {w}
                            </li>
                          ))}
                        </ul>
                      )}
                      {ji?.error && (
                        <p className="mt-2 text-[11px] text-red-500">{ji.error}</p>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          )}

          {/* 3 — Sending accounts */}
          {prepare && (
            <section>
              <label className="flex items-center gap-2 text-xs font-bold text-zinc-700 dark:text-zinc-300 uppercase tracking-wider cursor-pointer">
                <input
                  type="checkbox"
                  checked={attachAccounts}
                  disabled={!!job}
                  onChange={(e) => setAttachAccounts(e.target.checked)}
                  className="accent-violet-600"
                />
                Pre-select Google accounts
              </label>
              <p className="text-[11px] text-zinc-500 mt-1">
                Off: the drafts get no sending accounts — you attach them inside Skarpe. On: the
                selected mailboxes are attached to every created campaign.
              </p>
              {attachAccounts && (
                <div className="mt-2">
                  <div className="flex items-center gap-2 mb-2">
                    <input
                      value={accountFilter}
                      onChange={(e) => setAccountFilter(e.target.value)}
                      placeholder={`Filter ${prepare.sending_accounts.length} mailboxes…`}
                      className="flex-1 bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2.5 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
                    />
                    <label className="flex items-center gap-1.5 text-xs text-zinc-500 whitespace-nowrap">
                      Daily limit
                      <input
                        type="number"
                        min={1}
                        value={dailyLimit}
                        disabled={!!job}
                        onChange={(e) => setDailyLimit(e.target.value)}
                        placeholder="per mailbox"
                        className="w-24 bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
                        title="Leave empty to keep each mailbox on its own limit (usually right)"
                      />
                    </label>
                    <span className="text-xs text-zinc-500 whitespace-nowrap">
                      {accountIds.size} selected
                    </span>
                  </div>
                  <div className="max-h-48 overflow-y-auto rounded-md border border-zinc-200 dark:border-zinc-700/60 divide-y divide-zinc-100 dark:divide-zinc-800">
                    {activeAccounts.map((a) => (
                      <label
                        key={a.account_id}
                        className={`flex items-center gap-2 px-2.5 py-1.5 text-xs cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-800/40 ${
                          a.is_blocked || !a.is_active ? "opacity-50" : ""
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={accountIds.has(a.account_id)}
                          disabled={!!job}
                          onChange={(e) =>
                            setAccountIds((prev) => {
                              const next = new Set(prev);
                              if (e.target.checked) next.add(a.account_id);
                              else next.delete(a.account_id);
                              return next;
                            })
                          }
                          className="accent-violet-600"
                        />
                        <span className="font-mono text-zinc-800 dark:text-zinc-200">{a.email}</span>
                        <span className="text-zinc-500 tabular-nums ml-auto">limit {a.daily_limit}/day</span>
                        {a.is_blocked && (
                          <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold border bg-red-500/10 text-red-500 border-red-500/30">
                            blocked
                          </span>
                        )}
                        {!a.is_active && (
                          <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold border bg-zinc-100 dark:bg-zinc-800/60 text-zinc-500 border-zinc-200 dark:border-zinc-700/60">
                            inactive
                          </span>
                        )}
                      </label>
                    ))}
                    {activeAccounts.length === 0 && (
                      <p className="px-2.5 py-2 text-xs text-zinc-500">No mailboxes match.</p>
                    )}
                  </div>
                </div>
              )}
            </section>
          )}

          {/* 4 — Contact push + compliance */}
          {prepare && (
            <section>
              <label className="flex items-center gap-2 text-xs font-bold text-zinc-700 dark:text-zinc-300 uppercase tracking-wider cursor-pointer">
                <input
                  type="checkbox"
                  checked={pushContacts}
                  disabled={!!job}
                  onChange={(e) => setPushContacts(e.target.checked)}
                  className="accent-violet-600"
                />
                Also push contacts into the campaigns
              </label>
              <p className="text-[11px] text-zinc-500 mt-1">
                Streams every assigned, non-blocklisted contact of each list into its campaign so you
                don’t re-upload inside Skarpe. Contacts already on a campaign are skipped by Skarpe.
              </p>
              {pushContacts && (
                <div className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
                  <p className="text-[11px] font-semibold text-amber-600 dark:text-amber-500 mb-2">
                    Skarpe requires you to accept the following before contacts can be uploaded
                    (policy {prepare.policy.policy_version}):
                  </p>
                  <div className="max-h-32 overflow-y-auto rounded-md bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 p-2.5 mb-2">
                    {prepare.policy.statements.map((s) => (
                      <p key={s} className="text-xs text-zinc-700 dark:text-zinc-300">
                        {s}
                      </p>
                    ))}
                  </div>
                  <div className="flex items-center gap-3 flex-wrap text-[11px] mb-2">
                    {Object.entries(prepare.policy.links).map(([k, url]) => (
                      <a
                        key={k}
                        href={url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-violet-500 hover:text-violet-400 capitalize"
                      >
                        {k} ↗
                      </a>
                    ))}
                  </div>
                  <label className="flex items-start gap-2 text-xs text-zinc-700 dark:text-zinc-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={policyConfirmed}
                      disabled={!!job}
                      onChange={(e) => setPolicyConfirmed(e.target.checked)}
                      className="accent-violet-600 mt-0.5"
                    />
                    <span>I have read the statement above and confirm it.</span>
                  </label>
                  <label className="flex items-center gap-2 mt-2 text-xs text-zinc-500">
                    Confirmed by
                    <input
                      value={confirmedBy}
                      disabled={!!job}
                      onChange={(e) => setConfirmedBy(e.target.value)}
                      className="bg-white dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
                    />
                  </label>
                </div>
              )}
            </section>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-4 border-t border-zinc-200 dark:border-zinc-800 flex items-center gap-3">
          {submitError && <span className="text-xs text-red-500 flex-1">{submitError}</span>}
          {jobFinished && !submitError && (
            <span className="text-xs flex-1 text-zinc-600 dark:text-zinc-400">
              {doneCount} campaign{doneCount === 1 ? "" : "s"} created
              {errorCount > 0 && <span className="text-red-500">, {errorCount} failed</span>}.
            </span>
          )}
          {!submitError && !jobFinished && <span className="flex-1" />}
          <button
            onClick={onClose}
            className="px-4 py-1.5 text-xs rounded-lg border border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            {jobFinished ? "Close" : "Cancel"}
          </button>
          {!jobFinished && (
            <button
              onClick={handleSubmit}
              disabled={!canSubmit || polling}
              className="px-4 py-1.5 text-xs rounded-lg bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {polling
                ? `Creating… ${job?.done ?? 0}/${job?.total ?? lists.length}`
                : `Create ${lists.length} Draft Campaign${lists.length === 1 ? "" : "s"}`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
