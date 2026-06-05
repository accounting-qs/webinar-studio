"use client";

import { useState, useMemo, useCallback, useRef, useEffect, type ReactNode } from "react";
import {
  fetchBuckets, fetchSenders, fetchWebinars, fetchWebinarLists,
  assignBucketToWebinar, createWebinar as apiCreateWebinar,
  updateSender as apiUpdateSender, fetchBucketCopies, generateCopies,
  createSender as apiCreateSender, deleteAssignment as apiDeleteAssignment,
  updateAssignment as apiUpdateAssignment, updateWebinar as apiUpdateWebinar,
  deleteWebinar as apiDeleteWebinar, deleteCopy,
  createCopy as apiCreateCopy, updateCopy as apiUpdateCopy, regenerateCopy as apiRegenerateCopy,
  fetchCustomLists, fetchCustomListCopies, createCustomListCopy as apiCreateCustomListCopy,
  startWebinarListExport, fetchActiveWebinarListExports, fetchLatestWebinarListExport,
  downloadWebinarListExport,
  fetchWgCredentials, fetchWgWebinars, refreshWgWebinars,
  type ApiBucket, type ApiSender, type ApiWebinar, type ApiAssignment, type ApiCopy,
  type ApiCustomList, type ApiWebinarListExportJob, type ApiWgCredential, type WgWebinar,
} from "@/lib/api";
import { VariationsModal, apiCopyToVariant, type CopyVariant } from "../shared/VariationsModal";
import { ReleaseContactsModal } from "./ReleaseContactsModal";

/* ─── Copy link helper ────────────────────────────────────────────────── */

/**
 * Replace occurrences of "Register" and "Unsubscribe" in copy text with
 * hyperlinks to the webinar-level registration and unsubscribe URLs.
 * Case-insensitive match. Returns React nodes.
 */
function linkifyCopyText(
  text: string,
  registrationLink: string,
  unsubscribeLink: string,
): ReactNode {
  if (!registrationLink && !unsubscribeLink) return text;

  // Build a regex that matches whichever words we have links for
  const patterns: string[] = [];
  if (registrationLink) patterns.push("register");
  if (unsubscribeLink) patterns.push("unsubscribe");
  if (patterns.length === 0) return text;

  const regex = new RegExp(`(${patterns.join("|")})`, "gi");
  const parts = text.split(regex);

  if (parts.length === 1) return text;

  return parts.map((part, i) => {
    const lower = part.toLowerCase();
    if (lower === "register" && registrationLink) {
      return (
        <a key={i} href={registrationLink} target="_blank" rel="noopener noreferrer"
          className="text-violet-500 hover:text-violet-400 underline underline-offset-2"
        >{part}</a>
      );
    }
    if (lower === "unsubscribe" && unsubscribeLink) {
      return (
        <a key={i} href={unsubscribeLink} target="_blank" rel="noopener noreferrer"
          className="text-zinc-500 hover:text-zinc-400 underline underline-offset-2"
        >{part}</a>
      );
    }
    return part;
  });
}

/**
 * Produces an HTML string version of copy text with hyperlinks preserved,
 * so pasting into rich-text editors (email clients, docs) keeps the links.
 */
function linkifyToHtml(
  text: string,
  registrationLink: string,
  unsubscribeLink: string,
): string {
  // Escape HTML entities first
  let html = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // Convert newlines to <br> so formatting is preserved in rich-text paste
  html = html.replace(/\n/g, "<br>");

  // Linkify keywords
  const patterns: string[] = [];
  if (registrationLink) patterns.push("register");
  if (unsubscribeLink) patterns.push("unsubscribe");

  if (patterns.length > 0) {
    const regex = new RegExp(`(${patterns.join("|")})`, "gi");
    html = html.replace(regex, (match) => {
      const lower = match.toLowerCase();
      if (lower === "register" && registrationLink) {
        return `<a href="${registrationLink}">${match}</a>`;
      }
      if (lower === "unsubscribe" && unsubscribeLink) {
        return `<a href="${unsubscribeLink}">${match}</a>`;
      }
      return match;
    });
  }

  return html;
}

/* ─── Types ──────────────��──────────────────────────────��──────────────── */

// AvailableBucket now uses ApiBucket from API
type AvailableBucket = ApiBucket;

const isDisqualifiedBucket = (b: { name: string }) =>
  b.name.trim().toLowerCase() === "disqualified";

interface PlannedList {
  id: string;
  webinarId: string;
  bucket: string;
  description: string;
  listUrl: string;
  sender: string;
  listSize: number;
  listRemain: number;
  // Blocklist breakdown for tooltip
  listVolumeRaw: number;
  listBlocklistedTotal: number;
  title: string;
  accountsNeeded: number;
  listName?: string;
  isNonjoiners?: boolean;
  isNoListData?: boolean;
  isSetup?: boolean;
  sourceType?: string;
  sourceUploadId?: string;
  // Copy variants
  titleVariants?: { id: string; text: string; selected: boolean; variantIndex: number }[];
  descVariants?: { id: string; text: string; selected: boolean; variantIndex: number }[];
  copiesGenerated?: boolean;
  bucketId?: string;
  senderId?: string;
  senderColor?: string;
}

interface Webinar {
  id: string;
  number: number;
  date: string;
  isoDate: string;
  status: string;
  broadcastId: string;
  mainTitle: string;
  registrationLink: string;
  unsubscribeLink: string;
  lists: PlannedList[];
  expanded: boolean;
  /** Free-text A/B variant label, e.g. "Account A". null if this webinar
   * is the unique row for its number. */
  variantLabel: string | null;
  /** ConnectorCredential.id for the WebinarGeek account this variant
   * uses for sync. null → use the credential row named 'default'. */
  webinargeekCredentialId: string | null;
}

interface Sender {
  id: string;
  name: string;
  accounts: number;
  sendPerAccount: number;
  daysPerWeek: number;
  color: string;
}

// Map API sender to local Sender interface
function apiSenderToLocal(s: ApiSender): Sender {
  return {
    id: s.id,
    name: s.name,
    accounts: s.total_accounts,
    sendPerAccount: s.send_per_account,
    daysPerWeek: s.days_per_webinar,
    color: s.color || "zinc",
  };
}

/* ─── Sender + Badge Helpers ───────────────────────────────────────────── */

// Map DB color names to Tailwind badge classes
const COLOR_CLASS_MAP: Record<string, string> = {
  violet: "bg-violet-500/15 text-violet-400 border-violet-500/25",
  blue: "bg-blue-500/15 text-blue-400 border-blue-500/25",
  emerald: "bg-emerald-500/15 text-emerald-400 border-emerald-500/25",
  amber: "bg-amber-500/15 text-amber-400 border-amber-500/25",
  cyan: "bg-cyan-500/15 text-cyan-400 border-cyan-500/25",
  rose: "bg-rose-500/15 text-rose-400 border-rose-500/25",
  orange: "bg-orange-500/15 text-orange-400 border-orange-500/25",
  teal: "bg-teal-500/15 text-teal-400 border-teal-500/25",
  pink: "bg-pink-500/15 text-pink-400 border-pink-500/25",
  indigo: "bg-indigo-500/15 text-indigo-400 border-indigo-500/25",
};
const DEFAULT_BADGE_CLS = "bg-zinc-200 dark:bg-zinc-700/30 text-zinc-600 dark:text-zinc-400 border-zinc-600/30";

function CopyVariantButton({ text, tone, registrationLink, unsubscribeLink }: { text: string; tone: "title" | "description"; registrationLink?: string; unsubscribeLink?: string }) {
  const [copied, setCopied] = useState(false);
  const tones = tone === "title"
    ? { hover: "hover:text-violet-500 hover:bg-violet-50 dark:hover:bg-violet-500/10", badge: "bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400" }
    : { hover: "hover:text-blue-500 hover:bg-blue-50 dark:hover:bg-blue-500/10", badge: "bg-blue-100 dark:bg-blue-500/15 text-blue-600 dark:text-blue-400" };
  const doCopy = () => {
    const reg = registrationLink || "";
    const unsub = unsubscribeLink || "";
    if ((reg || unsub) && typeof ClipboardItem !== "undefined") {
      try {
        const html = linkifyToHtml(text, reg, unsub);
        const item = new ClipboardItem({
          "text/plain": new Blob([text], { type: "text/plain" }),
          "text/html": new Blob([html], { type: "text/html" }),
        });
        return navigator.clipboard.write([item]);
      } catch {
        return navigator.clipboard.writeText(text);
      }
    }
    return navigator.clipboard.writeText(text);
  };
  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          Promise.resolve(doCopy()).then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          });
        }}
        title={`Copy ${tone}`}
        className={`p-0.5 rounded text-zinc-400 transition-colors ${tones.hover}`}
      >
        <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
      </button>
      {copied && (
        <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${tones.badge}`}>
          Copied
        </span>
      )}
    </span>
  );
}


function SenderBadge({ name, color }: { name: string; color?: string }) {
  if (!name) return <span className="text-zinc-600">—</span>;
  const cls = (color && COLOR_CLASS_MAP[color]) || DEFAULT_BADGE_CLS;
  return <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold border ${cls}`}>{name}</span>;
}


function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, { badge: string; dot: string }> = {
    planning: { badge: "bg-amber-500/10 text-amber-400 border-amber-500/20", dot: "bg-amber-400" },
    sent: { badge: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20", dot: "bg-emerald-400" },
  };
  const c = colors[status.toLowerCase()] || { badge: "bg-zinc-200 dark:bg-zinc-700/30 text-zinc-600 dark:text-zinc-400 border-zinc-600/30", dot: "bg-zinc-400" };
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold border ${c.badge}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${c.dot}`} />
      {status}
    </span>
  );
}

/* ─── Data is now loaded from API ──────────────────────────────────────── */

// Helper: convert ApiAssignment to PlannedList
function apiAssignmentToList(a: ApiAssignment): PlannedList {
  return {
    id: a.id,
    webinarId: a.webinar_id,
    bucket: a.bucket?.name || "—",
    description: a.description || "",
    listName: a.list_name || undefined,
    listUrl: a.list_url || "",
    sender: a.sender?.name || "",
    senderId: a.sender?.id || undefined,
    senderColor: a.sender?.color || undefined,
    listSize: a.volume,
    listRemain: a.remaining,
    listVolumeRaw: a.volume_raw ?? a.volume,
    listBlocklistedTotal: a.blocklisted_total ?? 0,
    title: a.title_copy?.text || "",
    accountsNeeded: a.accounts_used,
    isNonjoiners: a.is_nonjoiners,
    isNoListData: a.is_no_list_data,
    isSetup: a.is_setup,
    sourceType: a.source_type,
    sourceUploadId: a.source_upload_id || undefined,
    copiesGenerated: !!a.title_copy,
    bucketId: a.bucket?.id,
    titleVariants: a.title_copy ? [{ id: a.title_copy.id, text: a.title_copy.text, selected: true, variantIndex: a.title_copy.variant_index }] : undefined,
    descVariants: a.desc_copy ? [{ id: a.desc_copy.id, text: a.desc_copy.text, selected: true, variantIndex: a.desc_copy.variant_index }] : undefined,
  };
}

/* ─── List Name suffix helper ─────────────────────────────────────────── */

/**
 * Generate a default list name from the description + a suffix (1a, 1b, 2c…)
 * when the same bucket is assigned to different senders within a webinar.
 * Suffix = bucket occurrence number + letter (a-z) per sender within that bucket.
 */
function generateListNameSuffix(lists: PlannedList[], currentList: PlannedList): string {
  // Group lists by bucketId (skip special rows)
  const normalLists = lists.filter((l) => !l.isNonjoiners && !l.isNoListData && l.bucketId);
  // Find all lists with the same bucket
  const sameBucket = normalLists.filter((l) => l.bucketId === currentList.bucketId);
  if (sameBucket.length <= 1) return "";
  // Assign a letter suffix based on order within the same bucket
  const idx = sameBucket.findIndex((l) => l.id === currentList.id);
  const letter = String.fromCharCode(97 + (idx >= 0 ? idx : 0)); // a, b, c…
  // Find the bucket's occurrence number (1-based) among unique buckets
  const uniqueBucketIds = [...new Set(normalLists.map((l) => l.bucketId))];
  const bucketNum = uniqueBucketIds.indexOf(currentList.bucketId) + 1;
  return ` ${bucketNum}${letter}`;
}

function getDefaultListName(lists: PlannedList[], list: PlannedList): string {
  const suffix = generateListNameSuffix(lists, list);
  return (list.description || "") + suffix;
}

/* ─── Custom Dropdown ──────────────────────────────────────────────────── */

interface DropdownOption {
  value: string;
  label: string;
}

function Dropdown({
  options,
  value,
  onChange,
  placeholder = "Select...",
  className = "",
}: {
  options: DropdownOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  const selected = options.find((o) => o.value === value);

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-sm text-left flex items-center justify-between gap-2 focus:outline-none focus:ring-1 focus:ring-violet-500 transition-colors"
      >
        <span className={selected ? "text-zinc-800 dark:text-zinc-200 truncate" : "text-zinc-500 truncate"}>
          {selected ? selected.label : placeholder}
        </span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          className={`text-zinc-500 shrink-0 transition-transform duration-150 ${open ? "rotate-180" : ""}`}>
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {open && (
        <div className="absolute left-0 right-0 top-full mt-1 z-50 bg-white dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-lg shadow-xl shadow-black/10 dark:shadow-black/40 max-h-[240px] overflow-y-auto py-1">
          {options.map((o) => (
            <button
              key={o.value}
              type="button"
              onClick={() => { onChange(o.value); setOpen(false); }}
              className={`w-full text-left px-3 py-1.5 text-sm transition-colors ${
                o.value === value
                  ? "bg-violet-500/10 text-violet-600 dark:text-violet-400 font-medium"
                  : "text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800/60"
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ─── Main Component ───────────────────────────────────────────────────── */

export function PlanningPage() {
  const [buckets, setBuckets] = useState<AvailableBucket[]>([]);
  const [senders, setSenders] = useState<Sender[]>([]);
  const [editingSenders, setEditingSenders] = useState(false);
  const [webinars, setWebinars] = useState<Webinar[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [loadingData, setLoadingData] = useState(true);

  /* ── Load all data from API on mount ──────────────────────────────── */
  useEffect(() => {
    let cancelled = false;
    async function loadData() {
      try {
        const [bucketsRes, sendersRes, webinarsRes] = await Promise.all([
          fetchBuckets(), fetchSenders(), fetchWebinars(),
        ]);
        if (cancelled) return;

        setBuckets(bucketsRes.buckets);
        setSenders(sendersRes.senders.map(apiSenderToLocal));

        // Load assignments for each webinar
        const webinarList: Webinar[] = [];
        const allAssignments: ApiAssignment[] = [];
        for (const w of webinarsRes.webinars) {
          const d = new Date(w.date + "T00:00:00");
          const dateStr = d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
          let lists: PlannedList[] = [];
          try {
            const { assignments } = await fetchWebinarLists(w.id);
            allAssignments.push(...assignments);
            lists = assignments.map(apiAssignmentToList);
          } catch { /* no lists yet */ }
          webinarList.push({
            id: w.id,
            number: w.number,
            date: dateStr,
            isoDate: w.date,
            status: w.status.charAt(0).toUpperCase() + w.status.slice(1),
            broadcastId: w.broadcast_id || "—",
            mainTitle: w.main_title || "",
            registrationLink: w.registration_link || "",
            unsubscribeLink: w.unsubscribe_link || "",
            lists,
            expanded: w.status === "planning",
            variantLabel: w.variant_label,
            webinargeekCredentialId: w.webinargeek_credential_id,
          });
        }

        // Load all copy variants for each unique bucket used in assignments
        const uniqueBucketIds = [...new Set(allAssignments.filter(a => a.bucket).map(a => a.bucket!.id))];
        const bucketCopiesMap: Record<string, { titles: ApiCopy[]; descriptions: ApiCopy[] }> = {};
        await Promise.all(uniqueBucketIds.map(async (bucketId) => {
          try {
            const copies = await fetchBucketCopies(bucketId);
            bucketCopiesMap[bucketId] = { titles: copies.titles, descriptions: copies.descriptions };
          } catch { /* no copies yet */ }
        }));

        // Enrich lists with all copy variants from their bucket
        for (const w of webinarList) {
          w.lists = w.lists.map((l) => {
            if (!l.bucketId || !bucketCopiesMap[l.bucketId]) return l;
            const copies = bucketCopiesMap[l.bucketId];
            const selectedTitleId = l.titleVariants?.[0]?.id;
            const selectedDescId = l.descVariants?.[0]?.id;
            return {
              ...l,
              copiesGenerated: copies.titles.length > 0 || copies.descriptions.length > 0,
              titleVariants: copies.titles.map((c) => ({
                id: c.id, text: c.text, selected: c.id === selectedTitleId || (!selectedTitleId && c.is_primary), variantIndex: c.variant_index,
              })),
              descVariants: copies.descriptions.map((c) => ({
                id: c.id, text: c.text, selected: c.id === selectedDescId || (!selectedDescId && c.is_primary), variantIndex: c.variant_index,
              })),
              title: (() => {
                if (selectedTitleId) {
                  const match = copies.titles.find(c => c.id === selectedTitleId);
                  if (match) return match.text;
                }
                const primary = copies.titles.find(c => c.is_primary);
                return primary?.text || l.title;
              })(),
            };
          });
        }

        setWebinars(webinarList);
        // Assignment form is not auto-opened — user clicks "Assign Lists"
      } catch (err) {
        console.error("Failed to load data:", err);
      } finally {
        if (!cancelled) setLoadingData(false);
      }
    }
    loadData();
    return () => { cancelled = true; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const [searchQuery, setSearchQuery] = useState("");

  // Sender filter — persisted to localStorage
  const [senderFilterId, setSenderFilterId] = useState<string>("");
  useEffect(() => {
    try {
      const stored = localStorage.getItem("planning_sender_filter");
      if (stored) setSenderFilterId(stored);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => {
    try {
      if (senderFilterId) localStorage.setItem("planning_sender_filter", senderFilterId);
      else localStorage.removeItem("planning_sender_filter");
    } catch { /* ignore */ }
  }, [senderFilterId]);
  const [showCopyModal, setShowCopyModal] = useState(false);
  const [copyModalLists, setCopyModalLists] = useState<PlannedList[]>([]);
  const [generatingCopies, setGeneratingCopies] = useState(false);
  const [planningCopyModal, setPlanningCopyModal] = useState<{ listId: string; webinarId: string; tab: "title" | "description" } | null>(null);
  const [modalBucketData, setModalBucketData] = useState<{
    bucket: ApiBucket;
    titles: ApiCopy[];
    descriptions: ApiCopy[];
  } | null>(null);

  // New Webinar modal state
  const [showNewWebinarModal, setShowNewWebinarModal] = useState(false);
  const getNextWebinarDefaults = useCallback(() => {
    const maxWebinar = webinars.reduce((max, w) => w.number > max.number ? w : max, webinars[0]);
    const nextNumber = maxWebinar ? maxWebinar.number + 1 : 1;
    // Parse the date string and add 7 days
    let nextDate = "";
    if (maxWebinar) {
      const d = new Date(maxWebinar.date);
      if (!isNaN(d.getTime())) {
        d.setDate(d.getDate() + 7);
        nextDate = d.toISOString().split("T")[0]; // YYYY-MM-DD for input[type=date]
      }
    }
    if (!nextDate) {
      const d = new Date();
      d.setDate(d.getDate() + 7);
      nextDate = d.toISOString().split("T")[0];
    }
    return { nextNumber, nextDate };
  }, [webinars]);
  const [newWebinarNumber, setNewWebinarNumber] = useState(0);
  const [newWebinarDate, setNewWebinarDate] = useState("");
  /** Free-text A/B variant label (e.g. "Account A"). Empty = no variant
   * label, which is allowed only when no other webinar exists for this
   * number. The form forces a non-empty label when the entered number
   * matches an existing webinar. */
  const [newWebinarVariantLabel, setNewWebinarVariantLabel] = useState("");
  /** Selected WebinarGeek credential id for this variant. Empty string
   * means "use default" (the legacy single-credential behavior). */
  const [newWebinarWgCredentialId, setNewWebinarWgCredentialId] = useState("");
  /** WG credentials list for the dropdown — fetched lazily when the new
   * webinar / edit modals open. */
  const [wgCredentials, setWgCredentials] = useState<ApiWgCredential[]>([]);

  // Edit Webinar modal state
  const [editWebinar, setEditWebinar] = useState<{ id: string; number: number; date: string; broadcastId: string; webinargeekCredentialId: string; status: string; registrationLink: string; unsubscribeLink: string; variantLabel: string } | null>(null);
  // Broadcast options for the Edit modal's selected WebinarGeek account.
  const [editBroadcasts, setEditBroadcasts] = useState<WgWebinar[]>([]);
  const [editBroadcastsLoading, setEditBroadcastsLoading] = useState(false);

  // Assignment form state — scoped to one webinar at a time
  const [assigningWebinarId, setAssigningWebinarId] = useState<string | null>(null);
  // Webinar id whose Assign request is in flight (button shows spinner + disabled)
  const [assignInFlight, setAssignInFlight] = useState<string | null>(null);
  // Synchronous mirror of assignInFlight. React state updates are async, so two
  // clicks fired within the same tick both see the stale `null` and pass the
  // state guard — letting two identical POSTs through. The ref updates in the
  // same call frame and blocks the second handler entry.
  const assignInFlightRef = useRef<string | null>(null);
  const [assignTab, setAssignTab] = useState<"buckets" | "custom_lists">("buckets");
  const [assignBucket, setAssignBucket] = useState("");
  const [assignCustomList, setAssignCustomList] = useState("");
  const [customLists, setCustomLists] = useState<ApiCustomList[]>([]);
  const [assignSender, setAssignSender] = useState("");
  const [assignVolume, setAssignVolume] = useState(0);
  // Assignment filter overrides (pre-filled from bucket, editable)
  const [assignCountries, setAssignCountries] = useState("");
  const [assignEmpRange, setAssignEmpRange] = useState("");
  const [assignAccounts, setAssignAccounts] = useState(0);
  const [assignSendPerAcct, setAssignSendPerAcct] = useState(0);
  const [assignDays, setAssignDays] = useState(5);

  // New Sender form state
  const [showNewSenderForm, setShowNewSenderForm] = useState(false);
  const [newSenderName, setNewSenderName] = useState("");
  const [newSenderAccounts, setNewSenderAccounts] = useState(5);
  const [newSenderSendPerAcct, setNewSenderSendPerAcct] = useState(50);
  const [newSenderDaysPerWeb, setNewSenderDaysPerWeb] = useState(5);
  const [creatingSender, setCreatingSender] = useState(false);

  const updateSender = async (id: string, field: keyof Sender, value: number) => {
    // Optimistic update
    setSenders((prev) => prev.map((s) => s.id === id ? { ...s, [field]: value } : s));
    // Map local field names to API field names
    const apiFieldMap: Record<string, string> = {
      accounts: "total_accounts",
      sendPerAccount: "send_per_account",
      daysPerWeek: "days_per_webinar",
    };
    const apiField = apiFieldMap[field] || field;
    try {
      await apiUpdateSender(id, { [apiField]: value });
    } catch (err) {
      console.error("Failed to update sender:", err);
    }
  };

  const handleAddSender = async () => {
    if (!newSenderName.trim()) return;
    setCreatingSender(true);
    try {
      const created = await apiCreateSender({
        name: newSenderName.trim(),
        total_accounts: newSenderAccounts,
        send_per_account: newSenderSendPerAcct,
        days_per_webinar: newSenderDaysPerWeb,
      });
      setSenders(prev => [...prev, apiSenderToLocal(created)]);
      setNewSenderName("");
      setNewSenderAccounts(5);
      setNewSenderSendPerAcct(50);
      setNewSenderDaysPerWeb(5);
      setShowNewSenderForm(false);
    } catch (err) {
      console.error("Failed to create sender:", err);
      alert(err instanceof Error ? err.message : "Failed to create sender");
    } finally {
      setCreatingSender(false);
    }
  };

  /* ── Stats ─────────────────────────────────────────────────────────── */

  const globalStats = useMemo(() => {
    const allLists = webinars.flatMap((w) => w.lists.filter((l) => !l.isNonjoiners && !l.isNoListData));
    return {
      totalLists: allLists.length,
      totalVolume: allLists.reduce((s, l) => s + l.listSize, 0),
      totalRemaining: allLists.reduce((s, l) => s + l.listRemain, 0),
      totalAccounts: Math.round(allLists.reduce((s, l) => s + l.accountsNeeded, 0)),
      availableBuckets: buckets
        .filter((b) => !isDisqualifiedBucket(b))
        .reduce((s, b) => s + (b.remaining_contacts || 0), 0),
    };
  }, [webinars, buckets]);

  /* ── Account tracking per sender per webinar ────────────────────────── */

  const getAccountsUsedForSender = useCallback((webinarId: string, senderId: string): number => {
    const w = webinars.find((w) => w.id === webinarId);
    if (!w) return 0;
    return Math.round(
      w.lists
        .filter((l) => !l.isNonjoiners && !l.isNoListData && l.senderId === senderId)
        .reduce((sum, l) => sum + l.accountsNeeded, 0)
    );
  }, [webinars]);

  const getAvailableAccounts = useCallback((webinarId: string, senderId: string): number => {
    const sender = senders.find((s) => s.id === senderId);
    if (!sender) return 0;
    const used = getAccountsUsedForSender(webinarId, senderId);
    return Math.max(0, sender.accounts - used);
  }, [senders, getAccountsUsedForSender]);

  /* ── Handlers ──────────────────────────────────────────────────────── */

  const toggleWebinar = (id: string) => {
    setWebinars((prev) => prev.map((w) => (w.id === id ? { ...w, expanded: !w.expanded } : w)));
  };

  const toggleAssignForm = (webinarId: string) => {
    if (assigningWebinarId === webinarId) {
      setAssigningWebinarId(null);
    } else {
      // Reset form and open for this webinar
      setAssigningWebinarId(webinarId);
      setAssignBucket("");
      setAssignCustomList("");
      setAssignSender("");
      setAssignVolume(0);
      setAssignCountries("");
      setAssignEmpRange("");
      setAssignAccounts(0);
      setAssignSendPerAcct(0);
      setAssignDays(5);
      // Load custom lists
      fetchCustomLists().then(({ lists }) => setCustomLists(lists)).catch(() => {});
    }
  };

  const handleUpdateWebinar = async (webinarId: string, field: "main_title" | "broadcast_id" | "status" | "number" | "date" | "registration_link" | "unsubscribe_link" | "variant_label" | "webinargeek_credential_id", value: string | number | null) => {
    // Optimistic update
    setWebinars((prev) => prev.map((w) => {
      if (w.id !== webinarId) return w;
      if (field === "main_title") return { ...w, mainTitle: value as string };
      if (field === "broadcast_id") return { ...w, broadcastId: (value as string) || "—" };
      if (field === "registration_link") return { ...w, registrationLink: value as string };
      if (field === "unsubscribe_link") return { ...w, unsubscribeLink: value as string };
      if (field === "status") return { ...w, status: (value as string).charAt(0).toUpperCase() + (value as string).slice(1) };
      if (field === "number") return { ...w, number: value as number };
      if (field === "variant_label") return { ...w, variantLabel: (value as string | null) || null };
      if (field === "webinargeek_credential_id") return { ...w, webinargeekCredentialId: (value as string | null) || null };
      if (field === "date") {
        const iso = value as string;
        const d = new Date(iso + "T00:00:00");
        return { ...w, isoDate: iso, date: d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" }) };
      }
      return w;
    }));
    try {
      await apiUpdateWebinar(webinarId, { [field]: value });
    } catch (err) {
      console.error("Failed to update webinar:", err);
    }
  };

  /** Load broadcast options for the Edit modal's selected WG account.
   * `refresh` first pulls the latest from WebinarGeek (used on modal open). */
  const loadEditBroadcasts = useCallback(async (credIdForFilter: string | undefined, refresh: boolean) => {
    setEditBroadcastsLoading(true);
    try {
      if (refresh) {
        try { await refreshWgWebinars(); } catch (e) { console.error("WG refresh failed:", e); }
      }
      const { broadcasts } = await fetchWgWebinars({ credential_id: credIdForFilter, limit: 500 });
      setEditBroadcasts(broadcasts);
    } catch (e) {
      console.error("Failed to load broadcasts:", e);
      setEditBroadcasts([]);
    } finally {
      setEditBroadcastsLoading(false);
    }
  }, []);

  const handleDeleteWebinar = async (webinarId: string) => {
    const w = webinars.find((w) => w.id === webinarId);
    if (!w) return;
    const listCount = w.lists.length;
    const msg = listCount > 0
      ? `Delete W${w.number} and its ${listCount} assigned list${listCount > 1 ? "s" : ""}? Assigned contacts will be released back to their buckets.`
      : `Delete W${w.number}?`;
    if (!confirm(msg)) return;

    try {
      await apiDeleteWebinar(webinarId);
      setWebinars((prev) => prev.filter((w) => w.id !== webinarId));
      if (assigningWebinarId === webinarId) setAssigningWebinarId(null);
      // Refetch buckets to get authoritative remaining counts
      try {
        const { buckets: freshBuckets } = await fetchBuckets();
        setBuckets(freshBuckets);
      } catch { /* non-critical */ }
    } catch (err) {
      console.error("Failed to delete webinar:", err);
      alert(err instanceof Error ? err.message : "Failed to delete webinar");
    }
  };

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const selectAllInWebinar = (webinarId: string) => {
    const w = webinars.find((w) => w.id === webinarId);
    if (!w) return;
    const listIds = w.lists.filter((l) => !l.isNonjoiners && !l.isNoListData).map((l) => l.id);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      const allSelected = listIds.every((id) => next.has(id));
      if (allSelected) listIds.forEach((id) => next.delete(id));
      else listIds.forEach((id) => next.add(id));
      return next;
    });
  };

  /* ── Bucket grouping: collapse + select helpers ────────────────────── */

  const [collapsedBuckets, setCollapsedBuckets] = useState<Set<string>>(new Set());

  const toggleBucketGroup = (webinarId: string, bucketId: string) => {
    const key = `${webinarId}::${bucketId}`;
    setCollapsedBuckets((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };

  const selectAllInBucketGroup = (webinarId: string, bucketId: string) => {
    const w = webinars.find((w) => w.id === webinarId);
    if (!w) return;
    const listIds = w.lists
      .filter((l) => !l.isNonjoiners && !l.isNoListData && l.bucketId === bucketId)
      .map((l) => l.id);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      const allSelected = listIds.every((id) => next.has(id));
      if (allSelected) listIds.forEach((id) => next.delete(id));
      else listIds.forEach((id) => next.add(id));
      return next;
    });
  };

  const selectAllInUniqueGroup = (listIds: string[]) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      const allSelected = listIds.every((id) => next.has(id));
      if (allSelected) listIds.forEach((id) => next.delete(id));
      else listIds.forEach((id) => next.add(id));
      return next;
    });
  };

  /* ── Release unused contacts modal ──────────────────────────────── */

  const [releaseModalWebinar, setReleaseModalWebinar] = useState<{ id: string; number: number } | null>(null);

  const refreshAfterRelease = useCallback(async (webinarId: string) => {
    // After a release we need fresh assignment counts (volume stays, but
    // bucket-side remaining_contacts and the contact mark counts change).
    try {
      const [{ buckets: freshBuckets }, { assignments }] = await Promise.all([
        fetchBuckets(),
        fetchWebinarLists(webinarId),
      ]);
      setBuckets(freshBuckets);
      setWebinars((prev) => prev.map((w) => (
        w.id === webinarId
          ? { ...w, lists: assignments.map(apiAssignmentToList) }
          : w
      )));
    } catch (err) {
      console.error("Failed to refresh after release:", err);
    }
  }, []);

  /* ── Webinar list export: background CSV build + polling ──────────── */

  const [exportJobs, setExportJobs] = useState<Map<string, ApiWebinarListExportJob>>(new Map());
  const [downloadingJobIds, setDownloadingJobIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { jobs } = await fetchActiveWebinarListExports();
        if (cancelled) return;
        setExportJobs(() => {
          const m = new Map<string, ApiWebinarListExportJob>();
          for (const j of jobs) m.set(j.webinar_id, j);
          return m;
        });
      } catch (e) {
        console.error(e);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const hasActive = Array.from(exportJobs.values()).some(
      (j) => j.status === "pending" || j.status === "processing",
    );
    if (!hasActive) return;

    let cancelled = false;
    const interval = setInterval(async () => {
      if (cancelled) return;
      const active = Array.from(exportJobs.values()).filter(
        (j) => j.status === "pending" || j.status === "processing",
      );
      try {
        const updated = await Promise.all(
          active.map((j) => fetchLatestWebinarListExport(j.webinar_id)),
        );
        if (cancelled) return;
        setExportJobs((prev) => {
          const next = new Map(prev);
          for (const j of updated) {
            if (j) next.set(j.webinar_id, j);
          }
          return next;
        });
      } catch (e) {
        console.error(e);
      }
    }, 2000);

    return () => { cancelled = true; clearInterval(interval); };
  }, [exportJobs]);

  const handleStartExport = async (webinarId: string) => {
    try {
      const job = await startWebinarListExport(webinarId);
      setExportJobs((prev) => {
        const next = new Map(prev);
        next.set(webinarId, job);
        return next;
      });
    } catch (e) {
      console.error(e);
      alert("Failed to start export");
    }
  };

  const handleDownloadExport = async (webinarId: string, webinarNumber: number, jobId: string) => {
    setDownloadingJobIds((prev) => { const n = new Set(prev); n.add(jobId); return n; });
    try {
      await downloadWebinarListExport(webinarId, jobId, `webinar-${webinarNumber}-lists.csv`);
    } catch (e) {
      console.error(e);
      alert("Failed to download export");
    } finally {
      setDownloadingJobIds((prev) => { const n = new Set(prev); n.delete(jobId); return n; });
    }
  };

  const handleAssign = useCallback(async (webinarIdOverride?: string) => {
    const targetId = webinarIdOverride || assigningWebinarId;
    if (!assignSender || assignVolume <= 0 || !targetId) return;
    if (assignInFlightRef.current) return;

    const isCustomListAssign = assignTab === "custom_lists";

    if (!isCustomListAssign && !assignBucket) return;
    if (isCustomListAssign && !assignCustomList) return;

    const sender = senders.find((s) => s.id === assignSender);
    if (!sender) return;

    const sendPerAcct = assignSendPerAcct > 0 ? assignSendPerAcct : sender.sendPerAccount;
    const calculatedAccts = sendPerAcct > 0 && assignDays > 0
      ? Math.ceil(assignVolume / (sendPerAcct * assignDays))
      : 0;
    const accts = assignAccounts > 0 ? assignAccounts : calculatedAccts;

    let requestData: Parameters<typeof assignBucketToWebinar>[1];

    if (isCustomListAssign) {
      requestData = {
        upload_id: assignCustomList,
        sender_id: assignSender,
        volume: assignVolume,
        accounts_used: accts,
        send_per_account: sendPerAcct,
        days: assignDays,
      };
    } else {
      const bucket = buckets.find((b) => b.id === assignBucket);
      if (!bucket) return;
      const volume = Math.min(assignVolume, bucket.remaining_contacts);
      const countries = assignCountries || (bucket.countries || []).join(", ");
      const empRange = assignEmpRange || bucket.emp_range || "";
      requestData = {
        bucket_id: assignBucket,
        sender_id: assignSender,
        volume,
        accounts_used: accts,
        send_per_account: sendPerAcct,
        days: assignDays,
        countries_override: countries,
        emp_range_override: empRange,
      };
    }

    assignInFlightRef.current = targetId;
    setAssignInFlight(targetId);
    try {
      const assignment = await assignBucketToWebinar(targetId, requestData);

      // Add to webinar in local state
      const newList = apiAssignmentToList(assignment);
      setWebinars((prev) => prev.map((w) =>
        w.id === targetId ? { ...w, lists: [...w.lists, newList] } : w
      ));

      // Update bucket remaining with authoritative DB value (only for bucket assignments)
      if (!isCustomListAssign && assignment.bucket_remaining !== undefined) {
        setBuckets((prev) => prev.map((b) =>
          b.id === assignBucket ? { ...b, remaining_contacts: assignment.bucket_remaining! } : b
        ));
      }

      // Refresh custom lists if we assigned from one
      if (isCustomListAssign) {
        fetchCustomLists().then(({ lists }) => setCustomLists(lists)).catch(() => {});
      }

      // Reset form
      setAssignBucket("");
      setAssignCustomList("");
      setAssignSender("");
      setAssignVolume(0);
      setAssignCountries("");
      setAssignEmpRange("");
      setAssignAccounts(0);
      setAssignSendPerAcct(0);
      setAssignDays(5);
    } catch (err) {
      console.error("Failed to assign:", err);
      alert(err instanceof Error ? err.message : "Failed to assign");
    } finally {
      assignInFlightRef.current = null;
      setAssignInFlight(null);
    }
  }, [assignBucket, assignCustomList, assignTab, assignSender, assignVolume, assigningWebinarId, assignCountries, assignEmpRange, assignAccounts, assignSendPerAcct, assignDays, buckets, senders]);

  const handleToggleSetup = useCallback(async (listId: string, webinarId: string, currentValue: boolean) => {
    const newValue = !currentValue;
    // Optimistic update
    setWebinars((prev) => prev.map((w) =>
      w.id === webinarId ? { ...w, lists: w.lists.map((l) => l.id === listId ? { ...l, isSetup: newValue } : l) } : w
    ));
    try {
      await apiUpdateAssignment(listId, { is_setup: newValue });
    } catch (err) {
      console.error("Failed to toggle setup:", err);
      // Revert on failure
      setWebinars((prev) => prev.map((w) =>
        w.id === webinarId ? { ...w, lists: w.lists.map((l) => l.id === listId ? { ...l, isSetup: currentValue } : l) } : w
      ));
    }
  }, []);

  const handleDeleteAssignment = useCallback(async (listId: string, webinarId: string) => {
    const w = webinars.find((w) => w.id === webinarId);
    const list = w?.lists.find((l) => l.id === listId);
    if (!list) return;
    if (!confirm(`Remove "${list.bucket}" (${list.listSize.toLocaleString()} contacts) from W${w?.number}? Contacts will be released back to the bucket.`)) return;

    try {
      const { released, bucket_id, bucket_remaining } = await apiDeleteAssignment(listId);

      // Remove list from webinar in local state
      setWebinars((prev) => prev.map((w) =>
        w.id === webinarId ? { ...w, lists: w.lists.filter((l) => l.id !== listId) } : w
      ));

      // Update bucket remaining with authoritative DB value
      if (bucket_id && bucket_remaining !== null) {
        setBuckets((prev) => prev.map((b) =>
          b.id === bucket_id ? { ...b, remaining_contacts: bucket_remaining } : b
        ));
      }

      // Deselect if selected
      setSelectedIds((prev) => {
        const next = new Set(prev);
        next.delete(listId);
        return next;
      });
    } catch (err) {
      console.error("Failed to delete assignment:", err);
      alert(err instanceof Error ? err.message : "Failed to delete assignment");
    }
  }, [webinars]);

  const handleBulkDelete = useCallback(async () => {
    const listsToDelete = webinars.flatMap((w) => w.lists.map((l) => ({ ...l, webinarId: w.id, webinarNumber: w.number }))).filter((l) => selectedIds.has(l.id) && !l.isNonjoiners && !l.isNoListData);
    if (listsToDelete.length === 0) return;
    const totalContacts = listsToDelete.reduce((sum, l) => sum + l.listSize, 0);
    if (!confirm(`Remove ${listsToDelete.length} assigned list${listsToDelete.length > 1 ? "s" : ""} (${totalContacts.toLocaleString()} contacts total)? Contacts will be released back to their buckets.`)) return;

    let deletedCount = 0;
    for (const list of listsToDelete) {
      try {
        const { bucket_id, bucket_remaining } = await apiDeleteAssignment(list.id);
        setWebinars((prev) => prev.map((w) =>
          w.id === list.webinarId ? { ...w, lists: w.lists.filter((l) => l.id !== list.id) } : w
        ));
        if (bucket_id && bucket_remaining !== null) {
          setBuckets((prev) => prev.map((b) =>
            b.id === bucket_id ? { ...b, remaining_contacts: bucket_remaining } : b
          ));
        }
        deletedCount++;
      } catch (err) {
        console.error(`Failed to delete assignment ${list.id}:`, err);
        alert(`Failed to delete list "${list.bucket}" from W${list.webinarNumber}. ${deletedCount} of ${listsToDelete.length} deleted so far.`);
        break;
      }
    }
    setSelectedIds(new Set());
  }, [webinars, selectedIds]);

  const openCopyModal = () => {
    const lists = webinars.flatMap((w) => w.lists).filter((l) => selectedIds.has(l.id) && !l.isNonjoiners && !l.isNoListData);
    setCopyModalLists(lists);
    setShowCopyModal(true);
  };

  const handleGenerateCopies = async () => {
    setGeneratingCopies(true);
    try {
      // Find unique bucket IDs from selected lists
      const uniqueBucketIds = [...new Set(
        copyModalLists.filter((l) => l.bucketId).map((l) => l.bucketId!)
      )];

      // Generate copies for each bucket (both title + description)
      const bucketCopiesMap: Record<string, { titles: ApiCopy[]; descriptions: ApiCopy[] }> = {};
      await Promise.all(uniqueBucketIds.map(async (bucketId) => {
        const result = await generateCopies(bucketId, { copy_type: "both" });
        bucketCopiesMap[bucketId] = { titles: result.titles, descriptions: result.descriptions };
      }));

      // Update lists with generated copies
      const updateList = (l: PlannedList): PlannedList => {
        if (!l.bucketId || !bucketCopiesMap[l.bucketId]) return l;
        const copies = bucketCopiesMap[l.bucketId];
        const primaryTitle = copies.titles.find((c) => c.is_primary);
        return {
          ...l,
          copiesGenerated: true,
          title: primaryTitle?.text || l.title,
          titleVariants: copies.titles.map((c) => ({ id: c.id, text: c.text, selected: c.is_primary, variantIndex: c.variant_index })),
          descVariants: copies.descriptions.map((c) => ({ id: c.id, text: c.text, selected: c.is_primary, variantIndex: c.variant_index })),
        };
      };

      setWebinars((prev) => prev.map((w) => ({
        ...w,
        lists: w.lists.map((l) => (copyModalLists.some((ml) => ml.id === l.id) ? updateList(l) : l)),
      })));
      setCopyModalLists((prev) => prev.map(updateList));

      // Persist primary copy IDs to each affected assignment
      await Promise.all(copyModalLists.map(async (l) => {
        if (!l.bucketId || !bucketCopiesMap[l.bucketId]) return;
        const copies = bucketCopiesMap[l.bucketId];
        const primaryTitle = copies.titles.find((c) => c.is_primary);
        const primaryDesc = copies.descriptions.find((c) => c.is_primary);
        const payload: Record<string, string> = {};
        if (primaryTitle) payload.title_copy_id = primaryTitle.id;
        if (primaryDesc) payload.desc_copy_id = primaryDesc.id;
        if (Object.keys(payload).length > 0) {
          await apiUpdateAssignment(l.id, payload).catch((err) =>
            console.error(`Failed to save copies for assignment ${l.id}:`, err)
          );
        }
      }));
    } catch (err) {
      console.error("Failed to generate copies:", err);
      alert(err instanceof Error ? err.message : "Failed to generate copies");
    } finally {
      setGeneratingCopies(false);
    }
  };

  const selectVariant = (listId: string, type: "title" | "desc", variantId: string) => {
    // Optimistic UI update
    setWebinars((prev) => prev.map((w) => ({
      ...w,
      lists: w.lists.map((l) => {
        if (l.id !== listId) return l;
        if (type === "title") {
          const selected = l.titleVariants?.find((v) => v.id === variantId);
          return {
            ...l,
            title: selected?.text || l.title,
            titleVariants: l.titleVariants?.map((v) => ({ ...v, selected: v.id === variantId })),
          };
        } else {
          return {
            ...l,
            descVariants: l.descVariants?.map((v) => ({ ...v, selected: v.id === variantId })),
          };
        }
      }),
    })));
    setCopyModalLists((prev) => prev.map((l) => {
      if (l.id !== listId) return l;
      if (type === "title") {
        const selected = l.titleVariants?.find((v) => v.id === variantId);
        return {
          ...l,
          title: selected?.text || l.title,
          titleVariants: l.titleVariants?.map((v) => ({ ...v, selected: v.id === variantId })),
        };
      } else {
        return {
          ...l,
          descVariants: l.descVariants?.map((v) => ({ ...v, selected: v.id === variantId })),
        };
      }
    }));

    // Persist to DB
    const payload = type === "title" ? { title_copy_id: variantId } : { desc_copy_id: variantId };
    apiUpdateAssignment(listId, payload).catch((err) =>
      console.error("Failed to persist variant selection:", err)
    );
  };

  const deleteVariant = async (listId: string, type: "title" | "desc", variantId: string) => {
    try {
      await deleteCopy(variantId);
    } catch (err) {
      console.error("Failed to delete variant:", err);
      return;
    }
    const removeFromList = (l: typeof webinars[0]["lists"][0]) => {
      if (l.id !== listId) return l;
      const key = type === "title" ? "titleVariants" : "descVariants";
      const remaining = l[key]?.filter((v) => v.id !== variantId);
      const wasSelected = l[key]?.find((v) => v.id === variantId)?.selected;
      // If the deleted variant was selected, auto-select the first remaining
      if (wasSelected && remaining && remaining.length > 0) {
        remaining[0].selected = true;
        if (type === "title") {
          const payload = { title_copy_id: remaining[0].id };
          apiUpdateAssignment(listId, payload).catch(() => {});
          return { ...l, title: remaining[0].text, [key]: remaining };
        } else {
          const payload = { desc_copy_id: remaining[0].id };
          apiUpdateAssignment(listId, payload).catch(() => {});
          return { ...l, [key]: remaining };
        }
      }
      return { ...l, [key]: remaining };
    };
    setWebinars((prev) => prev.map((w) => ({ ...w, lists: w.lists.map(removeFromList) })));
    setCopyModalLists((prev) => prev.map(removeFromList as any));
  };

  /* ── Variations Modal (shared with Copy Generator) ─────────────────── */

  const openVariationsModal = useCallback(async (listId: string, webinarId: string, tab: "title" | "description") => {
    const list = webinars.find(w => w.id === webinarId)?.lists.find(l => l.id === listId);
    if (!list) return;
    setPlanningCopyModal({ listId, webinarId, tab });
    setModalBucketData(null);
    try {
      if (list.sourceType === "custom_list" && list.sourceUploadId) {
        // Custom list: load copies from upload endpoint
        const copies = await fetchCustomListCopies(list.sourceUploadId);
        // Create a synthetic "bucket" object for the modal header
        setModalBucketData({
          bucket: {
            id: list.sourceUploadId,
            name: list.listName || list.description || "Custom List",
            industry: null,
            total_contacts: list.listSize,
            remaining_contacts: list.listRemain,
            countries: [],
            emp_range: null,
            source_file: null,
            copies_count: { titles: copies.titles.length, descriptions: copies.descriptions.length },
            has_primary_title: copies.titles.some(c => c.is_primary),
            has_primary_description: copies.descriptions.some(c => c.is_primary),
            title_primary_picked: false,
            desc_primary_picked: false,
            created_at: null,
          },
          titles: copies.titles,
          descriptions: copies.descriptions,
        });
      } else if (list.bucketId) {
        // Bucket: existing flow
        const bucketData = buckets.find(b => b.id === list.bucketId);
        if (!bucketData) return;
        const copies = await fetchBucketCopies(list.bucketId);
        setModalBucketData({
          bucket: bucketData,
          titles: copies.titles,
          descriptions: copies.descriptions,
        });
      }
    } catch (err) {
      console.error("Failed to load copies:", err);
    }
  }, [webinars, buckets]);

  const closeVariationsModal = () => {
    setPlanningCopyModal(null);
    setModalBucketData(null);
  };

  // Sync bucket copy updates back to the list's variant arrays (so table row stays current)
  const syncBucketCopiesToList = useCallback((
    bucketId: string,
    titles: ApiCopy[],
    descriptions: ApiCopy[],
  ) => {
    setWebinars((prev) => prev.map((w) => ({
      ...w,
      lists: w.lists.map((l) => {
        if (l.bucketId !== bucketId) return l;
        const selectedTitleId = l.titleVariants?.find(v => v.selected)?.id;
        const selectedDescId = l.descVariants?.find(v => v.selected)?.id;
        const newTitleVariants = titles.map(c => ({
          id: c.id, text: c.text, variantIndex: c.variant_index,
          selected: selectedTitleId ? c.id === selectedTitleId : c.is_primary,
        }));
        const newDescVariants = descriptions.map(c => ({
          id: c.id, text: c.text, variantIndex: c.variant_index,
          selected: selectedDescId ? c.id === selectedDescId : c.is_primary,
        }));
        const selectedTitle = newTitleVariants.find(v => v.selected);
        return {
          ...l,
          titleVariants: newTitleVariants,
          descVariants: newDescVariants,
          title: selectedTitle?.text ?? l.title,
          copiesGenerated: newTitleVariants.length > 0 || newDescVariants.length > 0,
        };
      }),
    })));
  }, []);

  const handleModalAddVariant = useCallback(async (bucketId: string, type: "title" | "description", text: string) => {
    try {
      // Detect if this is a custom-list assignment (bucketId is actually an upload_id)
      const targetList = planningCopyModal ? webinars.flatMap(w => w.lists).find(l => l.id === planningCopyModal.listId) : null;
      const isCustom = targetList?.sourceType === "custom_list";
      const newCopy = isCustom
        ? await apiCreateCustomListCopy(bucketId, { copy_type: type, text })
        : await apiCreateCopy(bucketId, { copy_type: type, text });
      setModalBucketData(prev => {
        if (!prev || prev.bucket.id !== bucketId) return prev;
        const key = type === "title" ? "titles" : "descriptions";
        const next = { ...prev, [key]: [...prev[key], newCopy] };
        syncBucketCopiesToList(bucketId, next.titles, next.descriptions);
        return next;
      });
    } catch (err) {
      console.error("Failed to add variant:", err);
    }
  }, [syncBucketCopiesToList]);

  const handleModalRegenerate = useCallback(async (bucketId: string, type: "title" | "description", copyId: string, feedback: string) => {
    try {
      const newCopy = await apiRegenerateCopy(copyId, feedback);
      setModalBucketData(prev => {
        if (!prev || prev.bucket.id !== bucketId) return prev;
        const key = type === "title" ? "titles" : "descriptions";
        const next = { ...prev, [key]: [...prev[key], newCopy] };
        syncBucketCopiesToList(bucketId, next.titles, next.descriptions);
        return next;
      });
    } catch (err) {
      console.error("Failed to regenerate:", err);
    }
  }, [syncBucketCopiesToList]);

  const handleModalUpdateVariant = useCallback(async (bucketId: string, type: "title" | "description", variantId: string, newText: string) => {
    try {
      const updated = await apiUpdateCopy(variantId, { text: newText });
      setModalBucketData(prev => {
        if (!prev || prev.bucket.id !== bucketId) return prev;
        const key = type === "title" ? "titles" : "descriptions";
        const updatedList = prev[key].map(c => c.id === variantId ? updated : c);
        const next = { ...prev, [key]: updatedList };
        syncBucketCopiesToList(bucketId, next.titles, next.descriptions);
        return next;
      });
    } catch (err) {
      console.error("Failed to update variant:", err);
    }
  }, [syncBucketCopiesToList]);

  const handleModalSetPrimary = useCallback(async (bucketId: string, type: "title" | "description", variantId: string) => {
    try {
      await apiUpdateCopy(variantId, { is_primary: true });
      setModalBucketData(prev => {
        if (!prev || prev.bucket.id !== bucketId) return prev;
        const key = type === "title" ? "titles" : "descriptions";
        const updatedList = prev[key].map(c => ({ ...c, is_primary: c.id === variantId }));
        return { ...prev, [key]: updatedList };
      });
    } catch (err) {
      console.error("Failed to set primary:", err);
    }
  }, []);

  const handleModalDeleteVariant = useCallback(async (bucketId: string, type: "title" | "description", variantId: string) => {
    try {
      await deleteCopy(variantId);
      setModalBucketData(prev => {
        if (!prev || prev.bucket.id !== bucketId) return prev;
        const key = type === "title" ? "titles" : "descriptions";
        const updatedList = prev[key].filter(c => c.id !== variantId);
        const next = { ...prev, [key]: updatedList };
        syncBucketCopiesToList(bucketId, next.titles, next.descriptions);
        return next;
      });
    } catch (err) {
      console.error("Failed to delete variant:", err);
    }
  }, [syncBucketCopiesToList]);

  const handleModalPickForList = useCallback((bucketId: string, type: "title" | "description", variantId: string) => {
    if (!planningCopyModal) return;
    const listId = planningCopyModal.listId;
    // Update assignment's title_copy_id or desc_copy_id
    const payload = type === "title" ? { title_copy_id: variantId } : { desc_copy_id: variantId };
    apiUpdateAssignment(listId, payload).catch((err) => console.error("Failed to pick variant:", err));
    // Update local list state: mark this variant as selected. We also fold
    // in the full modalBucketData.titles/descriptions so that when the
    // picked variant was added after the initial bucket-copies load
    // (e.g. via Copy Generator or another modal), the list's variants array
    // stays in sync and the pick doesn't silently no-op.
    const bucketTitles = modalBucketData?.titles ?? [];
    const bucketDescriptions = modalBucketData?.descriptions ?? [];
    setWebinars((prev) => prev.map((w) => ({
      ...w,
      lists: w.lists.map((l) => {
        if (l.id !== listId) return l;
        if (type === "title") {
          // Prefer the fresh bucket copies when they cover the picked variant;
          // otherwise fall back to the existing titleVariants.
          const source = bucketTitles.length > 0 ? bucketTitles : null;
          const newVariants = source
            ? source.map(c => ({
                id: c.id, text: c.text, variantIndex: c.variant_index,
                selected: c.id === variantId,
              }))
            : l.titleVariants?.map(v => ({ ...v, selected: v.id === variantId }));
          const selected = newVariants?.find(v => v.selected);
          return { ...l, titleVariants: newVariants, title: selected?.text ?? l.title };
        }
        const source = bucketDescriptions.length > 0 ? bucketDescriptions : null;
        const newVariants = source
          ? source.map(c => ({
              id: c.id, text: c.text, variantIndex: c.variant_index,
              selected: c.id === variantId,
            }))
          : l.descVariants?.map(v => ({ ...v, selected: v.id === variantId }));
        return { ...l, descVariants: newVariants };
      }),
    })));
  }, [planningCopyModal, modalBucketData]);

  const closeCopyModal = () => {
    setShowCopyModal(false);
    setSelectedIds(new Set());
  };

  const openNewWebinarModal = () => {
    const { nextNumber, nextDate } = getNextWebinarDefaults();
    setNewWebinarNumber(nextNumber);
    setNewWebinarDate(nextDate);
    setNewWebinarVariantLabel("");
    setNewWebinarWgCredentialId("");
    setShowNewWebinarModal(true);
    // Lazy-fetch WG credentials so the dropdown is populated by the time
    // the user reaches the WebinarGeek field.
    fetchWgCredentials()
      .then((res) => setWgCredentials(res.credentials))
      .catch((err) => console.error("Failed to load WG credentials:", err));
  };

  /** Other webinars for the same number — used by the new-webinar modal
   * to detect collisions and force a variant label. */
  const siblingsForNewNumber = useMemo(
    () => webinars.filter((w) => w.number === newWebinarNumber),
    [webinars, newWebinarNumber],
  );

  const handleCreateWebinar = async () => {
    if (!newWebinarNumber || !newWebinarDate) return;
    const trimmedLabel = newWebinarVariantLabel.trim();
    // Hard-prevent the obvious conflicts on the client; backend re-checks.
    if (siblingsForNewNumber.length > 0 && !trimmedLabel) {
      alert(
        `Webinar ${newWebinarNumber} already exists. ` +
        "Provide a Variant label to add it as an A/B variant.",
      );
      return;
    }
    try {
      const created = await apiCreateWebinar({
        number: newWebinarNumber,
        date: newWebinarDate,
        variant_label: trimmedLabel || null,
        webinargeek_credential_id: newWebinarWgCredentialId || null,
      });
      const d = new Date(newWebinarDate + "T00:00:00");
      const dateStr = d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
      const newWebinar: Webinar = {
        id: created.id,
        number: created.number,
        date: dateStr,
        isoDate: newWebinarDate,
        status: "Planning",
        broadcastId: "—",
        mainTitle: "",
        registrationLink: created.registration_link || "",
        unsubscribeLink: created.unsubscribe_link || "",
        lists: [],
        expanded: true,
        variantLabel: created.variant_label,
        webinargeekCredentialId: created.webinargeek_credential_id,
      };
      setWebinars((prev) => [newWebinar, ...prev]);
      // Auto-open assignment form for the new webinar
      toggleAssignForm(created.id);
      setShowNewWebinarModal(false);
    } catch (err) {
      console.error("Failed to create webinar:", err);
      alert(err instanceof Error ? err.message : "Failed to create webinar");
    }
  };

  /* ── Filtered webinars ─────────────────────────────────────────────── */

  const filteredWebinars = useMemo(() => {
    let result = [...webinars].sort((a, b) => b.isoDate.localeCompare(a.isoDate));
    // Sender filter: keep the webinar, but show only lists belonging to the selected sender
    if (senderFilterId) {
      result = result
        .map((w) => ({
          ...w,
          lists: w.lists.filter((l) => l.senderId === senderFilterId || l.isNonjoiners || l.isNoListData),
        }))
        .filter((w) => w.lists.some((l) => l.senderId === senderFilterId));
    }
    if (!searchQuery) return result;
    const q = searchQuery.toLowerCase();
    return result.filter((w) =>
      w.number.toString().includes(q) ||
      w.date.toLowerCase().includes(q) ||
      w.lists.some((l) => l.description.toLowerCase().includes(q) || l.bucket.toLowerCase().includes(q) || l.sender.toLowerCase().includes(q))
    );
  }, [webinars, searchQuery, senderFilterId]);

  const selectedCount = selectedIds.size;

  /* ── Render ────────────────────────────────────────────────────────── */

  return (
    <main className="min-h-screen pb-20">
      {/* ── Sticky header ──────────────────────────────────────────── */}
      <div className="sticky top-12 z-40 bg-white dark:bg-zinc-950/90 backdrop-blur-md border-b border-zinc-200 dark:border-zinc-800/40 px-6 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-6">
            <h1 className="text-lg font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Campaign Planning</h1>
            <div className="flex gap-2">
              {[
                { label: "Lists", value: globalStats.totalLists, color: "text-zinc-800 dark:text-zinc-200" },
                { label: "Volume", value: globalStats.totalVolume.toLocaleString(), color: "text-violet-400" },
                { label: "Available", value: globalStats.availableBuckets.toLocaleString(), color: "text-amber-400" },
                { label: "Accounts", value: globalStats.totalAccounts, color: "text-emerald-400" },
              ].map((s) => (
                <div key={s.label} className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-zinc-50 dark:bg-zinc-900/60 border border-zinc-200 dark:border-zinc-800/40">
                  <span className={`text-sm font-bold font-mono ${s.color}`}>{s.value}</span>
                  <span className="text-[10px] text-zinc-500 uppercase tracking-wider">{s.label}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-3">
            <select
              value={senderFilterId}
              onChange={(e) => setSenderFilterId(e.target.value)}
              className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 cursor-pointer"
              title="Filter lists by sender"
            >
              <option value="">All senders</option>
              {senders.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
            <input type="text" value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} placeholder="Search lists, buckets, senders..." className="w-56 bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500" />
            <button onClick={openNewWebinarModal} className="px-4 py-1.5 bg-violet-600 hover:bg-violet-500 text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>
              New Webinar
            </button>
          </div>
        </div>
      </div>

      {/* Sender legend — editable */}
      <div className="px-6 py-2 border-b border-zinc-200 dark:border-zinc-800/20 bg-white dark:bg-zinc-950/50">
        <div className="flex items-center gap-2 mb-0">
          <span className="text-xs text-zinc-500 uppercase tracking-wider font-semibold">Senders:</span>
          <button onClick={() => setEditingSenders(!editingSenders)} className="text-xs text-zinc-600 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200 transition-colors ml-1">
            {editingSenders ? "Done" : "Edit"}
          </button>
        </div>
        {!editingSenders ? (
          <div className="flex items-center gap-5 mt-1.5 overflow-x-auto">
            {senders.map((s) => (
              <div key={s.id} className="flex items-center gap-2 shrink-0">
                <SenderBadge name={s.name} color={s.color} />
                <span className="text-xs text-zinc-500 dark:text-zinc-400 font-mono whitespace-nowrap">{s.accounts} accts · {s.sendPerAccount}/acct · {s.daysPerWeek}d/webinar</span>
                <span className="text-xs text-zinc-700 dark:text-zinc-300 font-mono font-semibold whitespace-nowrap">= {(s.accounts * s.sendPerAccount).toLocaleString()}/d</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="flex items-center gap-5 mt-2 overflow-x-auto">
            {senders.map((s) => (
              <div key={s.id} className="flex items-center gap-2.5 bg-zinc-50 dark:bg-zinc-900/60 border border-zinc-200 dark:border-zinc-800/40 rounded-lg px-3 py-2 shrink-0">
                <input
                  type="text"
                  defaultValue={s.name}
                  onBlur={(e) => {
                    const newName = e.target.value.trim();
                    if (newName && newName !== s.name) {
                      setSenders(prev => prev.map(x => x.id === s.id ? { ...x, name: newName } : x));
                      apiUpdateSender(s.id, { name: newName }).catch(err => console.error("Failed to rename sender:", err));
                    }
                  }}
                  onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                  className="w-24 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 font-semibold focus:outline-none focus:ring-1 focus:ring-violet-500"
                />
                <div className="flex items-center gap-2">
                  <div className="flex flex-col items-center">
                    <span className="text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">Accts</span>
                    <input type="number" key={`accts-${s.id}-${s.accounts}`} defaultValue={s.accounts}
                      onBlur={(e) => { const v = parseInt(e.target.value) || 0; if (v !== s.accounts) updateSender(s.id, "accounts", v); }}
                      onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                      className="w-14 bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500" />
                  </div>
                  <span className="text-zinc-500 text-xs">×</span>
                  <div className="flex flex-col items-center">
                    <span className="text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">Send/Acct</span>
                    <input type="number" key={`spa-${s.id}-${s.sendPerAccount}`} defaultValue={s.sendPerAccount}
                      onBlur={(e) => { const v = parseInt(e.target.value) || 0; if (v !== s.sendPerAccount) updateSender(s.id, "sendPerAccount", v); }}
                      onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                      className="w-14 bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500" />
                  </div>
                  <span className="text-zinc-500 text-xs">×</span>
                  <div className="flex flex-col items-center">
                    <span className="text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">Days/Web</span>
                    <input type="number" key={`dpw-${s.id}-${s.daysPerWeek}`} defaultValue={s.daysPerWeek}
                      onBlur={(e) => { const v = parseInt(e.target.value) || 0; if (v !== s.daysPerWeek) updateSender(s.id, "daysPerWeek", v); }}
                      onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                      className="w-14 bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500" />
                  </div>
                  <span className="text-zinc-500 text-xs">=</span>
                  <span className="text-sm text-violet-600 dark:text-violet-400 font-mono font-bold">{(s.accounts * s.sendPerAccount).toLocaleString()}/d</span>
                </div>
              </div>
            ))}

            {/* Add Sender button / form */}
            {!showNewSenderForm ? (
              <button
                onClick={() => setShowNewSenderForm(true)}
                className="flex items-center gap-1.5 px-3 py-2 border-2 border-dashed border-zinc-300 dark:border-zinc-700 rounded-lg text-xs text-zinc-500 hover:text-violet-400 hover:border-violet-500/40 transition-colors"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>
                Add Sender
              </button>
            ) : (
              <div className="flex items-center gap-2.5 bg-violet-50 dark:bg-violet-500/5 border border-violet-200 dark:border-violet-500/20 rounded-lg px-3 py-2">
                <div className="flex flex-col">
                  <span className="text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">Name</span>
                  <input
                    type="text"
                    value={newSenderName}
                    onChange={(e) => setNewSenderName(e.target.value)}
                    placeholder="Name..."
                    autoFocus
                    className="w-24 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
                  />
                </div>
                <div className="flex items-center gap-2">
                  <div className="flex flex-col items-center">
                    <span className="text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">Accts</span>
                    <input type="number" value={newSenderAccounts} onChange={(e) => setNewSenderAccounts(parseInt(e.target.value) || 0)}
                      className="w-14 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500" />
                  </div>
                  <span className="text-zinc-500 text-xs">×</span>
                  <div className="flex flex-col items-center">
                    <span className="text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">Send/Acct</span>
                    <input type="number" value={newSenderSendPerAcct} onChange={(e) => setNewSenderSendPerAcct(parseInt(e.target.value) || 0)}
                      className="w-14 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500" />
                  </div>
                  <span className="text-zinc-500 text-xs">×</span>
                  <div className="flex flex-col items-center">
                    <span className="text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide">Days/Web</span>
                    <input type="number" value={newSenderDaysPerWeb} onChange={(e) => setNewSenderDaysPerWeb(parseInt(e.target.value) || 0)}
                      className="w-14 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500" />
                  </div>
                </div>
                <button
                  onClick={handleAddSender}
                  disabled={!newSenderName.trim() || creatingSender}
                  className="px-3 py-1.5 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white text-xs font-semibold rounded-md transition-colors"
                >
                  {creatingSender ? "Adding..." : "Add"}
                </button>
                <button
                  onClick={() => setShowNewSenderForm(false)}
                  className="text-xs text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200 transition-colors"
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── Loading state ─────────────────────────────────────────── */}
      {loadingData && (
        <div className="flex flex-col items-center justify-center py-24 gap-3">
          <div className="w-6 h-6 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
          <span className="text-sm text-zinc-500">Loading campaigns...</span>
        </div>
      )}

      {/* ── Webinar table ──────────────────────────────────────────── */}
      {!loadingData && <div className="overflow-x-auto">
        <table className="w-full text-xs min-w-[1600px]">
          <thead>
            <tr className="bg-zinc-50 dark:bg-zinc-900/90 border-b border-zinc-200 dark:border-zinc-800/40">
              <th className="w-8 px-2 py-2"></th>
              <th className="w-8 px-1 py-2"></th>
              <th className="text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] min-w-[130px]">Webinar #</th>
              <th className="text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Status</th>
              <th className="text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] min-w-[320px]">Description of List</th>
              <th className="text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] w-[130px] max-w-[130px]">Bucket</th>
              <th className="text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Sender</th>
              <th className="text-right px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">List Size</th>
              <th className="text-right px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Remaining</th>
              <th className="text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] min-w-[250px]">Title</th>
              <th className="text-left px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px] min-w-[250px]">Description</th>
              <th className="text-right px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Accts</th>
              <th className="text-center px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Copies</th>
              <th className="text-center px-2 py-2 text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">Setup</th>
              <th className="w-8 px-2 py-2"></th>
            </tr>
          </thead>
          {filteredWebinars.map((w) => {
              const wLists = w.lists.filter((l) => !l.isNonjoiners && !l.isNoListData);
              const wTotal = wLists.reduce((s, l) => s + l.listSize, 0);
              const wRemain = w.lists.reduce((s, l) => s + l.listRemain, 0);
              const wAccounts = Math.round(wLists.reduce((s, l) => s + l.accountsNeeded, 0));
              const allInWebinarSelected = wLists.length > 0 && wLists.every((l) => selectedIds.has(l.id));

              return (
                <tbody key={w.id}>
                  {/* ── Webinar parent row ─────────────────────────── */}
                  <tr className="bg-zinc-100 dark:bg-zinc-800/40 hover:bg-zinc-200 dark:hover:bg-zinc-800/60 cursor-pointer border-t-2 border-zinc-300 dark:border-zinc-700/40 transition-colors">
                    <td className="px-2 py-1.5 text-center" onClick={() => toggleWebinar(w.id)}>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                        className={`text-zinc-600 dark:text-zinc-400 transition-transform duration-200 ${w.expanded ? "rotate-90" : ""}`}>
                        <path d="M9 18l6-6-6-6"/>
                      </svg>
                    </td>
                    <td className="px-1 py-1.5">
                      {wLists.length > 0 && (
                        <div onClick={() => selectAllInWebinar(w.id)} className={`w-3.5 h-3.5 rounded border-2 flex items-center justify-center cursor-pointer transition-colors ${
                          allInWebinarSelected ? "bg-violet-600 border-violet-600" : "border-zinc-600 hover:border-zinc-500"
                        }`}>
                          {allInWebinarSelected && <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3"><path d="M20 6L9 17l-5-5"/></svg>}
                        </div>
                      )}
                    </td>
                    <td className="px-2 py-1.5" onClick={() => toggleWebinar(w.id)}>
                      <div className="flex items-center gap-2">
                        <span className="text-zinc-900 dark:text-zinc-100 font-bold text-sm">{w.number}</span>
                        {w.variantLabel && (
                          <span
                            title={`A/B variant: ${w.variantLabel}`}
                            className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-violet-500/15 text-violet-500 border border-violet-500/30"
                          >
                            {w.variantLabel}
                          </span>
                        )}
                        <span className="text-[11px] text-zinc-500">{w.date}</span>
                        {w.broadcastId && w.broadcastId !== "—" && (
                          <span className="text-[9px] text-zinc-500 font-mono bg-zinc-100 dark:bg-zinc-800/60 px-1 py-0.5 rounded border border-zinc-300 dark:border-zinc-700/30">ID: {w.broadcastId}</span>
                        )}
                        {w.expanded && (
                          <div className="flex items-center gap-0.5 ml-1" onClick={(e) => e.stopPropagation()}>
                            <button onClick={async () => {
                              setEditWebinar({
                                id: w.id,
                                number: w.number,
                                date: w.isoDate,
                                broadcastId: w.broadcastId === "—" ? "" : w.broadcastId,
                                webinargeekCredentialId: w.webinargeekCredentialId ?? "",
                                status: w.status.toLowerCase(),
                                registrationLink: w.registrationLink,
                                unsubscribeLink: w.unsubscribeLink,
                                variantLabel: w.variantLabel ?? "",
                              });
                              // Ensure WG accounts are loaded, then refresh + load this account's broadcasts.
                              let creds = wgCredentials;
                              if (creds.length === 0) {
                                try { creds = (await fetchWgCredentials()).credentials; setWgCredentials(creds); }
                                catch (err) { console.error("Failed to load WG credentials:", err); }
                              }
                              const credId = (w.webinargeekCredentialId ?? "") || creds.find((c) => c.name === "default")?.id;
                              loadEditBroadcasts(credId, true);
                            }} className="p-0.5 rounded hover:bg-zinc-200 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 transition-colors" title="Edit webinar">
                              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                            </button>
                            <button onClick={() => handleDeleteWebinar(w.id)}
                              className="p-0.5 rounded hover:bg-red-500/10 text-zinc-500 hover:text-red-400 transition-colors" title="Delete webinar">
                              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
                              </svg>
                            </button>
                            {w.lists.length > 0 && (
                              <button
                                onClick={(e) => { e.stopPropagation(); toggleAssignForm(w.id); }}
                                className={`ml-1 px-2 py-0.5 text-[9px] font-semibold rounded transition-colors flex items-center gap-1 ${
                                  assigningWebinarId === w.id
                                    ? "bg-violet-600 text-white"
                                    : "bg-zinc-200 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-400 hover:bg-violet-500/20 hover:text-violet-500"
                                }`}
                              >
                                <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>
                                Assign
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    </td>
                    <td className="px-2 py-2.5"><StatusBadge status={w.status} /></td>
                    <td className="px-2 py-1.5" colSpan={3}>
                      <input
                        type="text"
                        defaultValue={w.mainTitle}
                        placeholder={`${wLists.length} lists assigned`}
                        onBlur={(e) => {
                          const val = e.target.value.trim();
                          if (val !== w.mainTitle) handleUpdateWebinar(w.id, "main_title", val);
                        }}
                        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                        onClick={(e) => e.stopPropagation()}
                        className="w-full bg-transparent text-zinc-800 dark:text-zinc-300 font-medium text-[11px] border-none focus:outline-none focus:ring-1 focus:ring-violet-500 rounded px-1 -ml-1 placeholder-zinc-500"
                      />
                    </td>
                    <td className="px-2 py-2.5 text-right font-mono text-zinc-800 dark:text-zinc-200 font-bold">{wTotal > 0 ? wTotal.toLocaleString() : ""}</td>
                    <td className="px-2 py-2.5 text-right font-mono text-violet-400 font-bold">{wRemain > 0 ? wRemain.toLocaleString() : ""}</td>
                    <td className="px-2 py-2.5" colSpan={2}></td>
                    <td className="px-2 py-2.5 text-right font-mono text-emerald-400 font-bold">{wAccounts > 0 ? wAccounts : ""}</td>
                    <td className="px-2 py-2.5 text-right" colSpan={3} onClick={(e) => e.stopPropagation()}>
                      <div className="flex items-center justify-end gap-1.5 flex-wrap">
                        <button
                          onClick={() => setReleaseModalWebinar({ id: w.id, number: w.number })}
                          className="px-2 py-1 rounded text-[10px] font-semibold inline-flex items-center gap-1.5 whitespace-nowrap border bg-zinc-200/60 dark:bg-zinc-800/60 text-zinc-600 dark:text-zinc-400 hover:text-amber-500 hover:bg-amber-500/10 border-zinc-300 dark:border-zinc-700/50"
                          title="Upload a CSV of emails you couldn't contact in time — they'll be released back to their buckets."
                        >
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12a9 9 0 11-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><polyline points="21 3 21 8 16 8"/></svg>
                          Release unused
                        </button>
                      {(() => {
                        const job = exportJobs.get(w.id);
                        const isActive = job?.status === "pending" || job?.status === "processing";
                        const isReady = job?.status === "ready";
                        const isFailed = job?.status === "failed";
                        const isDownloading = job ? downloadingJobIds.has(job.id) : false;

                        if (isActive) {
                          return (
                            <button
                              disabled
                              className="px-2 py-1 rounded text-[10px] font-semibold inline-flex items-center gap-1.5 bg-violet-500/10 text-violet-500 border border-violet-500/30 whitespace-nowrap cursor-wait"
                              title="Building CSV in the background — you can leave this page"
                            >
                              <span className="w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin inline-block" />
                              Building…
                            </button>
                          );
                        }
                        if (isReady && job) {
                          return (
                            <button
                              onClick={() => handleDownloadExport(w.id, w.number, job.id)}
                              disabled={isDownloading}
                              className="px-2 py-1 rounded text-[10px] font-semibold inline-flex items-center gap-1.5 bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25 border border-emerald-500/30 whitespace-nowrap disabled:opacity-60"
                              title={`Download CSV (${job.contact_count.toLocaleString()} contacts)`}
                            >
                              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                              Download CSV
                            </button>
                          );
                        }
                        return (
                          <button
                            onClick={() => handleStartExport(w.id)}
                            className={`px-2 py-1 rounded text-[10px] font-semibold inline-flex items-center gap-1.5 whitespace-nowrap border transition-colors ${
                              isFailed
                                ? "bg-red-500/10 text-red-500 hover:bg-red-500/20 border-red-500/30"
                                : "bg-zinc-200/60 dark:bg-zinc-800/60 text-zinc-600 dark:text-zinc-400 hover:text-violet-500 hover:bg-violet-500/10 border-zinc-300 dark:border-zinc-700/50"
                            }`}
                            title={isFailed ? `Export failed: ${job?.error_message ?? "unknown error"} — click to retry` : "Export all assigned contacts as CSV (Email, List name)"}
                          >
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                            {isFailed ? "Retry export" : "Export lists"}
                          </button>
                        );
                      })()}
                      </div>
                    </td>
                  </tr>

                  {/* ── Assignment section (only for the active webinar) ── */}
                  {w.expanded && (assigningWebinarId === w.id || w.lists.length === 0) && (
                    <tr>
                      <td colSpan={15} className="p-0">
                        <div className="relative z-20 bg-zinc-50 dark:bg-zinc-900/40 border-y border-zinc-200 dark:border-zinc-800/30 px-6 py-4">
                          <div className="flex items-center justify-between mb-3">
                            <div className="flex items-center gap-3">
                              <span className="text-xs font-semibold text-zinc-600 dark:text-zinc-400 uppercase tracking-wider">Assign Lists to W{w.number}</span>
                              <div className="flex gap-1 bg-zinc-200 dark:bg-zinc-800 rounded-lg p-0.5">
                                <button
                                  onClick={() => setAssignTab("buckets")}
                                  className={`px-3 py-1 rounded-md text-[10px] font-semibold transition-all ${
                                    assignTab === "buckets"
                                      ? "bg-white dark:bg-zinc-700 text-zinc-900 dark:text-zinc-100 shadow-sm"
                                      : "text-zinc-500 dark:text-zinc-400 hover:text-zinc-700"
                                  }`}
                                >Buckets</button>
                                <button
                                  onClick={() => setAssignTab("custom_lists")}
                                  className={`px-3 py-1 rounded-md text-[10px] font-semibold transition-all ${
                                    assignTab === "custom_lists"
                                      ? "bg-white dark:bg-zinc-700 text-zinc-900 dark:text-zinc-100 shadow-sm"
                                      : "text-zinc-500 dark:text-zinc-400 hover:text-zinc-700"
                                  }`}
                                >Custom Lists</button>
                              </div>
                            </div>
                            {assignTab === "buckets" ? (
                              <span className="text-[10px] text-zinc-500">{buckets.filter((b) => b.remaining_contacts > 0).length} buckets available · {buckets.reduce((s, b) => s + (b.remaining_contacts || 0), 0).toLocaleString()} contacts</span>
                            ) : (
                              <span className="text-[10px] text-zinc-500">{customLists.length} custom lists available</span>
                            )}
                          </div>

                          {/* Assignment form — row 1: source + sender + volume */}
                          <div className="flex items-end gap-3 mb-2">
                            {assignTab === "buckets" && (
                            <div className="flex-1 min-w-[200px]">
                              <label className="text-[10px] text-zinc-500 uppercase tracking-wider block mb-1">Bucket</label>
                              <Dropdown
                                placeholder="Select bucket..."
                                value={assignBucket}
                                onChange={(val) => {
                                  setAssignBucket(val);
                                  const b = buckets.find((b) => b.id === val);
                                  if (b) {
                                    let vol = b.remaining_contacts;
                                    setAssignCountries((b.countries || []).join(", "));
                                    setAssignEmpRange(b.emp_range || "");
                                    setAssignAccounts(0);
                                    // If sender already selected, cap volume to what available accounts can handle
                                    if (assignSender) {
                                      const s = senders.find((s) => s.id === assignSender);
                                      if (s) {
                                        const spa = assignSendPerAcct > 0 ? assignSendPerAcct : s.sendPerAccount;
                                        const d = assignDays > 0 ? assignDays : s.daysPerWeek;
                                        const avail = getAvailableAccounts(w.id, assignSender);
                                        const maxVol = avail * spa * d;
                                        if (vol > maxVol && maxVol > 0) vol = maxVol;
                                      }
                                    }
                                    setAssignVolume(vol);
                                  }
                                }}
                                options={buckets.filter((b) => b.remaining_contacts > 0).map((b) => ({
                                  value: b.id,
                                  label: `${b.name} (${b.remaining_contacts.toLocaleString()} remaining)`,
                                }))}
                              />
                            </div>
                            )}
                            <div className="w-56 ml-auto">
                              <label className="text-[10px] text-zinc-500 uppercase tracking-wider block mb-1">Sender</label>
                              <Dropdown
                                placeholder="Select..."
                                value={assignSender}
                                onChange={(val) => {
                                  setAssignSender(val);
                                  const s = senders.find((s) => s.id === val);
                                  if (s) {
                                    // Prefill send/acct and days from sender defaults
                                    setAssignSendPerAcct(s.sendPerAccount);
                                    setAssignDays(s.daysPerWeek);
                                    setAssignAccounts(0);
                                    // Cap volume if available accounts can't handle the full bucket
                                    const avail = getAvailableAccounts(w.id, val);
                                    const maxVol = avail * s.sendPerAccount * s.daysPerWeek;
                                    if (assignVolume > maxVol && maxVol > 0) {
                                      setAssignVolume(maxVol);
                                    }
                                  }
                                }}
                                options={senders.map((s) => {
                                  const used = getAccountsUsedForSender(w.id, s.id);
                                  const avail = s.accounts - used;
                                  return {
                                    value: s.id,
                                    label: `${s.name} (${avail}/${s.accounts} accts free)`,
                                  };
                                })}
                              />
                            </div>
                            <div className="w-32">
                              <label className="text-[10px] text-zinc-500 uppercase tracking-wider block mb-1">Contacts</label>
                              <input type="number" value={assignVolume || ""} onChange={(e) => { setAssignVolume(parseInt(e.target.value) || 0); setAssignAccounts(0); }} className="w-full bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-1.5 text-sm text-zinc-800 dark:text-zinc-200 font-mono focus:outline-none focus:ring-1 focus:ring-violet-500" />
                            </div>
                            <button onClick={() => handleAssign(w.id)} disabled={(assignTab === "custom_lists" ? !assignCustomList : !assignBucket) || !assignSender || assignVolume <= 0 || assignInFlight === w.id}
                              className="px-4 py-1.5 bg-violet-600 hover:bg-violet-500 disabled:bg-zinc-200 dark:disabled:bg-zinc-700 disabled:text-zinc-500 disabled:cursor-wait text-white text-xs font-semibold rounded-lg transition-colors whitespace-nowrap inline-flex items-center gap-1.5">
                              {assignInFlight === w.id && (
                                <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
                              )}
                              {assignInFlight === w.id ? "Assigning…" : "Assign →"}
                            </button>
                          </div>

                          {/* Assignment form — row 2: sending config (shown when bucket + sender selected) */}
                          {(assignTab === "custom_lists" ? assignCustomList : assignBucket) && assignSender && (() => {
                            const s = senders.find((s) => s.id === assignSender);
                            if (!s) return null;
                            const usedAccts = getAccountsUsedForSender(w.id, s.id);
                            const availAccts = Math.max(0, s.accounts - usedAccts);
                            const sendPerAcct = assignSendPerAcct > 0 ? assignSendPerAcct : s.sendPerAccount;
                            // Accounts needed = ceil(volume / (send_per_acct × days))
                            const calculatedAccts = sendPerAcct > 0 && assignDays > 0
                              ? Math.ceil(assignVolume / (sendPerAcct * assignDays))
                              : 0;
                            const accts = assignAccounts > 0 ? assignAccounts : calculatedAccts;
                            const dailyCap = accts * sendPerAcct;
                            const totalCapacity = dailyCap * assignDays;
                            const overAllocated = accts > availAccts;
                            return (
                              <div className="space-y-2 mb-4">
                                {/* Sender info + account availability */}
                                <div className="flex items-center gap-3">
                                  <div className="flex items-center gap-2">
                                    <SenderBadge name={s.name} color={s.color} />
                                    <div className="flex items-center gap-1.5">
                                      <span className="text-[10px] text-zinc-500">Accounts:</span>
                                      <span className={`text-[11px] font-mono font-bold ${availAccts === 0 ? 'text-red-400' : availAccts <= 2 ? 'text-amber-400' : 'text-emerald-400'}`}>
                                        {availAccts}
                                      </span>
                                      <span className="text-[10px] text-zinc-500">/ {s.accounts} available</span>
                                      {usedAccts > 0 && (
                                        <span className="text-[9px] text-zinc-500 bg-zinc-100 dark:bg-zinc-800/60 px-1.5 py-0.5 rounded border border-zinc-300 dark:border-zinc-700/30">
                                          {usedAccts} used in W{w.number}
                                        </span>
                                      )}
                                    </div>
                                  </div>
                                  {/* Account usage bar */}
                                  <div className="flex-1 max-w-[120px]">
                                    <div className="h-1.5 bg-zinc-200 dark:bg-zinc-800 rounded-full overflow-hidden">
                                      <div
                                        className={`h-full rounded-full transition-all ${usedAccts / s.accounts > 0.8 ? 'bg-red-400' : usedAccts / s.accounts > 0.5 ? 'bg-amber-400' : 'bg-emerald-400'}`}
                                        style={{ width: `${Math.min(100, (usedAccts / s.accounts) * 100)}%` }}
                                      />
                                    </div>
                                  </div>
                                </div>

                                {/* Sending calculation: volume ÷ send/acct ÷ days = accounts needed */}
                                <div className="flex items-center gap-3">
                                  <div className="flex items-center gap-2 bg-zinc-100 dark:bg-zinc-800/40 border border-zinc-300 dark:border-zinc-700/30 rounded-lg px-3 py-2">
                                    <div className="flex items-center gap-1.5">
                                      <span className="text-[11px] text-violet-400 font-mono font-bold">{assignVolume.toLocaleString()}</span>
                                      <span className="text-[9px] text-zinc-500 uppercase">contacts</span>
                                      <span className="text-zinc-600 text-[10px]">÷</span>
                                      <div className="flex flex-col items-center">
                                        <span className="text-[8px] text-zinc-600 uppercase">Send/Acct</span>
                                        <input type="number" value={sendPerAcct} onChange={(e) => { setAssignSendPerAcct(parseInt(e.target.value) || 0); setAssignAccounts(0); }}
                                          className="w-14 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-1.5 py-0.5 text-[11px] text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500" />
                                      </div>
                                      <span className="text-zinc-600 text-[10px]">÷</span>
                                      <div className="flex flex-col items-center">
                                        <span className="text-[8px] text-zinc-600 uppercase">Days/Web</span>
                                        <input type="number" value={assignDays} onChange={(e) => { setAssignDays(parseInt(e.target.value) || 5); setAssignAccounts(0); }}
                                          className="w-14 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded px-1.5 py-0.5 text-[11px] text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500" />
                                      </div>
                                      <span className="text-zinc-600 text-[10px]">=</span>
                                      <div className="flex flex-col items-center">
                                        <span className="text-[8px] text-zinc-600 uppercase">Accts needed</span>
                                        <input type="number" value={accts} onChange={(e) => setAssignAccounts(parseInt(e.target.value) || 0)}
                                          className={`w-14 bg-white dark:bg-zinc-800 border rounded px-1.5 py-0.5 text-[11px] font-mono text-center font-bold focus:outline-none focus:ring-1 focus:ring-violet-500 ${
                                            overAllocated ? 'border-red-400 text-red-400' : 'border-zinc-300 dark:border-zinc-700/60 text-emerald-500 dark:text-emerald-400'
                                          }`} />
                                      </div>
                                    </div>
                                  </div>
                                  <div className="flex flex-col gap-0.5">
                                    <span className="text-[10px] text-zinc-500">
                                      {accts} accts × {sendPerAcct}/acct × {assignDays}d = <span className="text-violet-400 font-bold">{totalCapacity.toLocaleString()}</span> capacity
                                      {totalCapacity > 0 && assignVolume > 0 && totalCapacity !== assignVolume && (
                                        <span className="text-zinc-600"> · {totalCapacity > assignVolume ? `${(totalCapacity - assignVolume).toLocaleString()} headroom` : `${(assignVolume - totalCapacity).toLocaleString()} over`}</span>
                                      )}
                                    </span>
                                    {overAllocated && (
                                      <span className="text-[10px] text-red-400 font-medium">⚠ Exceeds available accounts by {accts - availAccts}</span>
                                    )}
                                  </div>
                                </div>
                              </div>
                            );
                          })()}

                          {/* Available sources mini-table */}
                          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 overflow-hidden max-h-[200px] overflow-y-auto">
                            <table className="w-full text-xs">
                              {assignTab === "buckets" ? (
                                <>
                                  <thead>
                                    {(() => {
                                      const availBuckets = buckets.filter((b) => b.remaining_contacts > 0 && !isDisqualifiedBucket(b));
                                      const totalSum = availBuckets.reduce((s, b) => s + b.total_contacts, 0);
                                      const remainSum = availBuckets.reduce((s, b) => s + b.remaining_contacts, 0);
                                      return (
                                        <tr className="bg-zinc-100 dark:bg-zinc-800/40">
                                          <th className="text-left px-3 py-1.5 text-zinc-500 font-medium">Bucket <span className="text-zinc-400 font-normal">{availBuckets.length}</span></th>
                                          <th className="text-right px-3 py-1.5 text-zinc-500 font-medium">Total <span className="text-zinc-400 font-normal">{totalSum.toLocaleString()}</span></th>
                                          <th className="text-right px-3 py-1.5 text-zinc-500 font-medium">Remaining <span className="text-violet-400/70 font-normal">{remainSum.toLocaleString()}</span></th>
                                        </tr>
                                      );
                                    })()}
                                  </thead>
                                  <tbody className="divide-y divide-zinc-800/20">
                                    {buckets
                                      .filter((b) => b.remaining_contacts > 0)
                                      .slice()
                                      .sort((a, b) => Number(isDisqualifiedBucket(a)) - Number(isDisqualifiedBucket(b)))
                                      .map((b) => (
                                      <tr key={b.id} onClick={() => {
                                        setAssignBucket(b.id);
                                        setAssignCountries((b.countries || []).join(", "));
                                        setAssignEmpRange(b.emp_range || "");
                                        setAssignAccounts(0);
                                        let vol = b.remaining_contacts;
                                        if (assignSender) {
                                          const s = senders.find((s) => s.id === assignSender);
                                          if (s) {
                                            const spa = assignSendPerAcct > 0 ? assignSendPerAcct : s.sendPerAccount;
                                            const d = assignDays > 0 ? assignDays : s.daysPerWeek;
                                            const avail = getAvailableAccounts(w.id, assignSender);
                                            const maxVol = avail * spa * d;
                                            if (vol > maxVol && maxVol > 0) vol = maxVol;
                                          }
                                        }
                                        setAssignVolume(vol);
                                      }}
                                        className={`cursor-pointer transition-colors ${assignBucket === b.id ? "bg-violet-500/10" : "hover:bg-zinc-200 dark:hover:bg-zinc-800/30"}`}>
                                        <td className="px-3 py-1.5 text-zinc-800 dark:text-zinc-300 font-medium">{b.name}</td>
                                        <td className="px-3 py-1.5 text-right font-mono text-zinc-600 dark:text-zinc-400">{b.total_contacts.toLocaleString()}</td>
                                        <td className="px-3 py-1.5 text-right font-mono text-violet-400">{b.remaining_contacts.toLocaleString()}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </>
                              ) : (
                                <>
                                  <thead>
                                    {(() => {
                                      const availLists = customLists.filter((l) => l.available_contacts > 0);
                                      const totalSum = availLists.reduce((s, l) => s + l.total_contacts, 0);
                                      const remainSum = availLists.reduce((s, l) => s + l.available_contacts, 0);
                                      return (
                                        <tr className="bg-zinc-100 dark:bg-zinc-800/40">
                                          <th className="text-left px-3 py-1.5 text-zinc-500 font-medium">Custom List <span className="text-zinc-400 font-normal">{availLists.length}</span></th>
                                          <th className="text-right px-3 py-1.5 text-zinc-500 font-medium">Total <span className="text-zinc-400 font-normal">{totalSum.toLocaleString()}</span></th>
                                          <th className="text-right px-3 py-1.5 text-zinc-500 font-medium">Remaining <span className="text-violet-400/70 font-normal">{remainSum.toLocaleString()}</span></th>
                                        </tr>
                                      );
                                    })()}
                                  </thead>
                                  <tbody className="divide-y divide-zinc-800/20">
                                    {customLists.filter((l) => l.available_contacts > 0).map((l) => (
                                      <tr key={l.id} onClick={() => {
                                        setAssignCustomList(l.id);
                                        setAssignVolume(l.available_contacts);
                                        setAssignAccounts(0);
                                      }}
                                        className={`cursor-pointer transition-colors ${assignCustomList === l.id ? "bg-violet-500/10" : "hover:bg-zinc-200 dark:hover:bg-zinc-800/30"}`}>
                                        <td className="px-3 py-1.5 text-zinc-800 dark:text-zinc-300 font-medium">{l.name}</td>
                                        <td className="px-3 py-1.5 text-right font-mono text-zinc-600 dark:text-zinc-400">{l.total_contacts.toLocaleString()}</td>
                                        <td className="px-3 py-1.5 text-right font-mono text-violet-400">{l.available_contacts.toLocaleString()}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </>
                              )}
                            </table>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}

                  {/* ── Child list rows (grouped by bucket) ─────────── */}
                  {w.expanded && (() => {
                    type Group = { bucketId: string; bucketName: string; lists: PlannedList[] };
                    const groups: Group[] = [];
                    const seen = new Map<string, number>();
                    const specialLists: PlannedList[] = [];
                    const unbucketedLists: PlannedList[] = [];
                    for (const l of w.lists) {
                      if (l.isNonjoiners || l.isNoListData) { specialLists.push(l); continue; }
                      if (!l.bucketId) { unbucketedLists.push(l); continue; }
                      const idx = seen.get(l.bucketId);
                      if (idx !== undefined) groups[idx].lists.push(l);
                      else {
                        seen.set(l.bucketId, groups.length);
                        groups.push({ bucketId: l.bucketId, bucketName: l.bucket, lists: [l] });
                      }
                    }

                    const renderRow = (l: PlannedList) => (
                    <tr key={l.id} className={`border-b border-zinc-200 dark:border-zinc-800/20 transition-colors ${
                      l.isNonjoiners || l.isNoListData ? "bg-zinc-50 dark:bg-zinc-900/20 text-zinc-500 italic" :
                      selectedIds.has(l.id) ? "bg-violet-500/5" :
                      l.isSetup ? "bg-emerald-500/5 hover:bg-emerald-500/10" :
                      "hover:bg-zinc-100 dark:bg-zinc-800/20"
                    }`}>
                      <td className="px-2 py-1.5"></td>
                      <td className="px-1 py-1.5">
                        {!l.isNonjoiners && !l.isNoListData && (
                          <div onClick={() => toggleSelect(l.id)} className={`w-3.5 h-3.5 rounded border-2 flex items-center justify-center cursor-pointer transition-colors ${
                            selectedIds.has(l.id) ? "bg-violet-600 border-violet-600" : "border-zinc-600 hover:border-zinc-500"
                          }`}>
                            {selectedIds.has(l.id) && <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3"><path d="M20 6L9 17l-5-5"/></svg>}
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-1.5">
                        {!l.isNonjoiners && !l.isNoListData && (
                          <input
                            type="text"
                            defaultValue={l.listName || getDefaultListName(w.lists, l)}
                            onBlur={(e) => {
                              const val = e.target.value.trim();
                              const defaultName = getDefaultListName(w.lists, l);
                              const newName = val === defaultName ? undefined : val || undefined;
                              if (newName !== l.listName) {
                                setWebinars((prev) => prev.map((ww) => ({ ...ww, lists: ww.lists.map((ll) => ll.id === l.id ? { ...ll, listName: newName } : ll) })));
                                apiUpdateAssignment(l.id, { list_name: val || "" }).catch(console.error);
                              }
                            }}
                            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                            className="w-full bg-transparent text-zinc-600 dark:text-zinc-400 text-[10px] border-none focus:outline-none focus:ring-1 focus:ring-violet-500 rounded px-0.5 placeholder-zinc-500 truncate"
                            title={l.listName || getDefaultListName(w.lists, l)}
                          />
                        )}
                      </td>
                      <td className="px-2 py-1.5"></td>
                      <td className="px-2 py-1.5">
                        <div className="flex items-center gap-1.5">
                          <span className={l.isNonjoiners || l.isNoListData ? "text-zinc-500" : "text-zinc-800 dark:text-zinc-300"}>{l.description}</span>
                          {!l.isNonjoiners && !l.isNoListData && (
                            l.listUrl ? (
                              <a href={l.listUrl} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} title={l.listUrl}
                                className="text-violet-400 hover:text-violet-300 shrink-0">
                                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                              </a>
                            ) : (
                              <button onClick={(e) => {
                                e.stopPropagation();
                                const url = prompt("Paste list URL:");
                                if (url) {
                                  setWebinars((prev) => prev.map((ww) => ({ ...ww, lists: ww.lists.map((ll) => ll.id === l.id ? { ...ll, listUrl: url } : ll) })));
                                  apiUpdateAssignment(l.id, { list_url: url }).catch(console.error);
                                }
                              }} title="Add list URL" className="text-zinc-500 hover:text-violet-400 shrink-0">
                                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/></svg>
                              </button>
                            )
                          )}
                        </div>
                      </td>
                      <td className="px-2 py-1.5 w-[130px] max-w-[130px]">
                        {l.bucket !== "—" ? (
                          <span
                            title={l.bucket}
                            className="text-zinc-600 dark:text-zinc-400 text-[10px] bg-zinc-100 dark:bg-zinc-800/60 px-1.5 py-0.5 rounded border border-zinc-300 dark:border-zinc-700/30 inline-block max-w-full truncate align-middle"
                          >
                            {l.bucket}
                          </span>
                        ) : <span className="text-zinc-600">—</span>}
                      </td>
                      <td className="px-2 py-1.5"><SenderBadge name={l.sender} color={l.senderColor} /></td>
                      <td className="px-2 py-1.5 text-right font-mono">
                        {l.listVolumeRaw > 0 ? (
                          <a
                            href={`/contacts/${l.id}?tab=all`}
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            title={`Total: ${l.listVolumeRaw.toLocaleString()} · Blocklisted: ${l.listBlocklistedTotal.toLocaleString()} · Available: ${l.listSize.toLocaleString()}`}
                            className="text-violet-500 hover:text-violet-400 underline underline-offset-2 decoration-violet-500/30 hover:decoration-violet-400/50 transition-colors"
                          >
                            {l.listSize.toLocaleString()}
                          </a>
                        ) : ""}
                      </td>
                      <td className="px-2 py-1.5 text-right">
                        {l.listVolumeRaw > 0 ? (
                          <a
                            href={`/contacts/${l.id}?tab=assigned`}
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            className="font-mono text-violet-400 hover:text-violet-300 underline underline-offset-2 decoration-violet-500/30 hover:decoration-violet-400/50 transition-colors"
                          >
                            {l.listRemain > 0 ? l.listRemain.toLocaleString() : "0"}
                          </a>
                        ) : <span className="font-mono text-violet-400"></span>}
                      </td>
                      <td className="px-2 py-1.5">
                        {l.title ? (
                          <div
                            className="max-w-[240px] cursor-pointer group/title"
                            onClick={() => openVariationsModal(l.id, w.id, "title")}
                          >
                            {(() => {
                              const selectedTitle = l.titleVariants?.find(v => v.selected);
                              if (!selectedTitle) return null;
                              const showVariantBadge = l.titleVariants && l.titleVariants.length > 1;
                              return (
                                <span className="inline-flex items-center gap-1 mb-0.5">
                                  {showVariantBadge && (
                                    <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400">
                                      V{selectedTitle.variantIndex + 1}
                                    </span>
                                  )}
                                  <CopyVariantButton text={selectedTitle.text} tone="title" />
                                </span>
                              );
                            })()}
                            <span className="text-zinc-700 dark:text-zinc-300 text-[10px] leading-snug truncate block overflow-hidden group-hover/title:text-violet-600 dark:group-hover/title:text-violet-400 transition-colors" title={l.title}>{l.title}</span>
                            <span className="text-[9px] text-violet-500 font-medium mt-0.5 block">
                              {l.titleVariants && l.titleVariants.length > 1 ? `${l.titleVariants.length} variations` : "View →"}
                            </span>
                          </div>
                        ) : (l.bucketId || l.sourceUploadId) ? (
                          // No title selected yet — let the user open the modal
                          // to pick a variant manually (can happen when the bucket
                          // has no primary copy set at assignment time).
                          <button
                            onClick={() => openVariationsModal(l.id, w.id, "title")}
                            className="text-[10px] font-semibold px-2 py-1 rounded border border-dashed border-zinc-400 dark:border-zinc-700 text-zinc-500 hover:text-violet-500 hover:border-violet-400 dark:hover:border-violet-500 transition-colors"
                          >
                            + Pick title
                          </button>
                        ) : <span className="text-zinc-600">—</span>}
                      </td>
                      <td className="px-2 py-1.5">
                        {(() => {
                          const selectedDesc = l.descVariants?.find(v => v.selected);
                          const descText = selectedDesc?.text || "";
                          return descText ? (
                            <div
                              className="max-w-[240px] cursor-pointer group/desc"
                              onClick={() => openVariationsModal(l.id, w.id, "description")}
                            >
                              {selectedDesc && (
                                <span className="inline-flex items-center gap-1 mb-0.5">
                                  {l.descVariants && l.descVariants.length > 1 && (
                                    <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-500/15 text-blue-600 dark:text-blue-400">
                                      V{selectedDesc.variantIndex + 1}
                                    </span>
                                  )}
                                  <CopyVariantButton text={selectedDesc.text} tone="description" registrationLink={w.registrationLink} unsubscribeLink={w.unsubscribeLink} />
                                </span>
                              )}
                              <span className="text-zinc-700 dark:text-zinc-300 text-[10px] leading-snug truncate block overflow-hidden group-hover/desc:text-blue-600 dark:group-hover/desc:text-blue-400 transition-colors" title={descText}>{descText.split("\n")[0]}</span>
                              <span className="text-[9px] text-blue-500 font-medium mt-0.5 block">
                                {l.descVariants && l.descVariants.length > 1 ? `${l.descVariants.length} variations` : "View →"}
                              </span>
                            </div>
                          ) : (l.bucketId || l.sourceUploadId) ? (
                            <button
                              onClick={() => openVariationsModal(l.id, w.id, "description")}
                              className="text-[10px] font-semibold px-2 py-1 rounded border border-dashed border-zinc-400 dark:border-zinc-700 text-zinc-500 hover:text-blue-500 hover:border-blue-400 dark:hover:border-blue-500 transition-colors"
                            >
                              + Pick description
                            </button>
                          ) : <span className="text-zinc-600">—</span>;
                        })()}
                      </td>
                      <td className="px-2 py-1.5 text-right font-mono text-emerald-400">{l.accountsNeeded > 0 ? l.accountsNeeded : ""}</td>
                      <td className="px-2 py-1.5 text-center">
                        {l.copiesGenerated ? (
                          <span className="w-4 h-4 rounded-full bg-emerald-500/20 text-emerald-400 inline-flex items-center justify-center">
                            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"><path d="M20 6L9 17l-5-5"/></svg>
                          </span>
                        ) : !l.isNonjoiners && !l.isNoListData ? (
                          <span className="text-zinc-600 text-[10px]">—</span>
                        ) : null}
                      </td>
                      <td className="px-2 py-1.5 text-center">
                        {!l.isNonjoiners && !l.isNoListData && (
                          <button
                            onClick={() => handleToggleSetup(l.id, w.id, !!l.isSetup)}
                            className={`px-2 py-1 rounded text-[10px] font-semibold transition-colors inline-flex items-center gap-1 ${
                              l.isSetup
                                ? "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25 border border-emerald-500/30"
                                : "bg-zinc-200/60 dark:bg-zinc-800/60 text-zinc-500 hover:text-emerald-500 hover:bg-emerald-500/10 border border-zinc-300 dark:border-zinc-700/50"
                            }`}
                            title={l.isSetup ? "Marked as set up — click to unmark" : "Mark this list as set up in your outreach tool"}
                          >
                            {l.isSetup ? (
                              <>
                                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
                                Setup
                              </>
                            ) : (
                              "Mark setup"
                            )}
                          </button>
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-center">
                        {!l.isNonjoiners && !l.isNoListData && (
                          <button
                            onClick={() => handleDeleteAssignment(l.id, w.id)}
                            className="p-1 rounded hover:bg-red-500/10 text-zinc-500 hover:text-red-400 transition-colors"
                            title="Remove assignment"
                          >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                              <path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
                            </svg>
                          </button>
                        )}
                      </td>
                    </tr>
                    );

                    // Split: bucket groups with multiple lists vs. single-list buckets
                    const multiListGroups = groups.filter((g) => g.lists.length >= 2);
                    const singleListLists = groups.filter((g) => g.lists.length === 1).map((g) => g.lists[0]);

                    const renderBucketGroup = (g: Group) => {
                        const groupKey = `${w.id}::${g.bucketId}`;
                        const collapsed = collapsedBuckets.has(groupKey);
                        const sumSize = g.lists.reduce((s, l) => s + l.listSize, 0);
                        const sumRemain = g.lists.reduce((s, l) => s + l.listRemain, 0);
                        const sumAccts = Math.round(g.lists.reduce((s, l) => s + l.accountsNeeded, 0));
                        const senderMap = new Map<string, { name: string; color?: string }>();
                        for (const cl of g.lists) {
                          if (cl.senderId && !senderMap.has(cl.senderId)) {
                            senderMap.set(cl.senderId, { name: cl.sender, color: cl.senderColor });
                          }
                        }
                        const uniqSenders = [...senderMap.values()];
                        const groupListIds = g.lists.map((l) => l.id);
                        const allGroupSelected = groupListIds.length > 0 && groupListIds.every((id) => selectedIds.has(id));
                        const allSetup = g.lists.every((l) => l.isSetup);

                        const header = (
                          <tr
                            key={`bucket-${g.bucketId}`}
                            onClick={() => toggleBucketGroup(w.id, g.bucketId)}
                            className="bg-zinc-100/70 dark:bg-zinc-800/25 hover:bg-zinc-200/70 dark:hover:bg-zinc-800/45 cursor-pointer border-b border-zinc-200 dark:border-zinc-800/30 transition-colors"
                          >
                            <td className="px-2 py-2 text-center">
                              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                                className={`text-zinc-500 dark:text-zinc-400 transition-transform duration-200 ${collapsed ? "" : "rotate-90"}`}>
                                <path d="M9 18l6-6-6-6"/>
                              </svg>
                            </td>
                            <td className="px-1 py-2">
                              <div
                                onClick={(e) => { e.stopPropagation(); selectAllInBucketGroup(w.id, g.bucketId); }}
                                className={`w-3.5 h-3.5 rounded border-2 flex items-center justify-center cursor-pointer transition-colors ${
                                  allGroupSelected ? "bg-violet-600 border-violet-600" : "border-zinc-500 hover:border-zinc-400"
                                }`}
                              >
                                {allGroupSelected && <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3"><path d="M20 6L9 17l-5-5"/></svg>}
                              </div>
                            </td>
                            <td className="px-2 py-2"></td>
                            <td className="px-2 py-2"></td>
                            <td className="px-2 py-2"></td>
                            <td className="px-2 py-2">
                              <div className="flex items-center gap-2">
                                <span
                                  title={g.bucketName}
                                  className="text-zinc-800 dark:text-zinc-100 text-xs font-bold truncate max-w-[280px]"
                                >
                                  {g.bucketName}
                                </span>
                                <span className="text-[10px] font-semibold text-zinc-500 dark:text-zinc-400 px-1.5 py-0.5 rounded bg-zinc-200 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/40">
                                  {g.lists.length}
                                </span>
                              </div>
                            </td>
                            <td className="px-2 py-2">
                              <div className="flex items-center gap-1 relative group/senders">
                                {uniqSenders.slice(0, 2).map((s) => <SenderBadge key={s.name} name={s.name} color={s.color} />)}
                                {uniqSenders.length > 2 && (
                                  <span className="text-[9px] text-zinc-500 font-semibold cursor-default">+{uniqSenders.length - 2}</span>
                                )}
                                {uniqSenders.length > 2 && (
                                  <div className="hidden group-hover/senders:flex absolute left-0 top-full mt-1 z-50 bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-lg shadow-lg p-1.5 gap-1 flex-wrap min-w-[120px]">
                                    {uniqSenders.map((s) => <SenderBadge key={s.name} name={s.name} color={s.color} />)}
                                  </div>
                                )}
                              </div>
                            </td>
                            <td className="px-2 py-2 text-right">
                              {sumSize > 0 ? (() => {
                                const linkable = g.lists.filter((l) => l.listVolumeRaw > 0);
                                const href = linkable.length > 0
                                  ? `/contacts/group?ids=${linkable.map((l) => l.id).join(",")}&tab=all`
                                  : null;
                                return href ? (
                                  <a
                                    href={href}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    onClick={(e) => e.stopPropagation()}
                                    title={`Open combined view of ${linkable.length} list${linkable.length === 1 ? "" : "s"}`}
                                    className="font-mono font-bold text-zinc-800 dark:text-zinc-100 hover:text-violet-500 dark:hover:text-violet-400 underline underline-offset-2 decoration-zinc-400/40 hover:decoration-violet-400/50 transition-colors"
                                  >
                                    {sumSize.toLocaleString()}
                                  </a>
                                ) : (
                                  <span className="font-mono font-bold text-zinc-800 dark:text-zinc-100">{sumSize.toLocaleString()}</span>
                                );
                              })() : ""}
                            </td>
                            <td className="px-2 py-2 text-right">
                              {sumRemain > 0 ? (() => {
                                const linkable = g.lists.filter((l) => l.listVolumeRaw > 0);
                                const href = linkable.length > 0
                                  ? `/contacts/group?ids=${linkable.map((l) => l.id).join(",")}&tab=assigned`
                                  : null;
                                return href ? (
                                  <a
                                    href={href}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    onClick={(e) => e.stopPropagation()}
                                    title={`Open combined view of ${linkable.length} list${linkable.length === 1 ? "" : "s"}`}
                                    className="font-mono font-bold text-violet-400 hover:text-violet-300 underline underline-offset-2 decoration-violet-500/30 hover:decoration-violet-400/50 transition-colors"
                                  >
                                    {sumRemain.toLocaleString()}
                                  </a>
                                ) : (
                                  <span className="font-mono font-bold text-violet-400">{sumRemain.toLocaleString()}</span>
                                );
                              })() : ""}
                            </td>
                            <td className="px-2 py-2"></td>
                            <td className="px-2 py-2"></td>
                            <td className="px-2 py-2 text-right font-mono font-bold text-emerald-400">
                              {sumAccts > 0 ? sumAccts : ""}
                            </td>
                            <td className="px-2 py-2"></td>
                            <td className="px-2 py-2 text-center">
                              {allSetup && (
                                <span className="text-[9px] font-semibold text-emerald-500">✓</span>
                              )}
                            </td>
                            <td className="px-2 py-2"></td>
                          </tr>
                        );
                        return { header, collapsed, lists: g.lists };
                    };

                    const nodes: ReactNode[] = [];

                    // 1) Multi-list bucket groups (bucket-name as header)
                    for (const g of multiListGroups) {
                      const { header, collapsed, lists } = renderBucketGroup(g);
                      nodes.push(header);
                      if (!collapsed) lists.forEach((l) => nodes.push(renderRow(l)));
                    }

                    // 2) "Unique Buckets" synthetic group — all buckets that appear only once in this webinar
                    if (singleListLists.length > 0) {
                      const groupKey = `${w.id}::__unique__`;
                      const collapsed = collapsedBuckets.has(groupKey);
                      const sumSize = singleListLists.reduce((s, l) => s + l.listSize, 0);
                      const sumRemain = singleListLists.reduce((s, l) => s + l.listRemain, 0);
                      const sumAccts = Math.round(singleListLists.reduce((s, l) => s + l.accountsNeeded, 0));
                      const senderMap = new Map<string, { name: string; color?: string }>();
                      for (const cl of singleListLists) {
                        if (cl.senderId && !senderMap.has(cl.senderId)) {
                          senderMap.set(cl.senderId, { name: cl.sender, color: cl.senderColor });
                        }
                      }
                      const uniqSenders = [...senderMap.values()];
                      const uniqListIds = singleListLists.map((l) => l.id);
                      const allGroupSelected = uniqListIds.length > 0 && uniqListIds.every((id) => selectedIds.has(id));

                      nodes.push(
                        <tr
                          key={`unique-${w.id}`}
                          onClick={() => {
                            setCollapsedBuckets((prev) => {
                              const next = new Set(prev);
                              if (next.has(groupKey)) next.delete(groupKey); else next.add(groupKey);
                              return next;
                            });
                          }}
                          className="bg-zinc-100/70 dark:bg-zinc-800/25 hover:bg-zinc-200/70 dark:hover:bg-zinc-800/45 cursor-pointer border-b border-zinc-200 dark:border-zinc-800/30 transition-colors"
                        >
                          <td className="px-2 py-2 text-center">
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                              className={`text-zinc-500 dark:text-zinc-400 transition-transform duration-200 ${collapsed ? "" : "rotate-90"}`}>
                              <path d="M9 18l6-6-6-6"/>
                            </svg>
                          </td>
                          <td className="px-1 py-2">
                            <div
                              onClick={(e) => { e.stopPropagation(); selectAllInUniqueGroup(uniqListIds); }}
                              className={`w-3.5 h-3.5 rounded border-2 flex items-center justify-center cursor-pointer transition-colors ${
                                allGroupSelected ? "bg-violet-600 border-violet-600" : "border-zinc-500 hover:border-zinc-400"
                              }`}
                            >
                              {allGroupSelected && <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3"><path d="M20 6L9 17l-5-5"/></svg>}
                            </div>
                          </td>
                          <td className="px-2 py-2"></td>
                          <td className="px-2 py-2"></td>
                          <td className="px-2 py-2"></td>
                          <td className="px-2 py-2">
                            <div className="flex items-center gap-2">
                              <span className="text-zinc-800 dark:text-zinc-100 text-xs font-bold italic">
                                Unique Buckets
                              </span>
                              <span className="text-[10px] font-semibold text-zinc-500 dark:text-zinc-400 px-1.5 py-0.5 rounded bg-zinc-200 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/40">
                                {singleListLists.length}
                              </span>
                            </div>
                          </td>
                          <td className="px-2 py-2">
                            <div className="flex items-center gap-1 relative group/usenders">
                              {uniqSenders.slice(0, 2).map((s) => <SenderBadge key={s.name} name={s.name} color={s.color} />)}
                              {uniqSenders.length > 2 && (
                                <span className="text-[9px] text-zinc-500 font-semibold cursor-default">+{uniqSenders.length - 2}</span>
                              )}
                              {uniqSenders.length > 2 && (
                                <div className="hidden group-hover/usenders:flex absolute left-0 top-full mt-1 z-50 bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-lg shadow-lg p-1.5 gap-1 flex-wrap min-w-[120px]">
                                  {uniqSenders.map((s) => <SenderBadge key={s.name} name={s.name} color={s.color} />)}
                                </div>
                              )}
                            </div>
                          </td>
                          <td className="px-2 py-2 text-right">
                            {sumSize > 0 ? (() => {
                              const linkable = singleListLists.filter((l) => l.listVolumeRaw > 0);
                              const href = linkable.length > 0
                                ? `/contacts/group?ids=${linkable.map((l) => l.id).join(",")}&tab=all`
                                : null;
                              return href ? (
                                <a
                                  href={href}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  onClick={(e) => e.stopPropagation()}
                                  title={`Open combined view of ${linkable.length} list${linkable.length === 1 ? "" : "s"}`}
                                  className="font-mono font-bold text-zinc-800 dark:text-zinc-100 hover:text-violet-500 dark:hover:text-violet-400 underline underline-offset-2 decoration-zinc-400/40 hover:decoration-violet-400/50 transition-colors"
                                >
                                  {sumSize.toLocaleString()}
                                </a>
                              ) : (
                                <span className="font-mono font-bold text-zinc-800 dark:text-zinc-100">{sumSize.toLocaleString()}</span>
                              );
                            })() : ""}
                          </td>
                          <td className="px-2 py-2 text-right">
                            {sumRemain > 0 ? (() => {
                              const linkable = singleListLists.filter((l) => l.listVolumeRaw > 0);
                              const href = linkable.length > 0
                                ? `/contacts/group?ids=${linkable.map((l) => l.id).join(",")}&tab=assigned`
                                : null;
                              return href ? (
                                <a
                                  href={href}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  onClick={(e) => e.stopPropagation()}
                                  title={`Open combined view of ${linkable.length} list${linkable.length === 1 ? "" : "s"}`}
                                  className="font-mono font-bold text-violet-400 hover:text-violet-300 underline underline-offset-2 decoration-violet-500/30 hover:decoration-violet-400/50 transition-colors"
                                >
                                  {sumRemain.toLocaleString()}
                                </a>
                              ) : (
                                <span className="font-mono font-bold text-violet-400">{sumRemain.toLocaleString()}</span>
                              );
                            })() : ""}
                          </td>
                          <td className="px-2 py-2"></td>
                          <td className="px-2 py-2"></td>
                          <td className="px-2 py-2 text-right font-mono font-bold text-emerald-400">
                            {sumAccts > 0 ? sumAccts : ""}
                          </td>
                          <td className="px-2 py-2"></td>
                          <td className="px-2 py-2"></td>
                          <td className="px-2 py-2"></td>
                        </tr>
                      );
                      if (!collapsed) singleListLists.forEach((l) => nodes.push(renderRow(l)));
                    }

                    for (const l of unbucketedLists) nodes.push(renderRow(l));
                    for (const l of specialLists) nodes.push(renderRow(l));
                    return nodes;
                  })()}
                </tbody>
              );
            })}
        </table>
      </div>}

      {/* ── Bulk action bar ─────────────────────────────────────────── */}
      {selectedCount > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-xl shadow-2xl shadow-black/50 px-5 py-3 flex items-center gap-4">
          <span className="text-sm text-zinc-800 dark:text-zinc-300 font-medium">{selectedCount} list{selectedCount > 1 ? "s" : ""} selected</span>
          <div className="w-px h-5 bg-zinc-200 dark:bg-zinc-700" />
          <button onClick={openCopyModal} className="px-4 py-1.5 bg-violet-600 hover:bg-violet-500 text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 20h9M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
            Generate Copies
          </button>
          <button onClick={handleBulkDelete} className="px-4 py-1.5 bg-red-600 hover:bg-red-500 text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
            Delete
          </button>
          <button onClick={() => setSelectedIds(new Set())} className="text-xs text-zinc-600 dark:text-zinc-400 hover:text-zinc-800 dark:text-zinc-200 transition-colors">Clear</button>
        </div>
      )}

      {/* ── Copy generation modal ──────────────────────────────────── */}
      {showCopyModal && (
        <div className="fixed inset-0 z-[60] bg-black/60 backdrop-blur-sm flex items-start justify-center pt-12 overflow-y-auto">
          <div className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-2xl shadow-2xl max-w-4xl w-full mx-4 mb-12">
            {/* Modal header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800/40">
              <div>
                <h2 className="text-lg font-bold text-zinc-900 dark:text-zinc-100">Generate Copies</h2>
                <p className="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">{copyModalLists.length} list{copyModalLists.length > 1 ? "s" : ""} · title + description variants per bucket</p>
              </div>
              <div className="flex items-center gap-3">
                {!copyModalLists[0]?.copiesGenerated && (
                  <button onClick={handleGenerateCopies} disabled={generatingCopies}
                    className="px-5 py-2 bg-violet-600 hover:bg-violet-500 disabled:bg-violet-800 text-white text-sm font-semibold rounded-lg transition-colors flex items-center gap-2">
                    {generatingCopies ? (
                      <><div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" /> Generating...</>
                    ) : (
                      <><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg> Generate All</>
                    )}
                  </button>
                )}
                <button onClick={closeCopyModal} className="text-zinc-600 dark:text-zinc-400 hover:text-zinc-800 dark:text-zinc-200 transition-colors p-1">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
                </button>
              </div>
            </div>

            {/* Modal body */}
            <div className="px-6 py-4 max-h-[70vh] overflow-y-auto space-y-6">
              {copyModalLists.map((l) => {
                const lWebinar = webinars.find(w => w.id === l.webinarId);
                const lRegLink = lWebinar?.registrationLink || "";
                const lUnsubLink = lWebinar?.unsubscribeLink || "";
                return (
                <div key={l.id} className="rounded-xl border border-zinc-200 dark:border-zinc-800/60 overflow-hidden">
                  {/* List header */}
                  <div className="bg-zinc-100 dark:bg-zinc-800/30 px-4 py-3 flex items-center justify-between">
                    <div>
                      <span className="text-sm text-zinc-800 dark:text-zinc-200 font-medium">{l.description}</span>
                      <div className="flex items-center gap-2 mt-1">
                        <span className="text-[10px] text-zinc-500 bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 rounded border border-zinc-300 dark:border-zinc-700/30">{l.bucket}</span>
                        <SenderBadge name={l.sender} color={l.senderColor} />
                        <span className="text-[10px] text-zinc-500 font-mono">{l.listSize.toLocaleString()} contacts</span>
                      </div>
                    </div>
                    {l.copiesGenerated && <span className="text-[10px] font-semibold text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 px-2 py-0.5 rounded-full">Generated</span>}
                  </div>

                  {l.copiesGenerated && l.titleVariants && l.descVariants && (
                    <div className="px-4 py-4 space-y-4">
                      {/* Titles */}
                      <div>
                        <span className="text-[10px] font-semibold text-zinc-600 dark:text-zinc-400 uppercase tracking-wider block mb-2">Title Variants</span>
                        <div className="space-y-2">
                          {l.titleVariants.map((v, i) => (
                            <label key={v.id} onClick={() => selectVariant(l.id, "title", v.id)}
                              className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-all group ${
                                v.selected ? "border-violet-500/40 bg-violet-500/5" : "border-zinc-200 dark:border-zinc-800/40 hover:border-zinc-300 dark:border-zinc-700/60"
                              }`}>
                              <div className={`w-4 h-4 mt-0.5 rounded-full border-2 flex items-center justify-center shrink-0 ${
                                v.selected ? "border-violet-500 bg-violet-500" : "border-zinc-600"
                              }`}>
                                {v.selected && <div className="w-1.5 h-1.5 bg-white rounded-full" />}
                              </div>
                              <div className="flex-1 min-w-0">
                                <span className="text-[10px] text-zinc-500 font-semibold uppercase">Variant {String.fromCharCode(65 + i)}</span>
                                <p className="text-sm text-zinc-800 dark:text-zinc-200 mt-0.5 leading-relaxed">{linkifyCopyText(v.text, lRegLink, lUnsubLink)}</p>
                              </div>
                              {l.titleVariants!.length > 1 && (
                                <button onClick={(e) => { e.preventDefault(); e.stopPropagation(); if (confirm("Delete this title variant?")) deleteVariant(l.id, "title", v.id); }}
                                  className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-red-100 dark:hover:bg-red-500/10 text-zinc-400 hover:text-red-500 transition-all shrink-0 mt-0.5">
                                  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
                                </button>
                              )}
                            </label>
                          ))}
                        </div>
                      </div>

                      {/* Descriptions */}
                      <div>
                        <span className="text-[10px] font-semibold text-zinc-600 dark:text-zinc-400 uppercase tracking-wider block mb-2">Description Variants</span>
                        <div className="space-y-2">
                          {l.descVariants.map((v, i) => (
                            <label key={v.id} onClick={() => selectVariant(l.id, "desc", v.id)}
                              className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-all group ${
                                v.selected ? "border-violet-500/40 bg-violet-500/5" : "border-zinc-200 dark:border-zinc-800/40 hover:border-zinc-300 dark:border-zinc-700/60"
                              }`}>
                              <div className={`w-4 h-4 mt-0.5 rounded-full border-2 flex items-center justify-center shrink-0 ${
                                v.selected ? "border-violet-500 bg-violet-500" : "border-zinc-600"
                              }`}>
                                {v.selected && <div className="w-1.5 h-1.5 bg-white rounded-full" />}
                              </div>
                              <div className="flex-1 min-w-0">
                                <span className="text-[10px] text-zinc-500 font-semibold uppercase">Variant {String.fromCharCode(65 + i)}</span>
                                <p className="text-sm text-zinc-800 dark:text-zinc-300 mt-0.5 leading-relaxed">{linkifyCopyText(v.text, lRegLink, lUnsubLink)}</p>
                              </div>
                              {l.descVariants!.length > 1 && (
                                <button onClick={(e) => { e.preventDefault(); e.stopPropagation(); if (confirm("Delete this description variant?")) deleteVariant(l.id, "desc", v.id); }}
                                  className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-red-100 dark:hover:bg-red-500/10 text-zinc-400 hover:text-red-500 transition-all shrink-0 mt-0.5">
                                  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
                                </button>
                              )}
                            </label>
                          ))}
                        </div>
                      </div>
                    </div>
                  )}

                  {!l.copiesGenerated && (
                    <div className="px-4 py-8 text-center">
                      <p className="text-sm text-zinc-500">Click &quot;Generate All&quot; to create title and description variants</p>
                    </div>
                  )}
                </div>
                );
              })}
            </div>

            {/* Modal footer */}
            {copyModalLists.some((l) => l.copiesGenerated) && (
              <div className="px-6 py-4 border-t border-zinc-200 dark:border-zinc-800/40 flex justify-end gap-3">
                <button onClick={closeCopyModal} className="px-4 py-2 text-sm text-zinc-600 dark:text-zinc-400 hover:text-zinc-800 dark:text-zinc-200 border border-zinc-300 dark:border-zinc-700/60 rounded-lg hover:bg-zinc-100 dark:bg-zinc-800/50 transition-colors">Cancel</button>
                <button onClick={closeCopyModal} className="px-5 py-2 bg-violet-600 hover:bg-violet-500 text-white text-sm font-semibold rounded-lg transition-colors">
                  Apply Selected Variants
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Planning Copy Variant Modal (shared with Copy Generator) ── */}
      {planningCopyModal && modalBucketData && (() => {
        const modalWebinar = webinars.find(w => w.id === planningCopyModal.webinarId);
        const targetList = webinars.flatMap(w => w.lists).find(l => l.id === planningCopyModal.listId);
        if (!targetList) return null;
        const assignedTitleId = targetList.titleVariants?.find(v => v.selected)?.id;
        const assignedDescId = targetList.descVariants?.find(v => v.selected)?.id;
        const titles: CopyVariant[] = modalBucketData.titles.map(c => ({
          ...apiCopyToVariant(c),
          isAssigned: c.id === assignedTitleId,
        }));
        const descriptions: CopyVariant[] = modalBucketData.descriptions.map(c => ({
          ...apiCopyToVariant(c),
          isAssigned: c.id === assignedDescId,
        }));
        return (
          <VariationsModal
            bucket={modalBucketData.bucket}
            initialTab={planningCopyModal.tab}
            titles={titles}
            descriptions={descriptions}
            contextLabel={`List: ${targetList.bucket} · ${targetList.sender}`}
            registrationLink={modalWebinar?.registrationLink || ""}
            unsubscribeLink={modalWebinar?.unsubscribeLink || ""}
            onClose={closeVariationsModal}
            onUpdateVariant={handleModalUpdateVariant}
            onSetPrimary={handleModalSetPrimary}
            onRegenerate={handleModalRegenerate}
            onAddVariant={handleModalAddVariant}
            onDeleteVariant={handleModalDeleteVariant}
            onPickForList={handleModalPickForList}
          />
        );
      })()}

      {/* ── Release Unused Contacts Modal ─────────────────────────── */}
      {releaseModalWebinar && (
        <ReleaseContactsModal
          webinarId={releaseModalWebinar.id}
          webinarNumber={releaseModalWebinar.number}
          onClose={() => setReleaseModalWebinar(null)}
          onReleased={() => refreshAfterRelease(releaseModalWebinar.id)}
        />
      )}

      {/* ── New Webinar Modal ──────────────────────────────────────── */}
      {showNewWebinarModal && (
        <div className="fixed inset-0 z-[60] bg-black/60 backdrop-blur-sm flex items-center justify-center" onClick={(e) => { if (e.target === e.currentTarget) setShowNewWebinarModal(false); }}>
          <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800/60 rounded-2xl shadow-2xl w-full max-w-md overflow-hidden">
            {/* Header */}
            <div className="px-6 py-4 border-b border-zinc-200 dark:border-zinc-800/40 flex items-center justify-between">
              <div>
                <h3 className="text-base font-bold text-zinc-900 dark:text-zinc-100">New Webinar</h3>
                <p className="text-[11px] text-zinc-500 mt-0.5">Create a new webinar campaign</p>
              </div>
              <button onClick={() => setShowNewWebinarModal(false)} className="p-1.5 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors">
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>

            {/* Body */}
            <div className="px-6 py-5 space-y-4">
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5">Webinar Number</label>
                <input
                  type="number"
                  value={newWebinarNumber}
                  onChange={(e) => setNewWebinarNumber(parseInt(e.target.value) || 0)}
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 font-mono focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors"
                  autoFocus
                />
              </div>
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5">Webinar Date</label>
                <input
                  type="date"
                  value={newWebinarDate}
                  onChange={(e) => setNewWebinarDate(e.target.value)}
                  onClick={(e) => (e.target as HTMLInputElement).showPicker?.()}
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors [color-scheme:dark] cursor-pointer [&::-webkit-calendar-picker-indicator]:opacity-70 [&::-webkit-calendar-picker-indicator]:invert [&::-webkit-calendar-picker-indicator]:cursor-pointer"
                />
              </div>
              {/* Variant label — required when this number already exists */}
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5 flex items-center gap-2">
                  Variant Label
                  {siblingsForNewNumber.length > 0 && (
                    <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-500/15 text-amber-500 border border-amber-500/30 normal-case">
                      Required — W{newWebinarNumber} already exists
                    </span>
                  )}
                  <span className="text-zinc-500 normal-case font-normal">(optional for a single webinar; required for A/B variants)</span>
                </label>
                <input
                  type="text"
                  value={newWebinarVariantLabel}
                  onChange={(e) => setNewWebinarVariantLabel(e.target.value)}
                  placeholder='e.g. "Account A" / "WG-Skarpe"'
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors"
                />
                {siblingsForNewNumber.length > 0 && (
                  <div className="mt-1.5 text-[10px] text-zinc-500">
                    Existing variant{siblingsForNewNumber.length === 1 ? "" : "s"} for W{newWebinarNumber}: {" "}
                    {siblingsForNewNumber.map((s) => s.variantLabel ?? "(no label)").join(", ")}
                  </div>
                )}
              </div>
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5">WebinarGeek Account</label>
                <select
                  value={newWebinarWgCredentialId}
                  onChange={(e) => setNewWebinarWgCredentialId(e.target.value)}
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors"
                >
                  <option value="">Default credential</option>
                  {wgCredentials
                    .filter((c) => c.name !== "default")
                    .map((c) => (
                      <option key={c.id} value={c.id}>{c.name}</option>
                    ))}
                </select>
                <div className="mt-1.5 text-[10px] text-zinc-500">
                  Pick a different account when this variant runs on a separate WebinarGeek workspace.
                  Manage accounts in the Connectors page.
                </div>
              </div>
              {/* Preview */}
              {newWebinarNumber > 0 && newWebinarDate && (
                <div className="bg-zinc-50 dark:bg-zinc-800/40 border border-zinc-200 dark:border-zinc-700/30 rounded-lg px-4 py-3">
                  <div className="flex items-center gap-3">
                    <span className="text-lg font-bold text-zinc-900 dark:text-zinc-100">W{newWebinarNumber}</span>
                    <span className="text-zinc-300 dark:text-zinc-600">·</span>
                    <span className="text-sm text-zinc-600 dark:text-zinc-400">
                      {new Date(newWebinarDate + "T00:00:00").toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" })}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 mt-1.5">
                    <StatusBadge status="Planning" />
                    <span className="text-[10px] text-zinc-500">0 lists assigned</span>
                  </div>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="px-6 py-4 border-t border-zinc-200 dark:border-zinc-800/40 flex items-center justify-between">
              <button onClick={() => setShowNewWebinarModal(false)} className="px-4 py-2 text-sm text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 transition-colors">
                Cancel
              </button>
              <button
                onClick={handleCreateWebinar}
                disabled={!newWebinarNumber || !newWebinarDate}
                className="px-5 py-2 bg-violet-600 hover:bg-violet-500 disabled:bg-zinc-300 dark:disabled:bg-zinc-700 disabled:text-zinc-500 text-white text-sm font-semibold rounded-lg transition-colors flex items-center gap-1.5"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>
                Create Webinar
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Edit Webinar Modal ─────────────────────────────────────── */}
      {editWebinar && (
        <div className="fixed inset-0 z-[60] bg-black/60 backdrop-blur-sm flex items-center justify-center" onClick={(e) => { if (e.target === e.currentTarget) setEditWebinar(null); }}>
          <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800/60 rounded-2xl shadow-2xl w-full max-w-md overflow-hidden">
            {/* Header */}
            <div className="px-6 py-4 border-b border-zinc-200 dark:border-zinc-800/40 flex items-center justify-between">
              <h3 className="text-base font-bold text-zinc-900 dark:text-zinc-100">Edit Webinar</h3>
              <button onClick={() => setEditWebinar(null)} className="p-1.5 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors">
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>

            {/* Body */}
            <div className="px-6 py-5 space-y-4">
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5">Webinar Number</label>
                <input type="number" value={editWebinar.number}
                  onChange={(e) => setEditWebinar({ ...editWebinar, number: parseInt(e.target.value) || 0 })}
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 font-mono focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors"
                  autoFocus />
              </div>
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5">Webinar Date</label>
                <input type="date" value={editWebinar.date}
                  onChange={(e) => setEditWebinar({ ...editWebinar, date: e.target.value })}
                  onClick={(e) => (e.target as HTMLInputElement).showPicker?.()}
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors [color-scheme:dark] cursor-pointer [&::-webkit-calendar-picker-indicator]:opacity-70 [&::-webkit-calendar-picker-indicator]:invert [&::-webkit-calendar-picker-indicator]:cursor-pointer" />
              </div>
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5 flex items-center gap-2">
                  Variant Label
                  <span className="text-zinc-500 normal-case font-normal">(optional for a single webinar; required for A/B variants)</span>
                </label>
                <input type="text" value={editWebinar.variantLabel}
                  onChange={(e) => setEditWebinar({ ...editWebinar, variantLabel: e.target.value })}
                  placeholder='e.g. "Account A" / "WG-Skarpe"'
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors" />
              </div>
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5">WebinarGeek Account</label>
                <select
                  value={editWebinar.webinargeekCredentialId}
                  onChange={(e) => {
                    const credId = e.target.value;
                    setEditWebinar({ ...editWebinar, webinargeekCredentialId: credId, broadcastId: "" });
                    const filterId = credId || wgCredentials.find((c) => c.name === "default")?.id;
                    loadEditBroadcasts(filterId, false);
                  }}
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors"
                >
                  <option value="">Default credential</option>
                  {wgCredentials.filter((c) => c.name !== "default").map((c) => (
                    <option key={c.id} value={c.id}>{c.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium mb-1.5 flex items-center gap-2">
                  WebinarGeek Broadcast
                  {editBroadcastsLoading && <span className="text-zinc-500 normal-case font-normal tracking-normal">loading…</span>}
                </label>
                <select
                  value={editWebinar.broadcastId}
                  disabled={editBroadcastsLoading}
                  onChange={(e) => {
                    const bid = e.target.value;
                    const b = editBroadcasts.find((x) => x.broadcast_id === bid);
                    setEditWebinar({
                      ...editWebinar,
                      broadcastId: bid,
                      // Auto-fill the date from the broadcast start (still editable above).
                      date: b?.starts_at ? new Date(b.starts_at).toISOString().slice(0, 10) : editWebinar.date,
                    });
                  }}
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors disabled:opacity-60"
                >
                  <option value="">— None —</option>
                  {editWebinar.broadcastId && !editBroadcasts.some((b) => b.broadcast_id === editWebinar.broadcastId) && (
                    <option value={editWebinar.broadcastId}>Current · {editWebinar.broadcastId}</option>
                  )}
                  {editBroadcasts.map((b) => (
                    <option key={b.broadcast_id} value={b.broadcast_id}>
                      {(b.internal_title || b.name || `Broadcast ${b.broadcast_id}`)}{b.starts_at ? ` · ${new Date(b.starts_at).toLocaleDateString()}` : ""}
                    </option>
                  ))}
                </select>
                <div className="mt-1.5 text-[10px] text-zinc-500">
                  Subscribers auto-sync once, ~2h after the broadcast start time. Picking a broadcast fills the date above (still editable).
                </div>
              </div>
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5">Registration Link</label>
                <input type="url" value={editWebinar.registrationLink}
                  onChange={(e) => setEditWebinar({ ...editWebinar, registrationLink: e.target.value })}
                  placeholder="https://..."
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors" />
              </div>
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5">Unsubscribe Link</label>
                <input type="url" value={editWebinar.unsubscribeLink}
                  onChange={(e) => setEditWebinar({ ...editWebinar, unsubscribeLink: e.target.value })}
                  placeholder="https://..."
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors" />
              </div>
              <div>
                <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5">Status</label>
                <select value={editWebinar.status}
                  onChange={(e) => setEditWebinar({ ...editWebinar, status: e.target.value })}
                  className="w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors">
                  <option value="planning">Planning</option>
                  <option value="sent">Sent</option>
                  <option value="archived">Archived</option>
                </select>
              </div>
            </div>

            {/* Footer */}
            <div className="px-6 py-4 border-t border-zinc-200 dark:border-zinc-800/40 flex items-center justify-between">
              <button onClick={() => setEditWebinar(null)} className="px-4 py-2 text-sm text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 transition-colors">
                Cancel
              </button>
              <button
                onClick={async () => {
                  const ew = editWebinar;
                  const w = webinars.find((w) => w.id === ew.id);
                  if (!w) return;
                  // Apply all changes
                  if (ew.number !== w.number) await handleUpdateWebinar(ew.id, "number", ew.number);
                  if (ew.date && ew.date !== w.isoDate) {
                    await handleUpdateWebinar(ew.id, "date", ew.date);
                  }
                  const currentBroadcast = w.broadcastId === "—" ? "" : w.broadcastId;
                  if (ew.broadcastId !== currentBroadcast) await handleUpdateWebinar(ew.id, "broadcast_id", ew.broadcastId);
                  if (ew.webinargeekCredentialId !== (w.webinargeekCredentialId ?? "")) {
                    await handleUpdateWebinar(ew.id, "webinargeek_credential_id", ew.webinargeekCredentialId || null);
                  }
                  if (ew.registrationLink !== w.registrationLink) await handleUpdateWebinar(ew.id, "registration_link", ew.registrationLink);
                  if (ew.unsubscribeLink !== w.unsubscribeLink) await handleUpdateWebinar(ew.id, "unsubscribe_link", ew.unsubscribeLink);
                  if (ew.status !== w.status.toLowerCase()) await handleUpdateWebinar(ew.id, "status", ew.status);
                  const trimmedLabel = ew.variantLabel.trim();
                  if (trimmedLabel !== (w.variantLabel ?? "")) {
                    await handleUpdateWebinar(ew.id, "variant_label", trimmedLabel || null);
                  }
                  setEditWebinar(null);
                }}
                disabled={!editWebinar.number || !editWebinar.date}
                className="px-5 py-2 bg-violet-600 hover:bg-violet-500 disabled:bg-zinc-300 dark:disabled:bg-zinc-700 disabled:text-zinc-500 text-white text-sm font-semibold rounded-lg transition-colors"
              >
                Save Changes
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
