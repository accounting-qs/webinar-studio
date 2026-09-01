"use client";

import { useEffect, useState } from "react";
import {
  fetchContactDetail,
  type ApiContactDetail,
  type ApiContactWebinarHistoryRow,
} from "@/lib/api";
import {
  BlocklistedBadge,
  ResponseBadge,
  StatusBadge,
  fmtDate,
  webinarLabel,
} from "./ContactBadges";

/** Half-page right-side panel with a contact's full profile, per-webinar
 *  history (list, sender, status, response, attendance, booking) and the
 *  attributed bookings. No backdrop: the directory stays clickable so contacts
 *  can be browsed one after another without closing the panel. */
export function ContactDetailDrawer({
  contactId,
  onClose,
}: {
  contactId: string | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ApiContactDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!contactId) return;
    let cancelled = false;
    setDetail(null);
    setError(null);
    fetchContactDetail(contactId)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load contact"); });
    return () => { cancelled = true; };
  }, [contactId]);

  useEffect(() => {
    if (!contactId) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [contactId, onClose]);

  if (!contactId) return null;

  const c = detail?.contact;
  const name = c ? [c.first_name, c.last_name].filter(Boolean).join(" ") : "";
  const customEntries = c
    ? Object.entries(c.custom_data).filter(([, v]) => v !== null && v !== undefined && String(v).trim() !== "")
    : [];

  return (
    <div className="fixed inset-y-0 right-0 z-[60] w-[50vw] min-w-[600px] bg-white dark:bg-zinc-900 border-l border-zinc-200 dark:border-zinc-800/60 shadow-2xl flex flex-col animate-in slide-in-from-right-8 duration-200">

      {/* ── Header ─────────────────────────────────────────────────── */}
      <div className="px-6 py-4 border-b border-zinc-200 dark:border-zinc-800/40 flex items-start gap-3 shrink-0">
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-bold text-zinc-900 dark:text-zinc-100 tracking-tight truncate">
            {detail ? (name || c?.email || "Contact") : "Contact"}
          </h2>
          {c?.email && (
            <div className="font-mono text-xs text-zinc-500 truncate mt-0.5">{c.email}</div>
          )}
          {c && (
            <div className="flex items-center flex-wrap gap-1.5 mt-2">
              <StatusBadge status={c.outreach_status} />
              {c.is_blocklisted && <BlocklistedBadge />}
              <span className="text-[11px] text-zinc-500">
                Invited {c.times_invited}×{c.last_invited_at ? ` · last ${fmtDate(c.last_invited_at)}` : ""}
              </span>
            </div>
          )}
        </div>
        <button
          onClick={onClose}
          className="shrink-0 p-1.5 rounded-md text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200 hover:bg-zinc-100 dark:hover:bg-zinc-800 transition-colors"
          aria-label="Close"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      {/* ── Body ───────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto px-6 py-5">
        {error && (
          <div className="text-sm text-rose-600 dark:text-rose-400">{error}</div>
        )}
        {!detail && !error && (
          <div className="flex items-center gap-3 text-zinc-400 text-sm py-10 justify-center">
            <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
            Loading contact…
          </div>
        )}

        {detail && c && (
          <div className="space-y-6">

            {/* Profile */}
            <Section title="Profile">
              <FieldGrid>
                <Field label="Title" value={c.title} />
                <Field label="Seniority" value={c.seniority} />
                <Field label="Company" value={c.company_website} href={websiteHref(c.company_website)} />
                <Field label="Industry" value={c.industry} />
                <Field label="Sector" value={c.sector} />
                <Field label="Country" value={c.country || c.list_location} />
                <Field label="Company country" value={c.company_country} />
                <Field
                  label="Employees"
                  value={c.employee_range
                    ? `${c.employee_range}${c.employee_count != null ? ` (${c.employee_count.toLocaleString()})` : ""}`
                    : c.employee_count != null ? c.employee_count.toLocaleString() : null}
                />
                <Field label="Founded" value={c.company_founded_year} />
                <Field label="Annual revenue" value={c.company_annual_revenue} />
                <Field label="Total funding" value={c.company_total_funding} />
              </FieldGrid>
            </Section>

            {/* Source */}
            <Section title="Source">
              <FieldGrid>
                <Field label="Bucket" value={c.bucket_name} />
                <Field label="Lead list" value={c.lead_list_name} />
                <Field label="Segment" value={c.segment_name} />
                <Field label="Classification" value={c.classification} />
                <Field label="Enrichment class" value={c.enrichment_classification} />
                <Field label="Primary identity" value={c.primary_identity} />
                <Field label="Sub identity" value={c.sub_identity} />
                <Field label="Provider" value={c.database_provider} />
                <Field label="Scraper" value={c.scraper} />
                <Field
                  label="Imported"
                  value={c.upload
                    ? `${c.upload.file_name}${c.upload.uploaded_at ? ` · ${fmtDate(c.upload.uploaded_at)}` : ""}`
                    : fmtDate(c.created_at)}
                />
              </FieldGrid>
            </Section>

            {/* Custom fields */}
            {customEntries.length > 0 && (
              <Section title="Custom fields">
                <FieldGrid>
                  {customEntries.map(([k, v]) => (
                    <Field key={k} label={k} value={String(v)} />
                  ))}
                </FieldGrid>
              </Section>
            )}

            {/* Webinar history */}
            <Section
              title="Webinar history"
              badge={detail.webinar_history.length > 0 ? String(detail.webinar_history.length) : undefined}
            >
              {detail.webinar_history.length === 0 ? (
                <p className="text-sm text-zinc-400">Never assigned to a webinar.</p>
              ) : (
                <div className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-zinc-50 dark:bg-zinc-800/30 border-b border-zinc-200 dark:border-zinc-800/40">
                        <Th>Webinar / List</Th>
                        <Th>Sender</Th>
                        <Th>Status</Th>
                        <Th>Response</Th>
                        <Th>Attendance</Th>
                        <Th>Booking</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.webinar_history.map((h) => (
                        <tr key={h.webinar_id} className="border-b border-zinc-100 dark:border-zinc-800/30 last:border-b-0 align-top">
                          <td className="px-2.5 py-2.5 max-w-[170px]">
                            <div className="text-xs font-semibold text-zinc-800 dark:text-zinc-200 truncate">
                              {webinarLabel(h.webinar_number, h.variant_label)}
                            </div>
                            <div className="text-[11px] text-zinc-500">{fmtDate(h.webinar_date) || "—"}</div>
                            <div className="text-[11px] text-zinc-400 truncate" title={h.list_label ?? undefined}>
                              {h.is_nonjoiners ? "Nonjoiners" : (h.list_label || "—")}
                            </div>
                          </td>
                          <td className="px-2.5 py-2.5 max-w-[150px]">
                            <div className="text-xs text-zinc-700 dark:text-zinc-300 truncate" title={h.sender_name ?? undefined}>
                              {h.sender_name || "—"}
                            </div>
                            {h.calendar_account && (
                              <div className="text-[11px] text-zinc-400 truncate" title={h.calendar_account}>
                                {h.calendar_account}
                              </div>
                            )}
                          </td>
                          <td className="px-2.5 py-2.5">
                            {h.membership_status ? (
                              <>
                                <StatusBadge status={h.membership_status} />
                                <div className="text-[11px] text-zinc-500 mt-1">
                                  {h.membership_status === "used" && h.used_at
                                    ? `used ${fmtDate(h.used_at)}`
                                    : h.assigned_date ? `assigned ${fmtDate(h.assigned_date)}` : ""}
                                </div>
                              </>
                            ) : (
                              <span className="text-[11px] text-zinc-400" title="No current membership — released after the invite, matched from calendar data, or self-registered">
                                {h.calendar_response || h.calendar_invited_date ? "no longer on list" : "self-registered"}
                              </span>
                            )}
                          </td>
                          <td className="px-2.5 py-2.5">
                            <ResponseBadge response={h.calendar_response} />
                            {h.calendar_invited_date && (
                              <div className="text-[11px] text-zinc-500 mt-1">invited {fmtDate(h.calendar_invited_date)}</div>
                            )}
                          </td>
                          <td className="px-2.5 py-2.5">
                            <AttendanceCell h={h} />
                          </td>
                          <td className="px-2.5 py-2.5">
                            <BookingCell h={h} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Section>

            {/* Bookings */}
            <Section
              title="Bookings"
              badge={detail.bookings.length > 0 ? String(detail.bookings.length) : undefined}
            >
              {detail.bookings.length === 0 ? (
                <p className="text-sm text-zinc-400">No booked calls.</p>
              ) : (
                <div className="space-y-2">
                  {detail.bookings.map((b) => (
                    <div
                      key={b.appointment_id}
                      className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 bg-zinc-50/50 dark:bg-zinc-800/20 px-4 py-3"
                    >
                      <div className="flex items-center flex-wrap gap-2">
                        <span className="text-xs font-semibold text-zinc-800 dark:text-zinc-200">
                          {b.booked_at ? `Booked ${fmtDate(b.booked_at)}` : "Booked"}
                        </span>
                        {b.call_at && (
                          <span className="text-xs text-zinc-500">→ call {fmtDate(b.call_at)}</span>
                        )}
                        {b.webinar_number != null && (
                          <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400 border border-violet-200 dark:border-violet-500/20">
                            {webinarLabel(b.webinar_number, b.variant_label)}
                          </span>
                        )}
                        {b.won === true && (
                          <span className="text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-500/20">
                            Won
                          </span>
                        )}
                        {b.disqualified === true && (
                          <span className="text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full bg-rose-100 dark:bg-rose-500/15 text-rose-600 dark:text-rose-400 border border-rose-200 dark:border-rose-500/20">
                            Disqualified
                          </span>
                        )}
                      </div>
                      <div className="flex items-center flex-wrap gap-3 mt-1 text-[11px] text-zinc-500">
                        {b.call_status && <span>Status: {b.call_status}</span>}
                        {b.lead_quality && <span>Quality: {b.lead_quality}</span>}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Section>

          </div>
        )}
      </div>
    </div>
  );
}

/* ── History cell renderers ────────────────────────────────────────────── */

/** Registered / Live / Replay / No-show, with minutes when known. */
function AttendanceCell({ h }: { h: ApiContactWebinarHistoryRow }) {
  const a = h.attendance;
  if (!a) return <span className="text-zinc-400 text-xs">—</span>;
  const mins = a.minutes_viewing != null && a.minutes_viewing > 0 ? ` · ${a.minutes_viewing} min` : "";
  let label: string;
  let cls: string;
  if (a.watched_live) {
    label = `Live${mins}`;
    cls = "bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-200 dark:border-emerald-500/20";
  } else if (a.watched_replay) {
    label = `Replay${mins}`;
    cls = "bg-sky-100 dark:bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-200 dark:border-sky-500/20";
  } else {
    label = "No-show";
    cls = "bg-zinc-100 dark:bg-zinc-800 text-zinc-500 dark:text-zinc-400 border-zinc-200 dark:border-zinc-700";
  }
  return (
    <>
      <span className={`inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-full border ${cls}`}>
        {label}
      </span>
      {a.subscribed_at && (
        <div className="text-[11px] text-zinc-500 mt-1">reg. {fmtDate(a.subscribed_at)}</div>
      )}
      {a.unsubscribed_at && (
        <div className="text-[11px] text-rose-500/80 mt-0.5">unsub. {fmtDate(a.unsubscribed_at)}</div>
      )}
    </>
  );
}

function BookingCell({ h }: { h: ApiContactWebinarHistoryRow }) {
  const b = h.booking;
  if (!b) return <span className="text-zinc-400 text-xs">—</span>;
  return (
    <>
      <span className="inline-flex items-center gap-1">
        <span className="inline-flex items-center text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full border bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-200 dark:border-violet-500/20">
          Booked
        </span>
        {b.won === true && (
          <span className="inline-flex items-center text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full border bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-200 dark:border-emerald-500/20">
            Won
          </span>
        )}
        {b.disqualified === true && (
          <span className="inline-flex items-center text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full border bg-rose-100 dark:bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-200 dark:border-rose-500/20">
            DQ
          </span>
        )}
      </span>
      <div className="text-[11px] text-zinc-500 mt-1">
        {b.call_at ? `call ${fmtDate(b.call_at)}` : b.booked_at ? `booked ${fmtDate(b.booked_at)}` : ""}
        {b.call_status ? ` · ${b.call_status}` : ""}
      </div>
    </>
  );
}

/* ── Small building blocks ─────────────────────────────────────────────── */

function Section({ title, badge, children }: { title: string; badge?: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="flex items-center gap-2 text-[11px] text-zinc-500 uppercase tracking-wider font-medium mb-2.5">
        {title}
        {badge && (
          <span className="px-1.5 py-0.5 rounded-full bg-zinc-100 dark:bg-zinc-800 text-zinc-500 text-[10px] normal-case tracking-normal">
            {badge}
          </span>
        )}
      </h3>
      {children}
    </section>
  );
}

function FieldGrid({ children }: { children: React.ReactNode }) {
  return <dl className="grid grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-2.5">{children}</dl>;
}

/** Renders nothing when the value is empty, so grids stay clean. */
function Field({ label, value, href }: { label: string; value: string | null | undefined; href?: string | null }) {
  if (!value || !String(value).trim()) return null;
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-zinc-400">{label}</dt>
      <dd className="text-sm text-zinc-800 dark:text-zinc-200 truncate" title={String(value)}>
        {href ? (
          <a href={href} target="_blank" rel="noreferrer" className="text-violet-600 dark:text-violet-400 hover:underline">
            {value}
          </a>
        ) : value}
      </dd>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="text-left px-2.5 py-2 text-[11px] text-zinc-500 uppercase tracking-wider font-medium whitespace-nowrap">
      {children}
    </th>
  );
}

function websiteHref(site: string | null): string | null {
  if (!site) return null;
  const s = site.trim();
  if (!s) return null;
  return /^https?:\/\//i.test(s) ? s : `https://${s}`;
}
