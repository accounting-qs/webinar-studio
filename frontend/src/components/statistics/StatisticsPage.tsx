"use client";

import { useState, useMemo, useEffect, type ReactNode } from "react";
import {
  fetchStatisticsWebinar,
  fetchStatisticsWebinarList,
  syncWgSubscribers,
  triggerGhlWebinarSync,
  type ApiStatisticsRow,
  type ApiStatisticsWebinar,
  type ApiStatisticsWebinarSummary,
  type StatisticsMeta,
  type StatisticsMetrics,
} from "@/lib/api";
import {
  GROUP_BOUNDARY_CLASSES,
  METRIC_COLUMNS,
  METRIC_GROUPS,
  columnsInGroup,
  formatMetricValue,
  isGroupBoundary,
  type MetricColumn,
} from "./metricRegistry";
import { ChatPanel } from "./ChatPanel";

/* ─── Identity columns (pinned left side of table) ────────────────────── */

const IDENTITY_COL_COUNT = 8; // expand, webinar#, status, note, description, copy, url, sendInfo

/* ─── Sticky identity columns ─────────────────────────────────────────────
 * Webinar #, Description, Copy, URL, and Send Info stay visible while the
 * wide metric band scrolls horizontally. Each column is a fixed width; left
 * offsets are cumulative so they line up as a single frozen panel:
 *   Webinar #:   200px wide  @ left-0
 *   Description: 240px wide  @ left-[200px]
 *   Copy:        260px wide  @ left-[440px]
 *   URL:          32px wide  @ left-[700px]
 *   Send Info:   120px wide  @ left-[732px]
 *   (total sticky pane: 852px)
 * Each row type gets its own background so scrolling content doesn't
 * bleed through the sticky cells. */

// z-index: header stacks above rows; list/parent/group stack above metrics
const Z_HEADER = "z-30";
const Z_ROW = "z-20";

// Row-type backgrounds (must be opaque so scroll doesn't bleed through)
const BG_HEADER = "bg-zinc-50 dark:bg-zinc-900";
const BG_PARENT = "bg-zinc-100 dark:bg-zinc-800";
const BG_GROUP = "bg-zinc-100 dark:bg-zinc-800";
const BG_LIST = "bg-white dark:bg-zinc-950";
const BG_SPECIAL = "bg-zinc-50 dark:bg-zinc-900";

// Sticky left offsets — full class strings so Tailwind can pick them up
const L_NUM = "sticky left-0";
const L_DESC = "sticky left-[200px]";
const L_COPY = "sticky left-[440px]";
const L_URL = "sticky left-[700px]";
const L_SEND = "sticky left-[732px]";

// Fixed widths — use w-[] to lock each sticky column
const W_NUM = "w-[200px] min-w-[200px] max-w-[200px]";
const W_DESC = "w-[240px] min-w-[240px] max-w-[240px]";
const W_COPY = "w-[260px] min-w-[260px] max-w-[260px]";
const W_URL = "w-[32px] min-w-[32px] max-w-[32px]";
const W_SEND = "w-[120px] min-w-[120px] max-w-[120px]";

// Composite classes (left + z + bg) per row type / column
const sNumH = `${L_NUM} ${Z_HEADER} ${BG_HEADER}`;
const sDescH = `${L_DESC} ${Z_HEADER} ${BG_HEADER}`;
const sCopyH = `${L_COPY} ${Z_HEADER} ${BG_HEADER}`;
const sUrlH = `${L_URL} ${Z_HEADER} ${BG_HEADER}`;
const sSendH = `${L_SEND} ${Z_HEADER} ${BG_HEADER}`;

const sNumP = `${L_NUM} ${Z_ROW} ${BG_PARENT}`;
const sDescP = `${L_DESC} ${Z_ROW} ${BG_PARENT}`;
// (parent-row's colSpan={4} cell spans Desc+Copy+URL+Send, one sticky cell)

const sNumG = `${L_NUM} ${Z_ROW} ${BG_GROUP}`;
const sDescG = `${L_DESC} ${Z_ROW} ${BG_GROUP}`;
const sCopyG = `${L_COPY} ${Z_ROW} ${BG_GROUP}`;
const sUrlG = `${L_URL} ${Z_ROW} ${BG_GROUP}`;
const sSendG = `${L_SEND} ${Z_ROW} ${BG_GROUP}`;

const sNumL = `${L_NUM} ${Z_ROW} ${BG_LIST}`;
const sDescL = `${L_DESC} ${Z_ROW} ${BG_LIST}`;
const sCopyL = `${L_COPY} ${Z_ROW} ${BG_LIST}`;
const sUrlL = `${L_URL} ${Z_ROW} ${BG_LIST}`;
const sSendL = `${L_SEND} ${Z_ROW} ${BG_LIST}`;

const sNumSp = `${L_NUM} ${Z_ROW} ${BG_SPECIAL}`;
const sDescSp = `${L_DESC} ${Z_ROW} ${BG_SPECIAL}`;
const sCopySp = `${L_COPY} ${Z_ROW} ${BG_SPECIAL}`;
const sUrlSp = `${L_URL} ${Z_ROW} ${BG_SPECIAL}`;
const sSendSp = `${L_SEND} ${Z_ROW} ${BG_SPECIAL}`;

/* ─── Metric info modal ──────────────────────────────────────────────── */

const ENTITY_COLOR: Record<string, string> = {
  "GHL Contact": "bg-violet-500/15 text-violet-500 border-violet-500/30",
  "GHL Opportunity": "bg-sky-500/15 text-sky-500 border-sky-500/30",
  "WebinarGeek Subscriber": "bg-emerald-500/15 text-emerald-500 border-emerald-500/30",
  "Planning Assignment": "bg-amber-500/15 text-amber-500 border-amber-500/30",
  "Webinar": "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
  "Computed": "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
};

function MetricInfoModal({ col, onClose }: { col: MetricColumn; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-3xl w-full max-h-[85vh] overflow-y-auto"
      >
        <div className="flex items-start justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800 sticky top-0 bg-white dark:bg-zinc-900 z-10">
          <div>
            <div className="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold mb-0.5">{col.group}</div>
            <div className="text-base font-bold text-zinc-900 dark:text-zinc-100">{col.label}</div>
            <div className="text-[10px] font-mono text-zinc-500 mt-0.5">{col.key}</div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
            aria-label="Close"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18M6 6l12 12"/>
            </svg>
          </button>
        </div>
        <div className="px-6 py-5 space-y-5 text-sm">
          {col.description && (
            <section>
              <div className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">What it tracks</div>
              <p className="text-zinc-800 dark:text-zinc-200 leading-relaxed">{col.description}</p>
            </section>
          )}

          <section>
            <div className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">Scope (webinar-level vs. per-list)</div>
            <div className="space-y-2 text-xs text-zinc-700 dark:text-zinc-300 leading-relaxed">
              <p>
                <span className="font-semibold text-zinc-900 dark:text-zinc-100">Webinar summary row</span>:
                the filters below are applied directly to all records in the relevant table (GHL contacts,
                opportunities, or WebinarGeek subscribers) — no Planning join.
              </p>
              <p>
                <span className="font-semibold text-zinc-900 dark:text-zinc-100">Per-list (child) row</span>:
                the same filters are additionally joined to the Planning list so only contacts assigned
                to that specific list are counted. The join path is
                <code className="mx-1 px-1.5 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800 text-[11px] text-zinc-700 dark:text-zinc-300 font-mono">
                  contacts.assignment_id → webinar_list_assignments.id
                </code>
                with the contact matched to GHL by
                <code className="mx-1 px-1.5 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800 text-[11px] text-zinc-700 dark:text-zinc-300 font-mono">
                  LOWER(planning_contact.email) = LOWER(ghl_contact.email)
                </code>.
                This scoping is implicit — it applies to every metric below.
              </p>
            </div>
          </section>

          {col.formulaText && (
            <section>
              <div className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">Formula</div>
              <code className="block bg-zinc-50 dark:bg-zinc-800/50 border border-zinc-200 dark:border-zinc-700/50 rounded-lg px-4 py-3 text-sm text-zinc-900 dark:text-zinc-100 font-mono overflow-x-auto whitespace-pre">
                {col.formulaText}
              </code>
            </section>
          )}

          {col.fieldsUsed && col.fieldsUsed.length > 0 && (
            <section>
              <div className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-2">Data fields used</div>
              <div className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 overflow-hidden">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="bg-zinc-50 dark:bg-zinc-900/60 border-b border-zinc-200 dark:border-zinc-800/60">
                      <th className="text-left px-3 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Entity</th>
                      <th className="text-left px-3 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Field</th>
                      <th className="text-left px-3 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Filter</th>
                      <th className="text-left px-3 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">GHL ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {col.fieldsUsed.map((f, i) => (
                      <tr key={i} className="border-t border-zinc-200 dark:border-zinc-800/40">
                        <td className="px-3 py-2 align-top">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-semibold border whitespace-nowrap ${ENTITY_COLOR[f.entity] ?? "bg-zinc-500/15 text-zinc-400 border-zinc-500/30"}`}>
                            {f.entity}
                          </span>
                        </td>
                        <td className="px-3 py-2 font-mono text-[11px] text-zinc-800 dark:text-zinc-200 align-top">
                          {f.field}
                        </td>
                        <td className="px-3 py-2 font-mono text-[11px] text-zinc-600 dark:text-zinc-400 align-top">
                          {f.filter ?? "—"}
                        </td>
                        <td className="px-3 py-2 font-mono text-[10px] text-zinc-500 align-top">
                          {f.fieldId ?? "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {col.source && (
            <section>
              <div className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">Notes</div>
              <p className="text-zinc-700 dark:text-zinc-300 text-xs leading-relaxed whitespace-pre-wrap">{col.source}</p>
            </section>
          )}

          <section>
            <div className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider mb-1.5">Display</div>
            <p className="text-zinc-700 dark:text-zinc-300 text-xs">
              Format: <code className="font-mono text-zinc-500">{col.format}</code>
              {col.decimals != null && <> · decimals: <code className="font-mono text-zinc-500">{col.decimals}</code></>}
              {" · "}null / zero-div renders as "—", explicit 0 renders as "0".
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}

/* ─── Status badge ────────────────────────────────────────────────────── */

function StatusBadge({ status }: { status: string | null }) {
  if (!status) return <span className="text-zinc-500">\u2014</span>;
  const s = status.toLowerCase();
  const colors: Record<string, string> = {
    sent: "bg-emerald-500/15 text-emerald-500 border-emerald-500/30",
    planning: "bg-amber-500/15 text-amber-500 border-amber-500/30",
    draft: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
    cancelled: "bg-red-500/15 text-red-400 border-red-500/30",
  };
  return (
    <span className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${colors[s] ?? "bg-zinc-500/15 text-zinc-400 border-zinc-500/30"}`}>
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
}

/* ─── Metric cell ─────────────────────────────────────────────────────── */

/** Keys that can be drilled down to a contact list. */
const DRILLDOWN_KEYS = new Set([
  "gcalInvitedGhl",
  "unsubscribes",
  "lpRegs",
  "yesMarked", "yesAttended", "yes10MinPlus", "yesAttendBySmsClick", "yesBookings",
  "maybeMarked", "maybeAttended", "maybe10MinPlus", "maybeAttendBySmsClick", "maybeBookings",
  "selfRegMarked", "selfRegAttended", "selfReg10MinPlus", "selfRegBookings",
  "totalRegs", "totalAttended", "total10MinPlus", "total30MinPlus", "attendBySmsReminder",
  "totalBookings", "totalCallsDatePassed", "confirmed", "shows", "noShows",
  "canceled", "won", "disqualified", "qualified",
  "leadQualityGreat", "leadQualityOk", "leadQualityBarelyPassable", "leadQualityBadDq",
]);

function MetricCell({
  value, col, bold, boundary, webinarNumber, webinarId, assignmentId, listLabel, rowMetrics,
}: {
  value: number | null | undefined;
  col: MetricColumn;
  bold?: boolean;
  boundary?: boolean;
  webinarNumber?: number;
  /** Webinar UUID — preferred for drilldown so A/B variants stay separate.
   * The drilldown URL falls back to ?webinar={number} when this is absent. */
  webinarId?: string | null;
  assignmentId?: string | null;
  listLabel?: string | null;
  rowMetrics?: Record<string, number | null>;
}) {
  const formatted = formatMetricValue(value, col);
  const isNull = value === null || value === undefined;
  const isNonZero = typeof value === "number" && value > 0;
  const drillable = (webinarId != null || webinarNumber != null) && DRILLDOWN_KEYS.has(col.key) && isNonZero;

  // Source-missing warning: only fires when the cell is null AND the metric
  // has declared formulaSources AND at least one of those sources is null on
  // this row. Zero-valued sources are NOT warnings — that's "no data yet",
  // not "data missing because not synced".
  const missingSources: string[] =
    isNull && col.formulaSources && rowMetrics
      ? col.formulaSources.filter((k) => rowMetrics[k] === null || rowMetrics[k] === undefined)
      : [];
  const hasWarning = missingSources.length > 0;

  const content = drillable ? (
    <a
      href={(() => {
        const qs = new URLSearchParams({ metric: col.key });
        if (webinarId) qs.set("webinar_id", webinarId);
        else if (webinarNumber != null) qs.set("webinar", String(webinarNumber));
        if (assignmentId) qs.set("assignment", assignmentId);
        if (listLabel) qs.set("list", listLabel);
        return `/statistics/contacts?${qs.toString()}`;
      })()}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      className="hover:text-violet-500 dark:hover:text-violet-400 underline underline-offset-2 decoration-dotted decoration-zinc-400/40"
      title={`Click to see the ${col.group} · ${col.label} contacts`}
    >
      {formatted}
    </a>
  ) : (
    formatted
  );

  return (
    <td className={`px-2 py-1.5 text-right font-mono whitespace-nowrap ${
      bold ? "font-bold" : ""
    } ${isNull ? "text-zinc-400" : bold ? "text-zinc-800 dark:text-zinc-200" : "text-zinc-700 dark:text-zinc-300"} ${
      boundary ? GROUP_BOUNDARY_CLASSES : ""
    }`}>
      {hasWarning && (
        <span
          title={`Formula uses ${col.formulaSources!.join(", ")}; missing/not synced: ${missingSources.join(", ")}`}
          className="mr-1 text-amber-500 cursor-help"
          aria-label="Source data missing"
        >
          ⚠
        </span>
      )}
      {content}
    </td>
  );
}

/* ─── External link icon ──────────────────────────────────────────────── */

/* ─── Copy (title + description) preview + modal ─────────────────────── */

function VariantBadge({ idx, kind }: { idx: number; kind: "title" | "desc" }) {
  const colors = kind === "title"
    ? "bg-violet-500/15 text-violet-500 border-violet-500/30"
    : "bg-sky-500/15 text-sky-500 border-sky-500/30";
  return (
    <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border ${colors}`}>
      V{idx + 1}
    </span>
  );
}

function CopyCell({ row, onClick }: { row: ApiStatisticsRow; onClick: () => void }) {
  const t = row.titleCopy;
  const d = row.descCopy;
  if (!t && !d) return <span className="text-zinc-600">—</span>;
  return (
    <div
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      className="cursor-pointer group/copy max-w-[260px]"
      title="Click to view full title + description"
    >
      {t && (
        <div className="flex items-center gap-1 mb-0.5">
          <VariantBadge idx={t.variantIndex} kind="title" />
          <span className="text-[10px] text-zinc-700 dark:text-zinc-300 truncate group-hover/copy:text-violet-500 dark:group-hover/copy:text-violet-400">
            {t.text}
          </span>
        </div>
      )}
      {d && (
        <div className="flex items-center gap-1">
          <VariantBadge idx={d.variantIndex} kind="desc" />
          <span className="text-[10px] text-zinc-600 dark:text-zinc-400 truncate group-hover/copy:text-sky-500 dark:group-hover/copy:text-sky-400">
            {d.text.split("\n")[0]}
          </span>
        </div>
      )}
    </div>
  );
}

function CopyModal({ row, onClose }: { row: ApiStatisticsRow; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-2xl w-[90vw] max-h-[80vh] overflow-y-auto"
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800">
          <div>
            <div className="text-xs text-zinc-500 uppercase tracking-wider font-semibold mb-0.5">Copy used for list</div>
            <div className="text-sm font-bold text-zinc-900 dark:text-zinc-100">
              {row.description ?? row.listName ?? "—"}
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
            aria-label="Close"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18M6 6l12 12"/>
            </svg>
          </button>
        </div>
        <div className="px-6 py-4 space-y-4">
          <section>
            <div className="flex items-center gap-2 mb-2">
              <VariantBadge idx={row.titleCopy?.variantIndex ?? 0} kind="title" />
              <span className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">Title</span>
            </div>
            {row.titleCopy ? (
              <p className="text-sm text-zinc-800 dark:text-zinc-200 whitespace-pre-wrap">{row.titleCopy.text}</p>
            ) : (
              <p className="text-sm text-zinc-500 italic">No title copy set</p>
            )}
          </section>
          <section>
            <div className="flex items-center gap-2 mb-2">
              <VariantBadge idx={row.descCopy?.variantIndex ?? 0} kind="desc" />
              <span className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">Description</span>
            </div>
            {row.descCopy ? (
              <p className="text-sm text-zinc-800 dark:text-zinc-200 whitespace-pre-wrap">{row.descCopy.text}</p>
            ) : (
              <p className="text-sm text-zinc-500 italic">No description copy set</p>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

function ExternalLinkIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  );
}

/* ─── Placeholder builder ────────────────────────────────────────────── */

const EMPTY_METRICS = {} as StatisticsMetrics;

/** Build an `ApiStatisticsWebinar` shell for a not-yet-loaded webinar so the
 * existing render code can lay out the parent row. Metric cells render as
 * "—" until the per-webinar fetch lands and replaces this entry. */
function placeholderWebinar(
  s: ApiStatisticsWebinarSummary,
  source: StatisticsMeta["source"],
): ApiStatisticsWebinar {
  return {
    id: s.id,
    webinarId: s.webinarId,
    number: s.number,
    variantLabel: s.variantLabel,
    date: s.date,
    title: s.title,
    workbookRow: 0,
    source: source === "workbook" ? "workbook_mock" : "ghl",
    summary: EMPTY_METRICS,
    usedFallback: false,
    hasSiblingVariants: false,
    rows: [],
  };
}

/* ─── Main Component ──────────────────────────────────────────────────── */

/* ─── Bucket-grouped child-row renderer ─────────────────────────────── */

/** Metric formats whose values are raw counts (or sum-by-design currency),
 * so summing across a bucket's lists is meaningful. Percentages, per-1k,
 * and ratio columns intentionally stay blank on bucket headers — averaging
 * those across child rows would be misleading. */
const SUMMABLE_FORMATS = new Set<string>(["number"]);

function sumMetric(rows: ApiStatisticsRow[], key: string): number {
  let total = 0;
  for (const r of rows) {
    const v = r.metrics[key];
    if (typeof v === "number") total += v;
  }
  return total;
}

/**
 * Render child rows for an expanded webinar, grouped by bucket like the
 * Planning page: multi-list buckets collapse under a header row, single-list
 * buckets go into a synthetic "Unique Buckets" group, and Nonjoiners /
 * NO LIST DATA rows render below as-is.
 */
function renderGroupedRows(
  w: ApiStatisticsWebinar,
  collapsedBuckets: Set<string>,
  toggleBucketGroup: (webinarId: string, groupKey: string) => void,
  setCopyModalRow: (row: ApiStatisticsRow) => void,
  sortKey: string | null,
  sortDir: "asc" | "desc",
): ReactNode[] {
  // Sort comparator: numeric metric value, null last, respecting asc/desc.
  const cmp = (a: ApiStatisticsRow, b: ApiStatisticsRow): number => {
    if (!sortKey) return 0;
    const av = a.metrics[sortKey];
    const bv = b.metrics[sortKey];
    const aNum = typeof av === "number" ? av : null;
    const bNum = typeof bv === "number" ? bv : null;
    if (aNum === null && bNum === null) return 0;
    if (aNum === null) return 1;  // nulls always last
    if (bNum === null) return -1;
    return sortDir === "desc" ? bNum - aNum : aNum - bNum;
  };
  type Group = { bucketId: string; bucketName: string; lists: ApiStatisticsRow[] };

  const groups: Group[] = [];
  const seen = new Map<string, number>();
  const unbucketed: ApiStatisticsRow[] = [];
  const specials: ApiStatisticsRow[] = [];

  for (const r of w.rows) {
    if (r.kind !== "list") { specials.push(r); continue; }
    if (!r.bucketId) { unbucketed.push(r); continue; }
    const idx = seen.get(r.bucketId);
    if (idx !== undefined) {
      groups[idx].lists.push(r);
    } else {
      seen.set(r.bucketId, groups.length);
      groups.push({
        bucketId: r.bucketId,
        bucketName: r.bucketName ?? r.description ?? "Bucket",
        lists: [r],
      });
    }
  }

  const multi = groups.filter((g) => g.lists.length >= 2);
  const single = groups.filter((g) => g.lists.length === 1).map((g) => g.lists[0]);

  // Apply per-webinar sort when active. Sort within each multi-list bucket,
  // sort the "Unique Buckets" bundle, and sort unbucketed rows. Specials
  // (Nonjoiners / NO LIST DATA) stay at the bottom; we also sort them for
  // consistency (useful when the same column is clicked across webinars).
  if (sortKey) {
    for (const g of multi) g.lists = [...g.lists].sort(cmp);
    single.sort(cmp);
    unbucketed.sort(cmp);
    specials.sort(cmp);
  }

  const renderListRow = (row: ApiStatisticsRow) => {
    const isSpecial = row.kind !== "list";
    const num = isSpecial ? sNumSp : sNumL;
    const desc = isSpecial ? sDescSp : sDescL;
    const copy = isSpecial ? sCopySp : sCopyL;
    const url = isSpecial ? sUrlSp : sUrlL;
    const send = isSpecial ? sSendSp : sSendL;
    return (
    <tr
      key={row.id}
      className={`border-b border-zinc-200 dark:border-zinc-800/20 transition-colors ${
        isSpecial
          ? "bg-zinc-50 dark:bg-zinc-900/20 text-zinc-500 italic"
          : "hover:bg-zinc-100 dark:hover:bg-zinc-800/20"
      }`}
    >
      <td className="px-2 py-1.5"></td>
      <td className={`px-2 py-1.5 ${W_NUM} ${num}`}></td>
      <td className="px-2 py-1.5">
        {!isSpecial && <StatusBadge status={row.status} />}
      </td>
      <td className="px-2 py-1.5">
        <span className={isSpecial ? "text-zinc-500" : "text-zinc-700 dark:text-zinc-300"}>
          {row.note ?? ""}
        </span>
      </td>
      <td className={`px-2 py-1.5 ${W_DESC} ${desc}`}>
        <span className={`block truncate ${isSpecial ? "text-zinc-500" : "text-zinc-800 dark:text-zinc-300"}`} title={row.description ?? undefined}>
          {row.description ?? (row.kind === "nonjoiners" ? "Nonjoiners" : row.kind === "no_list_data" ? "NO LIST DATA" : "")}
          {row.kind === "no_list_data" && row.sharedAcrossVariants && (
            <span
              title={
                "GHL invite-response and booked-call signals are stored per webinar number, not per A/B variant. " +
                "These leftover counts therefore appear on both variants' NO LIST DATA rows. " +
                "WebinarGeek-derived counts (registrations, attendance) are correctly scoped per broadcast."
              }
              className="ml-1.5 inline-block px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-500/15 text-amber-500 border border-amber-500/30 align-middle not-italic"
            >
              shared signals
            </span>
          )}
        </span>
      </td>
      <td className={`px-2 py-1.5 ${W_COPY} ${copy}`}>
        <CopyCell row={row} onClick={() => setCopyModalRow(row)} />
      </td>
      <td className={`px-2 py-1.5 text-center ${W_URL} ${url}`}>
        {row.listUrl && (
          <a
            href={row.listUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            title={row.listUrl}
            className="text-violet-400 hover:text-violet-300"
          >
            <ExternalLinkIcon />
          </a>
        )}
      </td>
      <td className={`px-2 py-1.5 text-zinc-600 dark:text-zinc-400 ${W_SEND} ${send}`}>
        <span className="block truncate" title={row.sendInfo ?? undefined}>
          {row.sendInfo ?? ""}
        </span>
      </td>
      {METRIC_COLUMNS.map((col, idx) => (
        <MetricCell
          key={col.key}
          value={row.metrics[col.key]}
          col={col}
          boundary={isGroupBoundary(idx)}
          webinarNumber={w.number}
          webinarId={w.webinarId}
          assignmentId={row.assignmentId}
          listLabel={row.description}
          rowMetrics={row.metrics}
        />
      ))}
    </tr>
    );
  };

  const renderGroupHeader = (groupKey: string, bucketName: string, lists: ApiStatisticsRow[], italic = false) => {
    const key = `${w.id}::${groupKey}`;
    const collapsed = collapsedBuckets.has(key);
    const uniqSenders: { name: string; color: string | null }[] = [];
    const seenSenders = new Set<string>();
    for (const l of lists) {
      if (l.sendInfo && !seenSenders.has(l.sendInfo)) {
        seenSenders.add(l.sendInfo);
        uniqSenders.push({ name: l.sendInfo, color: l.senderColor });
      }
    }
    const summed: Record<string, number> = {};
    for (const col of METRIC_COLUMNS) {
      if (SUMMABLE_FORMATS.has(col.format)) {
        summed[col.key] = sumMetric(lists, col.key);
      }
    }

    return (
      <tr
        key={`bucket-${groupKey}`}
        onClick={() => toggleBucketGroup(w.id, groupKey)}
        className="bg-zinc-100/70 dark:bg-zinc-800/25 hover:bg-zinc-200/70 dark:hover:bg-zinc-800/45 cursor-pointer border-b border-zinc-200 dark:border-zinc-800/30 transition-colors"
      >
        <td className="px-2 py-2 text-center">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
            className={`text-zinc-500 dark:text-zinc-400 transition-transform duration-200 ${collapsed ? "" : "rotate-90"}`}>
            <path d="M9 18l6-6-6-6"/>
          </svg>
        </td>
        <td className={`px-2 py-2 ${W_NUM} ${sNumG}`}></td>
        <td className="px-2 py-2"></td>
        <td className="px-2 py-2"></td>
        <td className={`px-2 py-2 ${W_DESC} ${sDescG}`}>
          <div className="flex items-center gap-2">
            <span
              title={bucketName}
              className={`text-zinc-800 dark:text-zinc-100 text-xs font-bold truncate ${italic ? "italic" : ""}`}
            >
              {bucketName}
            </span>
            <span className="text-[10px] font-semibold text-zinc-500 dark:text-zinc-400 px-1.5 py-0.5 rounded bg-zinc-200 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/40">
              {lists.length}
            </span>
          </div>
        </td>
        <td className={`px-2 py-2 ${W_COPY} ${sCopyG}`}></td>
        <td className={`px-2 py-2 ${W_URL} ${sUrlG}`}></td>
        <td className={`px-2 py-2 ${W_SEND} ${sSendG}`}>
          <div className="flex items-center gap-1">
            {uniqSenders.slice(0, 2).map((s) => (
              <span
                key={s.name}
                title={s.name}
                className="text-[9px] font-semibold px-1.5 py-0.5 rounded border"
                style={s.color ? { color: s.color, borderColor: s.color, backgroundColor: `${s.color}15` } : undefined}
              >
                {s.name}
              </span>
            ))}
            {uniqSenders.length > 2 && (
              <span className="text-[9px] text-zinc-500 font-semibold">+{uniqSenders.length - 2}</span>
            )}
          </div>
        </td>
        {METRIC_COLUMNS.map((col, idx) => {
          const isSummable = SUMMABLE_FORMATS.has(col.format);
          const val = isSummable ? summed[col.key] : 0;
          const show = isSummable && val > 0;
          return (
            <td
              key={col.key}
              className={`px-2 py-2 text-right font-mono font-bold whitespace-nowrap ${
                show ? "text-zinc-800 dark:text-zinc-100" : "text-zinc-500"
              } ${isGroupBoundary(idx) ? GROUP_BOUNDARY_CLASSES : ""}`}
            >
              {show ? formatMetricValue(val, col) : ""}
            </td>
          );
        })}
      </tr>
    );
  };

  const nodes: ReactNode[] = [];

  // 1) Multi-list bucket groups
  for (const g of multi) {
    const key = `${w.id}::${g.bucketId}`;
    const collapsed = collapsedBuckets.has(key);
    nodes.push(renderGroupHeader(g.bucketId, g.bucketName, g.lists));
    if (!collapsed) g.lists.forEach((l) => nodes.push(renderListRow(l)));
  }

  // 2) "Unique Buckets" — synthetic group for single-list buckets
  if (single.length > 0) {
    const uniqueKey = "__unique__";
    const collapsed = collapsedBuckets.has(`${w.id}::${uniqueKey}`);
    nodes.push(renderGroupHeader(uniqueKey, "Unique Buckets", single, true));
    if (!collapsed) single.forEach((l) => nodes.push(renderListRow(l)));
  }

  // 3) Unbucketed lists (shouldn't normally exist but render safely)
  for (const l of unbucketed) nodes.push(renderListRow(l));

  // 4) Special rows (Nonjoiners / NO LIST DATA) — always visible, italic
  for (const l of specials) nodes.push(renderListRow(l));

  return nodes;
}


export function StatisticsPage() {
  const [webinars, setWebinars] = useState<ApiStatisticsWebinar[]>([]);
  /** Lightweight summary (status + listCount) for rows whose full metrics
   * haven't loaded yet. Keyed by the synthetic `id` (e.g. "stat-w136" or
   * "stat-w136-Account A") so A/B variants of the same number don't collide. */
  const [summariesById, setSummariesById] = useState<Map<string, ApiStatisticsWebinarSummary>>(new Map());
  /** Synthetic ids whose per-webinar fetch is still in flight. */
  const [loadingIds, setLoadingIds] = useState<Set<string>>(new Set());
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [meta, setMeta] = useState<StatisticsMeta | null>(null);
  const [syncingWebinar, setSyncingWebinar] = useState<number | null>(null);
  const [collapsedBuckets, setCollapsedBuckets] = useState<Set<string>>(new Set());
  const [copyModalRow, setCopyModalRow] = useState<ApiStatisticsRow | null>(null);
  const [infoModalCol, setInfoModalCol] = useState<MetricColumn | null>(null);
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  /** Sender filter — empty string means "all senders". When set, only list
   * rows whose sendInfo equals this value are shown, special rows (Nonjoiners /
   * NO LIST DATA) are hidden, and webinars with no matching rows drop out. */
  const [senderFilter, setSenderFilter] = useState<string>("");
  /** Statistics chat panel — slides in from the right. Conversation lives in
   * the panel's own state; closing the panel does not clear it, but a page
   * refresh does. */
  const [chatOpen, setChatOpen] = useState(false);

  const toggleSort = (key: string) => {
    if (sortKey === key) {
      // Cycle: desc -> asc -> off
      if (sortDir === "desc") setSortDir("asc");
      else { setSortKey(null); }
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const toggleBucketGroup = (webinarId: string, groupKey: string) => {
    setCollapsedBuckets((prev) => {
      const key = `${webinarId}::${groupKey}`;
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleWebinarSync = async (webinarNumber: number, e: React.MouseEvent) => {
    e.stopPropagation();
    if (syncingWebinar !== null) return;
    setSyncingWebinar(webinarNumber);
    try {
      await triggerGhlWebinarSync(webinarNumber);
      alert(`Webinar ${webinarNumber} sync started. Track progress on the Sync page.`);
    } catch (err) {
      alert(err instanceof Error ? err.message : `Failed to start webinar ${webinarNumber} sync`);
    } finally {
      setSyncingWebinar(null);
    }
  };

  /* ── Per-webinar WG + GHL sync ─────────────────────────────────── */
  const [wgSelected, setWgSelected] = useState<string>("");
  const [wgSyncing, setWgSyncing] = useState(false);
  const [wgMessage, setWgMessage] = useState<string | null>(null);

  async function handleWgSync() {
    if (!wgSelected) return;
    // wgSelected is now the synthetic id (per variant). The dropdown label
    // shows the user-facing number + variant; under the hood we use the id
    // so two siblings on the same number target the right rows.
    const summary = summariesById.get(wgSelected);
    if (!summary) return;
    setWgSyncing(true);
    setWgMessage(null);
    try {
      const parts: string[] = [];
      if (summary.broadcastId) {
        await syncWgSubscribers(summary.broadcastId);
        parts.push("WG sync started");
      } else {
        parts.push("WG: no broadcast linked");
      }
      // GHL sync is keyed on webinar number — both variants of the same
      // number share the same GHL pull, so this triggers either variant's
      // backing data correctly.
      await triggerGhlWebinarSync(summary.number);
      parts.push("GHL sync started");
      setWgMessage(parts.join(" · "));
    } catch (e) {
      setWgMessage(e instanceof Error ? e.message : "Sync failed");
    } finally {
      setWgSyncing(false);
    }
  }

  /* ── Progressive load ───────────────────────────────────────────────
   * 1. Fetch the lightweight list — renders parent rows immediately with
   *    "—" cells for unloaded metrics.
   * 2. Drop future webinars (date > today, or today-but-not-yet-sent):
   *    they have no real data yet.
   * 3. Load webinars one at a time, date-desc (latest first). Per-webinar
   *    queries are ~30s each; loading them in parallel overloads the DB
   *    and pushes individual requests past Render's ~100s edge timeout,
   *    which manifests as CORS errors in the browser. Serial loading is
   *    slower in total wall time but every request completes, and the
   *    backend response cache makes subsequent loads fast anyway. */
  useEffect(() => {
    let cancelled = false;
    // 1 = strictly serial after the priority head. Do not raise without
    // verifying the deployed backend's per-webinar latency × concurrency
    // stays under Render's edge timeout (~100s).
    const CONCURRENCY = 1;

    async function load() {
      let summaries: ApiStatisticsWebinarSummary[];
      let metaResp: StatisticsMeta;
      try {
        const res = await fetchStatisticsWebinarList();
        summaries = res.webinars;
        metaResp = res.meta;
      } catch (err) {
        console.error("Failed to load statistics list:", err);
        if (!cancelled) setLoading(false);
        return;
      }
      if (cancelled) return;

      // "Passed" = webinar date < today, OR date == today AND status == "sent".
      // Webinar.date is a SQL Date column, so the API returns "YYYY-MM-DD";
      // lexicographic compare matches chronological order. A webinar dated
      // today that hasn't been marked sent yet hasn't actually run — its
      // stats would all be zeros, so we drop it to save the per-webinar
      // fetch.
      const today = new Date();
      const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
      summaries = summaries.filter((s) => {
        if (s.date == null) return false;
        if (s.date < todayStr) return true;
        if (s.date > todayStr) return false;
        return (s.status ?? "").toLowerCase() === "sent";
      });

      // Sort: date desc (most-recently-passed first), variant label asc so
      // sibling A/B variants stay grouped together.
      summaries.sort((a, b) => {
        const da = a.date ?? "";
        const db = b.date ?? "";
        if (db !== da) return db.localeCompare(da);
        return (a.variantLabel ?? "").localeCompare(b.variantLabel ?? "");
      });
      setMeta(metaResp);
      setSummariesById(new Map(summaries.map((s) => [s.id, s])));
      setWebinars(summaries.map((s) => placeholderWebinar(s, metaResp.source)));
      setLoadingIds(new Set(summaries.map((s) => s.id)));
      if (summaries.length > 0) {
        setExpandedIds(new Set([summaries[0].id]));
      }
      setLoading(false);

      const loadOne = async (id: string, webinarId: string | null) => {
        if (!webinarId) {
          // Workbook source has no UUID — its synthetic id is the route
          // segment the backend expects. Fall back to passing the id as the
          // resource identifier; the backend matches on the synthetic id
          // for workbook rows.
          webinarId = id;
        }
        try {
          const w = await fetchStatisticsWebinar(webinarId);
          if (cancelled) return;
          setWebinars((prev) => prev.map((p) => (p.id === id ? w : p)));
        } catch (err) {
          console.error(`Failed to load webinar ${id}:`, err);
        } finally {
          if (!cancelled) {
            setLoadingIds((prev) => {
              const next = new Set(prev);
              next.delete(id);
              return next;
            });
          }
        }
      };

      // Priority head: load the most-recently-passed webinar alone first
      // so it always appears before older webinars on the page, regardless
      // of which query happens to complete first on the backend.
      if (summaries.length > 0) {
        await loadOne(summaries[0].id, summaries[0].webinarId);
        if (cancelled) return;
      }

      // Worker pool for the rest.
      const queue = summaries.slice(1);
      const workers = Array.from(
        { length: Math.min(CONCURRENCY, queue.length) },
        async () => {
          while (queue.length > 0 && !cancelled) {
            const s = queue.shift();
            if (s !== undefined) await loadOne(s.id, s.webinarId);
          }
        },
      );
      await Promise.all(workers);
    }
    load();
    return () => { cancelled = true; };
  }, []);

  /* ── Unique senders (for the filter dropdown) ──────────────────── */
  const allSenders = useMemo(() => {
    const seen = new Map<string, string | null>();
    for (const w of webinars) {
      for (const r of w.rows) {
        if (r.sendInfo && !seen.has(r.sendInfo)) {
          seen.set(r.sendInfo, r.senderColor);
        }
      }
    }
    return Array.from(seen.entries())
      .map(([name, color]) => ({ name, color }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [webinars]);

  /* ── Search + sender filter ────────────────────────────────────── */
  const filteredWebinars = useMemo(() => {
    let list = webinars;
    if (senderFilter) {
      list = list
        .map((w) => ({
          ...w,
          rows: w.rows.filter((r) => r.kind === "list" && r.sendInfo === senderFilter),
        }))
        .filter((w) => w.rows.length > 0);
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      list = list.filter((w) => {
        if (String(w.number).includes(q)) return true;
        if (w.variantLabel?.toLowerCase().includes(q)) return true;
        if (w.title?.toLowerCase().includes(q)) return true;
        if (w.date?.toLowerCase().includes(q)) return true;
        return w.rows.some(
          (r) =>
            r.description?.toLowerCase().includes(q) ||
            r.note?.toLowerCase().includes(q) ||
            r.sendInfo?.toLowerCase().includes(q)
        );
      });
    }
    return list;
  }, [webinars, searchQuery, senderFilter]);

  /** When a sender filter is active, force-expand every matching webinar so
   * the user immediately sees the filtered lists (otherwise the filter looks
   * like it did nothing on collapsed rows). Plain `expandedIds` is preserved
   * for when the filter is cleared. */
  const effectiveExpandedIds = useMemo(() => {
    if (!senderFilter) return expandedIds;
    return new Set(filteredWebinars.map((w) => w.id));
  }, [senderFilter, expandedIds, filteredWebinars]);

  /* ── Global summary stats ───────────────────────────────────────── */
  const globalStats = useMemo(() => {
    const totalInvited = webinars.reduce((s, w) => s + (w.summary.invited ?? 0), 0);
    const totalAttended = webinars.reduce((s, w) => s + (w.summary.totalAttended ?? 0), 0);
    const totalBookings = webinars.reduce((s, w) => s + (w.summary.totalBookings ?? 0), 0);
    const totalWon = webinars.reduce((s, w) => s + (w.summary.won ?? 0), 0);
    return { totalInvited, totalAttended, totalBookings, totalWon };
  }, [webinars]);

  /* ── Toggle expand ──────────────────────────────────────────────── */
  const toggleExpand = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  /* ── Loading state ──────────────────────────────────────────────── */
  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="flex items-center gap-3">
          <div className="w-4 h-4 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
          <span className="text-sm text-zinc-500">Loading statistics...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col" style={{ height: "calc(100vh - 48px)" }}>
      {/* ── Sticky header ──────────────────────────────────────────── */}
      <div className="flex-none z-40 bg-white dark:bg-zinc-950/90 backdrop-blur-md border-b border-zinc-200 dark:border-zinc-800/40 px-6 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-6">
            <h1 className="text-lg font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Statistics</h1>
            {meta && (
              <span
                className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${
                  meta.source === "ghl"
                    ? "bg-violet-500/15 text-violet-400 border-violet-500/30"
                    : "bg-zinc-500/15 text-zinc-400 border-zinc-500/30"
                }`}
                title={
                  meta.source === "ghl" && meta.last_sync?.completed_at
                    ? `Last synced: ${new Date(meta.last_sync.completed_at).toLocaleString()}`
                    : meta.source === "ghl"
                    ? "GHL sync running"
                    : "Using workbook fixture (no GHL sync yet)"
                }
              >
                {meta.source === "ghl"
                  ? `GHL · synced ${meta.last_sync?.completed_at ? new Date(meta.last_sync.completed_at).toLocaleDateString() : "—"}`
                  : "Workbook"}
              </span>
            )}
            <div className="flex gap-2">
              {[
                { label: "Webinars", value: webinars.length, color: "text-zinc-800 dark:text-zinc-200" },
                { label: "Invited", value: globalStats.totalInvited.toLocaleString(), color: "text-violet-400" },
                { label: "Attended", value: globalStats.totalAttended.toLocaleString(), color: "text-emerald-400" },
                { label: "Bookings", value: globalStats.totalBookings, color: "text-amber-400" },
                { label: "Won", value: globalStats.totalWon, color: "text-sky-400" },
              ].map((s) => (
                <div key={s.label} className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-zinc-50 dark:bg-zinc-900/60 border border-zinc-200 dark:border-zinc-800/40">
                  <span className={`text-sm font-bold font-mono ${s.color}`}>{s.value}</span>
                  <span className="text-[10px] text-zinc-500 uppercase tracking-wider">{s.label}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-3">
            {webinars.length > 0 && (
              <div className="flex items-center gap-2">
                <select
                  value={wgSelected}
                  onChange={(e) => {
                    setWgSelected(e.target.value);
                    setWgMessage(null);
                  }}
                  className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-2 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 max-w-[320px]"
                >
                  <option value="">Select webinar to sync…</option>
                  {webinars.map((w) => {
                    const summary = summariesById.get(w.id);
                    const dateLabel = w.date ? new Date(w.date).toLocaleDateString() : "—";
                    const titleLabel = w.title ? ` · ${w.title}` : "";
                    const variantLabel = w.variantLabel ? ` · ${w.variantLabel}` : "";
                    const noBroadcast = !summary?.broadcastId ? " · no WG broadcast" : "";
                    return (
                      <option key={w.id} value={w.id}>
                        #{w.number}{variantLabel} · {dateLabel}{titleLabel}{noBroadcast}
                      </option>
                    );
                  })}
                </select>
                <button
                  onClick={handleWgSync}
                  disabled={!wgSelected || wgSyncing}
                  className="px-3 py-1.5 text-xs rounded-lg bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {wgSyncing ? "Syncing..." : "Sync"}
                </button>
                {wgMessage && (
                  <span className="text-[10px] text-zinc-500 max-w-[220px] truncate" title={wgMessage}>
                    {wgMessage}
                  </span>
                )}
              </div>
            )}
            {allSenders.length > 0 && (
              <div className="flex items-center gap-1.5">
                <select
                  value={senderFilter}
                  onChange={(e) => setSenderFilter(e.target.value)}
                  title="Show only lists for a single sender"
                  className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-2 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 max-w-[200px]"
                  style={
                    senderFilter
                      ? {
                          color: allSenders.find((s) => s.name === senderFilter)?.color ?? undefined,
                          borderColor: allSenders.find((s) => s.name === senderFilter)?.color ?? undefined,
                        }
                      : undefined
                  }
                >
                  <option value="">All senders</option>
                  {allSenders.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name}
                    </option>
                  ))}
                </select>
                {senderFilter && (
                  <button
                    type="button"
                    onClick={() => setSenderFilter("")}
                    title="Clear sender filter"
                    className="text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors p-1"
                    aria-label="Clear sender filter"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M18 6L6 18M6 6l12 12" />
                    </svg>
                  </button>
                )}
              </div>
            )}
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search webinars..."
              className="w-56 bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
            />
            <button
              onClick={() => setChatOpen((v) => !v)}
              title="Ask the AI assistant about this data"
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold inline-flex items-center gap-1.5 transition-colors ${
                chatOpen
                  ? "bg-violet-600 text-white"
                  : "bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 text-zinc-700 dark:text-zinc-300 hover:bg-violet-500/10 hover:text-violet-500 hover:border-violet-500/40"
              }`}
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
              </svg>
              Ask AI
            </button>
          </div>
        </div>
      </div>

      {/* ── Table ──────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-auto">
        <table className="w-full text-xs min-w-[3200px]">
          <thead className="sticky top-0 z-30">
            {/* Row 1: Group spans */}
            <tr className="bg-zinc-50 dark:bg-zinc-900/90 border-b border-zinc-100 dark:border-zinc-800/20">
              <th colSpan={IDENTITY_COL_COUNT} className={`px-2 py-1 ${L_NUM} ${Z_HEADER} ${BG_HEADER}`}></th>
              {METRIC_GROUPS.map((g) => (
                <th
                  key={g}
                  colSpan={columnsInGroup(g)}
                  className="text-center px-1 py-1 text-[9px] font-bold uppercase tracking-wider text-zinc-400 border-l border-zinc-200 dark:border-zinc-800/30"
                >
                  {g}
                </th>
              ))}
            </tr>
            {/* Row 2: Individual column labels */}
            <tr className="bg-zinc-50 dark:bg-zinc-900/90 border-b border-zinc-200 dark:border-zinc-800/40">
              <th className="w-8 px-2 py-2"></th>
              <th className={`text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] ${W_NUM} ${sNumH}`}>Webinar #</th>
              <th className="text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Status</th>
              <th className="text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] min-w-[120px]">Note</th>
              <th className={`text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] ${W_DESC} ${sDescH}`}>Description</th>
              <th className={`text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] ${W_COPY} ${sCopyH}`}>Copy</th>
              <th className={`text-center px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] ${W_URL} ${sUrlH}`}>URL</th>
              <th className={`text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] ${W_SEND} ${sSendH}`}>Send Info</th>
              {METRIC_COLUMNS.map((col, idx) => {
                const isSorted = sortKey === col.key;
                const sortArrow = isSorted ? (sortDir === "desc" ? "▼" : "▲") : "";
                return (
                  <th
                    key={col.key}
                    className={`px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] whitespace-nowrap ${
                      isGroupBoundary(idx) ? GROUP_BOUNDARY_CLASSES : ""
                    }`}
                    title={col.description ?? col.formulaText}
                  >
                    <div className="flex items-center justify-end gap-1">
                      <button
                        type="button"
                        onClick={() => toggleSort(col.key)}
                        className={`inline-flex items-center gap-0.5 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors ${
                          isSorted ? "text-violet-500 dark:text-violet-400" : ""
                        }`}
                        title={`Sort by ${col.label} (per webinar)`}
                      >
                        <span>{col.label}</span>
                        {sortArrow && <span className="text-[8px]">{sortArrow}</span>}
                      </button>
                      <button
                        type="button"
                        onClick={() => setInfoModalCol(col)}
                        className="text-zinc-400 hover:text-violet-500 dark:hover:text-violet-400 transition-colors opacity-60 hover:opacity-100"
                        aria-label={`Info about ${col.label}`}
                        title="What does this column track?"
                      >
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <circle cx="12" cy="12" r="10" />
                          <line x1="12" y1="8" x2="12" y2="8.01" />
                          <line x1="12" y1="12" x2="12" y2="16" />
                        </svg>
                      </button>
                    </div>
                  </th>
                );
              })}
            </tr>
          </thead>

          {filteredWebinars.map((w) => {
            const isExpanded = effectiveExpandedIds.has(w.id);
            const isLoading = loadingIds.has(w.id);
            const summary = summariesById.get(w.id);
            // Use rows[] when loaded; fall back to the lightweight summary
            // count / status while metrics are still in flight.
            const listCount = w.rows.length > 0
              ? w.rows.filter((r) => r.kind === "list").length
              : summary?.listCount ?? 0;
            const statusForBadge = w.rows[0]?.status ?? summary?.status ?? null;

            return (
              <tbody key={w.id}>
                {/* ── Parent row ─────────────────────────────────── */}
                <tr
                  className="bg-zinc-100 dark:bg-zinc-800/40 hover:bg-zinc-200 dark:hover:bg-zinc-800/60 cursor-pointer border-t-2 border-zinc-300 dark:border-zinc-700/40 transition-colors"
                  onClick={() => toggleExpand(w.id)}
                >
                  <td className="px-2 py-2.5 text-center">
                    <svg
                      width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                      strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                      className={`text-zinc-600 dark:text-zinc-400 transition-transform duration-200 ${isExpanded ? "rotate-90" : ""}`}
                    >
                      <path d="M9 18l6-6-6-6" />
                    </svg>
                  </td>
                  <td className={`px-2 py-2.5 ${W_NUM} ${sNumP}`}>
                    <div className="flex items-center gap-2">
                      <span className="text-zinc-900 dark:text-zinc-100 font-bold text-sm">{w.number}</span>
                      <span className="text-zinc-500">{w.date ?? "\u2014"}</span>
                      {isLoading && (
                        <span
                          title="Loading metrics…"
                          className="inline-block w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin"
                        />
                      )}
                    </div>
                    {w.variantLabel && (
                      <div className="mt-1">
                        <span
                          title={`A/B variant: ${w.variantLabel}`}
                          className="inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold bg-violet-500/15 text-violet-500 border border-violet-500/30 max-w-[184px] truncate align-middle"
                        >
                          {w.variantLabel}
                        </span>
                      </div>
                    )}
                    {w.title && w.title !== "TOTAL" && (
                      <div className="text-[10px] text-zinc-500 mt-0.5 truncate max-w-[200px]">{w.title}</div>
                    )}
                  </td>
                  <td className="px-2 py-2.5">
                    <StatusBadge status={statusForBadge} />
                  </td>
                  <td className="px-2 py-2.5 text-zinc-500 text-[10px]">
                    <span>{listCount} lists</span>
                    {w.usedFallback && (
                      <span
                        title="Rate metrics use the planned 'Invited' number because no contacts have been marked sent yet for this webinar (or all sent contacts have been released). Mark contacts as used on the planning page for more accurate rates."
                        className="ml-1.5 inline-block px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-500/15 text-amber-500 border border-amber-500/30 align-middle"
                      >
                        uses planned
                      </span>
                    )}
                  </td>
                  <td className={`px-2 py-2.5 ${sDescP}`} colSpan={4}>
                    <button
                      onClick={(e) => handleWebinarSync(w.number, e)}
                      disabled={syncingWebinar !== null}
                      title={`Pull full GHL contact rows (contains e${w.number}) + opportunities for W${w.number}`}
                      className="px-2 py-1 text-[10px] font-semibold rounded bg-violet-500/15 text-violet-500 hover:bg-violet-500/25 border border-violet-500/30 transition-colors disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1"
                    >
                      {syncingWebinar === w.number ? (
                        <>
                          <div className="w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
                          Starting…
                        </>
                      ) : (
                        <>Sync GHL</>
                      )}
                    </button>
                  </td>
                  {METRIC_COLUMNS.map((col, idx) => (
                    <MetricCell
                      key={col.key}
                      value={w.summary[col.key]}
                      col={col}
                      bold
                      boundary={isGroupBoundary(idx)}
                      webinarNumber={w.number}
                      webinarId={w.webinarId}
                      listLabel={`Webinar ${w.number}${w.variantLabel ? " · " + w.variantLabel : ""}`}
                      rowMetrics={w.summary}
                    />
                  ))}
                </tr>

                {/* ── Child rows (bucket-grouped) ─────────────────── */}
                {isExpanded && w.rows.length > 0 &&
                  renderGroupedRows(w, collapsedBuckets, toggleBucketGroup, setCopyModalRow, sortKey, sortDir)}
                {isExpanded && w.rows.length === 0 && (
                  <tr className="bg-white dark:bg-zinc-950 border-b border-zinc-200 dark:border-zinc-800/20">
                    <td colSpan={IDENTITY_COL_COUNT + METRIC_COLUMNS.length} className="px-4 py-6 text-center text-xs text-zinc-500">
                      <span className="inline-flex items-center gap-2">
                        {isLoading && (
                          <span className="inline-block w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
                        )}
                        {isLoading ? "Loading metrics…" : "No data"}
                      </span>
                    </td>
                  </tr>
                )}
              </tbody>
            );
          })}
        </table>
      </div>

      {/* ── Empty state ────────────────────────────────────────────── */}
      {!loading && filteredWebinars.length === 0 && (
        <div className="text-center py-20 text-zinc-500">
          {senderFilter
            ? `No lists from ${senderFilter}${searchQuery ? " match your search" : ""}.`
            : searchQuery
            ? "No webinars match your search."
            : "No statistics data available."}
        </div>
      )}

      {/* ── Copy preview modal ─────────────────────────────────────── */}
      {copyModalRow && (
        <CopyModal row={copyModalRow} onClose={() => setCopyModalRow(null)} />
      )}

      {/* ── Metric info modal ──────────────────────────────────────── */}
      {infoModalCol && (
        <MetricInfoModal col={infoModalCol} onClose={() => setInfoModalCol(null)} />
      )}

      {/* ── AI chat panel ──────────────────────────────────────────── */}
      <ChatPanel open={chatOpen} onClose={() => setChatOpen(false)} webinars={webinars} />
    </div>
  );
}
