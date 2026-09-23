"use client";

import { useState, useRef, useEffect, useCallback, type ReactNode } from "react";
import { fetchCopyUsage, type ApiBucket, type ApiCopy, type ApiCopyUsage } from "@/lib/api";

export interface CopyVariant {
  id: string;
  text: string;
  isPrimary: boolean;
  isAssigned?: boolean;
  /** Short user-set label to tell variants apart (descriptions). */
  internalName?: string | null;
  /** Number of webinar lists currently using this copy. */
  timesUsed?: number;
  /** Set when the variant is archived (soft-deleted). */
  deletedAt?: string | null;
}

export function apiCopyToVariant(c: ApiCopy): CopyVariant {
  return {
    id: c.id,
    text: c.text,
    isPrimary: c.is_primary,
    isAssigned: c.is_assigned,
    internalName: c.internal_name ?? null,
    timesUsed: c.times_used,
    deletedAt: c.deleted_at ?? null,
  };
}

/**
 * Convert clipboard HTML (Google Docs, Word, web pages) to plain text that
 * keeps the visual structure: blank lines between paragraphs, one line per
 * list item with a "1." / "-" marker, <br> as line breaks. Plain textareas
 * only receive the source's text/plain by default, and Google Docs separates
 * paragraphs there with a single newline — which collapses all spacing.
 */
function htmlToPlainText(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  let out = "";

  // Append a paragraph ("\n\n") or line ("\n") break — but never right after
  // a freshly written list marker, and without stacking onto existing breaks.
  const ensureBreak = (sep: "\n" | "\n\n") => {
    if (!out) return;
    if (/(^|\n)(\d+\. |- )$/.test(out)) return;
    out = out.replace(/[ \t]+$/, "");
    const trailing = out.match(/\n*$/)?.[0].length ?? 0;
    if (sep.length > trailing) out += "\n".repeat(sep.length - trailing);
  };

  const walk = (node: Node, ctx: { ol: { n: number } | null; inLi: boolean }) => {
    if (node.nodeType === Node.TEXT_NODE) {
      out += (node.textContent || "").replace(/\s+/g, " ");
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as HTMLElement;
    const tag = el.tagName.toLowerCase();
    if (tag === "style" || tag === "script" || tag === "meta" || tag === "title" || tag === "head") return;
    if (tag === "br") { out += "\n"; return; }
    if (tag === "ol" || tag === "ul") {
      ensureBreak(ctx.inLi ? "\n" : "\n\n");
      const listCtx = { ol: tag === "ol" ? { n: 0 } : null, inLi: ctx.inLi };
      el.childNodes.forEach((c) => walk(c, listCtx));
      ensureBreak(ctx.inLi ? "\n" : "\n\n");
      return;
    }
    if (tag === "li") {
      ensureBreak("\n");
      if (ctx.ol) { ctx.ol.n += 1; out += `${ctx.ol.n}. `; }
      else out += "- ";
      el.childNodes.forEach((c) => walk(c, { ol: ctx.ol, inLi: true }));
      ensureBreak("\n");
      return;
    }
    const para = tag === "p" || /^h[1-6]$/.test(tag);
    const block = para || ["div", "section", "article", "blockquote", "pre", "table", "tr", "header", "footer"].includes(tag);
    // Inside a list item everything stays on the item's line(s).
    const sep: "\n" | "\n\n" = para && !ctx.inLi ? "\n\n" : "\n";
    if (block) ensureBreak(sep);
    el.childNodes.forEach((c) => walk(c, ctx));
    if (block) ensureBreak(sep);
  };

  doc.body.childNodes.forEach((c) => walk(c, { ol: null, inLi: false }));
  return out
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/**
 * Paste handler for the variant textareas: when the clipboard carries HTML,
 * insert its structured plain-text conversion at the caret instead of the
 * (formatting-collapsed) default text/plain payload.
 */
function handleRichPaste(
  e: React.ClipboardEvent<HTMLTextAreaElement>,
  setValue: (next: string) => void,
) {
  const html = e.clipboardData?.getData("text/html");
  if (!html) return;
  const text = htmlToPlainText(html);
  if (!text) return;
  e.preventDefault();
  const ta = e.currentTarget;
  const start = ta.selectionStart ?? ta.value.length;
  const end = ta.selectionEnd ?? start;
  setValue(ta.value.slice(0, start) + text + ta.value.slice(end));
  const caret = start + text.length;
  requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = caret; });
}

function LoadingSpinner() {
  return (
    <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

/**
 * Render copy text with "Register" / "Unsubscribe" keywords hyperlinked
 * (case-insensitive). If no links are provided, returns the original text.
 */
function linkifyCopyText(
  text: string,
  registrationLink: string,
  unsubscribeLink: string,
): ReactNode {
  if (!registrationLink && !unsubscribeLink) return text;
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
          onClick={(e) => e.stopPropagation()}
        >{part}</a>
      );
    }
    if (lower === "unsubscribe" && unsubscribeLink) {
      return (
        <a key={i} href={unsubscribeLink} target="_blank" rel="noopener noreferrer"
          className="text-zinc-500 hover:text-zinc-400 underline underline-offset-2"
          onClick={(e) => e.stopPropagation()}
        >{part}</a>
      );
    }
    return part;
  });
}

/**
 * Build an HTML string version of copy text with hyperlinks preserved,
 * so pasting into rich-text editors keeps the links.
 */
function linkifyToHtml(
  text: string,
  registrationLink: string,
  unsubscribeLink: string,
): string {
  let html = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  html = html.replace(/\n/g, "<br>");
  const patterns: string[] = [];
  if (registrationLink) patterns.push("register");
  if (unsubscribeLink) patterns.push("unsubscribe");
  if (patterns.length > 0) {
    const regex = new RegExp(`(${patterns.join("|")})`, "gi");
    html = html.replace(regex, (match) => {
      const lower = match.toLowerCase();
      if (lower === "register" && registrationLink) return `<a href="${registrationLink}">${match}</a>`;
      if (lower === "unsubscribe" && unsubscribeLink) return `<a href="${unsubscribeLink}">${match}</a>`;
      return match;
    });
  }
  return html;
}

export interface VariationsModalProps {
  bucket: ApiBucket;
  initialTab: "title" | "description";
  titles: CopyVariant[];
  descriptions: CopyVariant[];
  onClose: () => void;
  onUpdateVariant: (bucketId: string, type: "title" | "description", variantId: string, newText: string) => void;
  onSetPrimary: (bucketId: string, type: "title" | "description", variantId: string) => void;
  onRegenerate: (bucketId: string, type: "title" | "description", copyId: string, feedback: string) => Promise<void>;
  onAddVariant: (bucketId: string, type: "title" | "description", text: string) => Promise<void>;
  onDeleteVariant: (bucketId: string, type: "title" | "description", variantId: string) => Promise<void>;
  /** Optional: archived (deleted) variants, shown in the expandable Archive section. */
  archivedTitles?: CopyVariant[];
  archivedDescriptions?: CopyVariant[];
  /** Optional: restore an archived variant back to the active list. */
  onRestoreVariant?: (bucketId: string, type: "title" | "description", variantId: string) => Promise<void>;
  /** Optional: save a variant's short internal name (shown on descriptions). */
  onUpdateInternalName?: (bucketId: string, type: "title" | "description", variantId: string, name: string) => Promise<void>;
  /** Optional: planning-page mode. When provided, shows "Pick for this list" button. */
  onPickForList?: (bucketId: string, type: "title" | "description", variantId: string) => void;
  /** Optional: per-tab subtitle shown under bucket name (e.g. "List: Wealth Mgmt · Santi"). */
  contextLabel?: string;
  /** Optional: webinar registration URL — replaces "Register" keyword with a hyperlink. */
  registrationLink?: string;
  /** Optional: webinar unsubscribe URL — replaces "Unsubscribe" keyword with a hyperlink. */
  unsubscribeLink?: string;
}

export function VariationsModal({
  bucket,
  initialTab,
  titles,
  descriptions,
  onClose,
  onUpdateVariant,
  onSetPrimary,
  onRegenerate,
  onAddVariant,
  onDeleteVariant,
  archivedTitles,
  archivedDescriptions,
  onRestoreVariant,
  onUpdateInternalName,
  onPickForList,
  contextLabel,
  registrationLink = "",
  unsubscribeLink = "",
}: VariationsModalProps) {
  const [activeTab, setActiveTab] = useState<"title" | "description">(initialTab);
  const [feedback, setFeedback] = useState("");
  const [regenerating, setRegenerating] = useState(false);
  const [regeneratingIds, setRegeneratingIds] = useState<Set<string>>(new Set());
  const [selectedForRegen, setSelectedForRegen] = useState<Set<string>>(new Set());
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [addingManual, setAddingManual] = useState(false);
  const [manualText, setManualText] = useState("");
  const [generatingNew, setGeneratingNew] = useState(false);
  const [showArchive, setShowArchive] = useState(false);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [nameEditingId, setNameEditingId] = useState<string | null>(null);
  const [nameText, setNameText] = useState("");
  // Usage side panel: which variant's webinar list is open, and its data
  const [usageFor, setUsageFor] = useState<CopyVariant | null>(null);
  const [usageList, setUsageList] = useState<ApiCopyUsage[] | null>(null);
  const [usageError, setUsageError] = useState<string | null>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const editTextareaRef = useRef<HTMLTextAreaElement>(null);
  const manualTextareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const ta = editTextareaRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = `${ta.scrollHeight}px`;
    }
  }, [editText, editingId]);

  useEffect(() => {
    const ta = manualTextareaRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = `${ta.scrollHeight}px`;
      ta.focus();
    }
  }, [addingManual, manualText]);

  const variants = activeTab === "title" ? titles : descriptions;
  const archived = (activeTab === "title" ? archivedTitles : archivedDescriptions) ?? [];

  const switchTab = useCallback((tab: "title" | "description") => {
    setActiveTab(tab);
    setSelectedForRegen(new Set());
    setUsageFor(null);
    setNameEditingId(null);
  }, []);

  const openUsage = useCallback(async (v: CopyVariant) => {
    setUsageFor(v);
    setUsageList(null);
    setUsageError(null);
    try {
      const { usages } = await fetchCopyUsage(v.id);
      setUsageList(usages);
    } catch {
      setUsageError("Failed to load usage");
    }
  }, []);

  const handleSaveName = useCallback(async (variantId: string) => {
    if (!onUpdateInternalName) return;
    await onUpdateInternalName(bucket.id, activeTab, variantId, nameText.trim());
    setNameEditingId(null);
    setNameText("");
  }, [bucket.id, activeTab, nameText, onUpdateInternalName]);

  const handleRestore = useCallback(async (variantId: string) => {
    if (!onRestoreVariant) return;
    setRestoringId(variantId);
    try {
      await onRestoreVariant(bucket.id, activeTab, variantId);
    } finally {
      setRestoringId(null);
    }
  }, [bucket.id, activeTab, onRestoreVariant]);

  const toggleRegenSelect = useCallback((id: string) => {
    setSelectedForRegen(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const handleCopy = useCallback((id: string, text: string) => {
    // If links are provided, write both plain text and HTML (with hyperlinks)
    // so pasting into rich-text editors preserves the links.
    if ((registrationLink || unsubscribeLink) && typeof ClipboardItem !== "undefined") {
      try {
        const html = linkifyToHtml(text, registrationLink, unsubscribeLink);
        const item = new ClipboardItem({
          "text/plain": new Blob([text], { type: "text/plain" }),
          "text/html": new Blob([html], { type: "text/html" }),
        });
        navigator.clipboard.write([item]);
      } catch {
        navigator.clipboard.writeText(text);
      }
    } else {
      navigator.clipboard.writeText(text);
    }
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  }, [registrationLink, unsubscribeLink]);

  const handleStartEdit = useCallback((id: string, text: string) => {
    setEditingId(id);
    setEditText(text);
  }, []);

  const handleSaveEdit = useCallback((variantId: string) => {
    onUpdateVariant(bucket.id, activeTab, variantId, editText);
    setEditingId(null);
    setEditText("");
  }, [bucket.id, activeTab, editText, onUpdateVariant]);

  const handleRegenerate = useCallback(async () => {
    if (!feedback.trim()) return;
    const targetIds = selectedForRegen.size > 0 ? Array.from(selectedForRegen) : [];
    if (targetIds.length === 0) return;

    setRegenerating(true);
    setRegeneratingIds(new Set(targetIds));

    for (const copyId of targetIds) {
      await onRegenerate(bucket.id, activeTab, copyId, feedback);
      setRegeneratingIds(prev => {
        const next = new Set(prev);
        next.delete(copyId);
        return next;
      });
    }

    setFeedback("");
    setSelectedForRegen(new Set());
    setRegenerating(false);
    setRegeneratingIds(new Set());
  }, [feedback, activeTab, bucket, selectedForRegen, onRegenerate]);

  const handleGenerateNew = useCallback(async () => {
    if (!feedback.trim() || variants.length === 0) return;
    setGeneratingNew(true);
    const base = variants.find(v => v.isPrimary) || variants[0];
    await onRegenerate(bucket.id, activeTab, base.id, feedback);
    setFeedback("");
    setGeneratingNew(false);
  }, [feedback, activeTab, bucket, variants, onRegenerate]);

  const handleAddManual = useCallback(async () => {
    if (!manualText.trim()) return;
    await onAddVariant(bucket.id, activeTab, manualText.trim());
    setManualText("");
    setAddingManual(false);
  }, [manualText, activeTab, bucket, onAddVariant]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div
      ref={backdropRef}
      className="fixed inset-0 z-[100] flex items-center justify-center gap-4 bg-black/50 backdrop-blur-sm"
      onClick={(e) => { if (e.target === backdropRef.current) onClose(); }}
    >
      <div className="bg-white dark:bg-zinc-900 rounded-2xl border border-zinc-200 dark:border-zinc-800/60 shadow-2xl w-full max-w-4xl max-h-[85vh] flex flex-col overflow-hidden animate-in fade-in zoom-in-95 duration-200">

        {/* ── Header ──────────────────────────────────────────────── */}
        <div className="flex items-center gap-3 px-6 py-4 border-b border-zinc-200 dark:border-zinc-800/40 shrink-0">
          <div className="flex-1 min-w-0">
            <h2 className="text-base font-bold text-zinc-900 dark:text-zinc-100 truncate">{bucket.name}</h2>
            <div className="flex items-center gap-2 mt-0.5">
              {contextLabel ? (
                <span className="text-xs text-zinc-500">{contextLabel}</span>
              ) : (
                <>
                  <span className="text-xs text-zinc-500">{bucket.industry}</span>
                  <span className="text-zinc-300 dark:text-zinc-600">·</span>
                  <span className="text-xs text-zinc-500">{bucket.remaining_contacts.toLocaleString()} remaining</span>
                </>
              )}
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        {/* ── Tab Bar ─────────────────────────────────────────────── */}
        <div className="flex items-center gap-1 px-6 py-2.5 border-b border-zinc-200 dark:border-zinc-800/40 shrink-0">
          <button
            onClick={() => switchTab("title")}
            className={`px-4 py-1.5 rounded-lg text-xs font-semibold transition-all ${
              activeTab === "title"
                ? "bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400 border border-violet-200 dark:border-violet-500/25"
                : "text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800/50"
            }`}
          >
            Titles ({titles.length})
          </button>
          <button
            onClick={() => switchTab("description")}
            className={`px-4 py-1.5 rounded-lg text-xs font-semibold transition-all ${
              activeTab === "description"
                ? "bg-blue-100 dark:bg-blue-500/15 text-blue-600 dark:text-blue-400 border border-blue-200 dark:border-blue-500/25"
                : "text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800/50"
            }`}
          >
            Descriptions ({descriptions.length})
          </button>
        </div>

        {/* ── Variants List ───────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
          {variants.length === 0 ? (
            <div className="flex items-center justify-center py-16 text-zinc-400 text-sm">
              No {activeTab === "title" ? "titles" : "descriptions"} generated yet
            </div>
          ) : (
            variants.map((v, i) => {
              const isEditing = editingId === v.id;
              const isSelectedForRegen = selectedForRegen.has(v.id);
              const isRegenerating = regeneratingIds.has(v.id);
              return (
                <div
                  key={v.id}
                  className={`rounded-xl border transition-all ${
                    isSelectedForRegen
                      ? "border-amber-300 dark:border-amber-500/40 bg-amber-50/50 dark:bg-amber-500/5 ring-1 ring-amber-300/50 dark:ring-amber-500/20"
                      : v.isAssigned
                        ? "border-emerald-300 dark:border-emerald-500/40 bg-emerald-50/50 dark:bg-emerald-500/5 ring-1 ring-emerald-300/40 dark:ring-emerald-500/20"
                        : v.isPrimary
                          ? activeTab === "title"
                            ? "border-violet-300 dark:border-violet-500/30 bg-violet-50/50 dark:bg-violet-500/5"
                            : "border-blue-300 dark:border-blue-500/30 bg-blue-50/50 dark:bg-blue-500/5"
                          : "border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/60"
                  } ${isRegenerating ? "opacity-60" : ""}`}
                >
                  <div className="px-4 py-3">
                    <div className="flex items-center gap-2 mb-2">
                      <button
                        onClick={() => toggleRegenSelect(v.id)}
                        className={`shrink-0 w-5 h-5 flex items-center justify-center rounded text-[10px] font-bold transition-all ${
                          isSelectedForRegen
                            ? "bg-amber-400 dark:bg-amber-500 text-white ring-1 ring-amber-500/50"
                            : activeTab === "title"
                              ? "bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400 hover:bg-violet-200 dark:hover:bg-violet-500/25"
                              : "bg-blue-100 dark:bg-blue-500/15 text-blue-600 dark:text-blue-400 hover:bg-blue-200 dark:hover:bg-blue-500/25"
                        }`}
                        title={isSelectedForRegen ? "Deselect for regeneration" : "Select for regeneration"}
                      >
                        {isSelectedForRegen ? "✓" : i + 1}
                      </button>
                      {v.isPrimary && (
                        <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${
                          activeTab === "title"
                            ? "bg-violet-200/60 dark:bg-violet-500/20 text-violet-600 dark:text-violet-400"
                            : "bg-blue-200/60 dark:bg-blue-500/20 text-blue-600 dark:text-blue-400"
                        }`}>
                          Primary
                        </span>
                      )}
                      {v.isAssigned && (
                        <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-emerald-200/60 dark:bg-emerald-500/20 text-emerald-600 dark:text-emerald-400">
                          {onPickForList ? "Picked for list" : "Picked"}
                        </span>
                      )}
                      {activeTab === "description" && onUpdateInternalName && (
                        nameEditingId === v.id ? (
                          <input
                            value={nameText}
                            onChange={(e) => setNameText(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") { e.preventDefault(); handleSaveName(v.id); }
                              if (e.key === "Escape") { setNameEditingId(null); setNameText(""); }
                            }}
                            onBlur={() => handleSaveName(v.id)}
                            placeholder="Internal name…"
                            autoFocus
                            className="text-[10px] font-semibold px-1.5 py-0.5 w-32 rounded border border-blue-300 dark:border-blue-500/40 bg-white dark:bg-zinc-800 text-zinc-700 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-500/50"
                          />
                        ) : v.internalName ? (
                          <button
                            onClick={() => { setNameEditingId(v.id); setNameText(v.internalName || ""); }}
                            title="Edit internal name"
                            className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800 border border-zinc-200 dark:border-zinc-700/60 text-zinc-600 dark:text-zinc-300 hover:border-blue-300 dark:hover:border-blue-500/40 transition-colors max-w-[160px] truncate"
                          >
                            {v.internalName}
                          </button>
                        ) : (
                          <button
                            onClick={() => { setNameEditingId(v.id); setNameText(""); }}
                            title="Add a short internal name to this variant"
                            className="text-[10px] font-medium px-1.5 py-0.5 rounded text-zinc-400 hover:text-blue-500 hover:bg-blue-50 dark:hover:bg-blue-500/10 transition-colors"
                          >
                            + name
                          </button>
                        )
                      )}
                      {typeof v.timesUsed === "number" && v.timesUsed > 0 && (
                        <button
                          onClick={() => openUsage(v)}
                          title="Show the webinars where this was used"
                          className={`text-[10px] font-bold px-1.5 py-0.5 rounded-full transition-colors ${
                            usageFor?.id === v.id
                              ? "bg-violet-600 text-white"
                              : "bg-zinc-200 dark:bg-zinc-700/60 text-zinc-600 dark:text-zinc-300 hover:bg-violet-100 dark:hover:bg-violet-500/20 hover:text-violet-600 dark:hover:text-violet-400"
                          }`}
                        >
                          {v.timesUsed}×
                        </button>
                      )}
                      <span className="text-[10px] text-zinc-400 font-mono">{v.text.length} chars{activeTab === "description" ? ` · ${v.text.trim().split(/\s+/).length} words` : ""}</span>
                      <div className="flex-1" />

                      <div className="flex items-center gap-1">
                        {onPickForList && !v.isAssigned && (
                          <button
                            onClick={() => onPickForList(bucket.id, activeTab, v.id)}
                            className="text-[10px] font-medium px-2 py-1 rounded-md text-zinc-500 hover:text-emerald-500 hover:bg-emerald-50 dark:hover:bg-emerald-500/10 transition-colors"
                          >
                            Pick for list
                          </button>
                        )}
                        {!v.isPrimary && (
                          <button
                            onClick={() => onSetPrimary(bucket.id, activeTab, v.id)}
                            className="text-[10px] font-medium px-2 py-1 rounded-md text-zinc-500 hover:text-violet-500 hover:bg-violet-50 dark:hover:bg-violet-500/10 transition-colors"
                          >
                            Set Primary
                          </button>
                        )}
                        {!isEditing ? (
                          <button
                            onClick={() => handleStartEdit(v.id, v.text)}
                            className="text-[10px] font-medium px-2 py-1 rounded-md text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800/50 transition-colors"
                          >
                            Edit
                          </button>
                        ) : (
                          <button
                            onClick={() => handleSaveEdit(v.id)}
                            className="text-[10px] font-medium px-2 py-1 rounded-md text-emerald-500 hover:bg-emerald-50 dark:hover:bg-emerald-500/10 transition-colors"
                          >
                            Save
                          </button>
                        )}
                        <button
                          onClick={() => handleCopy(v.id, v.text)}
                          className={`text-[10px] font-medium px-2 py-1 rounded-md transition-colors ${
                            copiedId === v.id
                              ? "text-emerald-500 bg-emerald-50 dark:bg-emerald-500/10"
                              : "text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800/50"
                          }`}
                        >
                          {copiedId === v.id ? "Copied!" : "Copy"}
                        </button>
                        <button
                          onClick={() => {
                            if (confirm(`Delete this ${activeTab} variant? It will move to the Archive below.`))
                              onDeleteVariant(bucket.id, activeTab, v.id);
                          }}
                          className="text-[10px] font-medium px-2 py-1 rounded-md text-zinc-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-500/10 transition-colors"
                        >
                          Delete
                        </button>
                      </div>
                    </div>

                    {isRegenerating ? (
                      <div className="flex items-center gap-2 py-2 text-amber-500 text-xs font-medium">
                        <LoadingSpinner /> Regenerating this variant…
                      </div>
                    ) : isEditing ? (
                      <textarea
                        ref={editTextareaRef}
                        value={editText}
                        onPaste={(e) => handleRichPaste(e, setEditText)}
                        onChange={(e) => {
                          setEditText(e.target.value);
                          const ta = e.target;
                          ta.style.height = "auto";
                          ta.style.height = `${ta.scrollHeight}px`;
                        }}
                        className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 leading-relaxed focus:outline-none focus:ring-2 focus:ring-violet-500/50 resize-vertical min-h-[60px] font-sans whitespace-pre-wrap"
                        autoFocus
                        onKeyDown={(e) => { if (e.key === "Escape") { setEditingId(null); } }}
                      />
                    ) : (
                      <pre
                        className="text-sm text-zinc-700 dark:text-zinc-300 leading-relaxed cursor-text whitespace-pre-wrap font-sans"
                        onClick={() => handleStartEdit(v.id, v.text)}
                      >
                        {linkifyCopyText(v.text, registrationLink, unsubscribeLink)}
                      </pre>
                    )}
                  </div>
                </div>
              );
            })
          )}

          {/* ── Add Manual Variant ─────────────────────────────────── */}
          {addingManual ? (
            <div className="rounded-xl border border-dashed border-emerald-300 dark:border-emerald-500/30 bg-emerald-50/30 dark:bg-emerald-500/5 p-4">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-[10px] font-bold uppercase tracking-wider text-emerald-600 dark:text-emerald-400">
                  New manual variant
                </span>
              </div>
              <textarea
                ref={manualTextareaRef}
                value={manualText}
                onPaste={(e) => handleRichPaste(e, setManualText)}
                onChange={(e) => setManualText(e.target.value)}
                placeholder={`Type your ${activeTab} text here…`}
                className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 leading-relaxed focus:outline-none focus:ring-2 focus:ring-emerald-500/50 resize-vertical min-h-[60px] font-sans whitespace-pre-wrap"
                onKeyDown={(e) => {
                  if (e.key === "Escape") { setAddingManual(false); setManualText(""); }
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleAddManual(); }
                }}
              />
              <div className="flex items-center gap-2 mt-2">
                <button
                  onClick={handleAddManual}
                  disabled={!manualText.trim()}
                  className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-xs font-semibold rounded-lg transition-colors"
                >
                  Add variant
                </button>
                <button
                  onClick={() => { setAddingManual(false); setManualText(""); }}
                  className="px-3 py-1.5 text-xs font-medium text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 transition-colors"
                >
                  Cancel
                </button>
                <span className="text-[10px] text-zinc-400 ml-auto">Cmd+Enter to save</span>
              </div>
            </div>
          ) : (
            <button
              onClick={() => setAddingManual(true)}
              className="w-full rounded-xl border border-dashed border-zinc-300 dark:border-zinc-700/40 hover:border-zinc-400 dark:hover:border-zinc-600 py-3 text-xs font-medium text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors flex items-center justify-center gap-1.5"
            >
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" /></svg>
              Add variant manually
            </button>
          )}

          {/* ── Archive (deleted variants) ─────────────────────────── */}
          {archived.length > 0 && (
            <div className="pt-1">
              <button
                onClick={() => setShowArchive(s => !s)}
                className="w-full flex items-center gap-2 py-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors"
              >
                <svg className={`w-3 h-3 transition-transform ${showArchive ? "rotate-90" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
                Archive ({archived.length})
                <span className="flex-1 border-t border-dashed border-zinc-200 dark:border-zinc-800" />
              </button>
              {showArchive && (
                <div className="space-y-3 mt-1">
                  {archived.map((v) => (
                    <div key={v.id} className="rounded-xl border border-dashed border-zinc-300 dark:border-zinc-700/50 bg-zinc-50/60 dark:bg-zinc-800/20 px-4 py-3 opacity-75">
                      <div className="flex items-center gap-2 mb-2">
                        <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-zinc-200/70 dark:bg-zinc-700/40 text-zinc-500 dark:text-zinc-400">
                          Archived
                        </span>
                        {v.internalName && (
                          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800 border border-zinc-200 dark:border-zinc-700/60 text-zinc-500 dark:text-zinc-400 max-w-[160px] truncate">
                            {v.internalName}
                          </span>
                        )}
                        {typeof v.timesUsed === "number" && v.timesUsed > 0 && (
                          <button
                            onClick={() => openUsage(v)}
                            title="Show the webinars where this was used"
                            className={`text-[10px] font-bold px-1.5 py-0.5 rounded-full transition-colors ${
                              usageFor?.id === v.id
                                ? "bg-violet-600 text-white"
                                : "bg-zinc-200 dark:bg-zinc-700/60 text-zinc-600 dark:text-zinc-300 hover:bg-violet-100 dark:hover:bg-violet-500/20 hover:text-violet-600 dark:hover:text-violet-400"
                            }`}
                          >
                            {v.timesUsed}×
                          </button>
                        )}
                        {v.deletedAt && (
                          <span className="text-[10px] text-zinc-400">
                            deleted {new Date(v.deletedAt).toLocaleDateString()}
                          </span>
                        )}
                        <div className="flex-1" />
                        {onRestoreVariant && (
                          <button
                            onClick={() => handleRestore(v.id)}
                            disabled={restoringId === v.id}
                            className="text-[10px] font-medium px-2 py-1 rounded-md text-zinc-500 hover:text-emerald-500 hover:bg-emerald-50 dark:hover:bg-emerald-500/10 disabled:opacity-50 transition-colors"
                          >
                            {restoringId === v.id ? "Restoring…" : "Restore"}
                          </button>
                        )}
                      </div>
                      <pre className="text-sm text-zinc-500 dark:text-zinc-400 leading-relaxed whitespace-pre-wrap font-sans">
                        {linkifyCopyText(v.text, registrationLink, unsubscribeLink)}
                      </pre>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* ── AI Generator ─────────────────────────────────────────── */}
        <div className="shrink-0 border-t border-zinc-200 dark:border-zinc-800/40 px-6 py-4 bg-zinc-50/50 dark:bg-zinc-800/20">
          <div className="flex items-center gap-2 mb-2">
            <label className="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold">
              AI Copy Generator
            </label>
            {selectedForRegen.size > 0 ? (
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-200 dark:border-amber-500/25">
                {selectedForRegen.size} variant{selectedForRegen.size !== 1 ? "s" : ""} selected — will regenerate
              </span>
            ) : (
              <span className="text-[10px] text-zinc-400">
                describe what you want — generates a new variant, or select variants to regenerate them
              </span>
            )}
          </div>
          <div className="flex gap-2">
            <input
              type="text"
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  if (selectedForRegen.size > 0) handleRegenerate();
                  else handleGenerateNew();
                }
              }}
              placeholder={selectedForRegen.size > 0
                ? `Describe how to improve the ${selectedForRegen.size} selected variant${selectedForRegen.size !== 1 ? "s" : ""}…`
                : `e.g. "Make it about ROI", "Shorter and punchier", "Focus on AI angle"…`
              }
              className="flex-1 bg-white dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500 transition-colors"
              disabled={regenerating || generatingNew}
            />
            {selectedForRegen.size > 0 ? (
              <button
                onClick={handleRegenerate}
                disabled={!feedback.trim() || regenerating}
                className="px-4 py-2 bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-400 hover:to-orange-400 disabled:opacity-40 disabled:cursor-not-allowed text-white text-xs font-semibold rounded-lg transition-all flex items-center gap-1.5 shrink-0"
              >
                {regenerating && <LoadingSpinner />}
                {regenerating
                  ? `Regenerating${regeneratingIds.size > 0 ? ` (${regeneratingIds.size} left)` : ""}…`
                  : `Regenerate ${selectedForRegen.size} variant${selectedForRegen.size !== 1 ? "s" : ""}`
                }
              </button>
            ) : (
              <button
                onClick={handleGenerateNew}
                disabled={!feedback.trim() || generatingNew || variants.length === 0}
                className="px-4 py-2 bg-gradient-to-r from-violet-600 to-blue-600 hover:from-violet-500 hover:to-blue-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-xs font-semibold rounded-lg transition-all flex items-center gap-1.5 shrink-0"
              >
                {generatingNew && <LoadingSpinner />}
                {generatingNew ? "Generating…" : "Generate new variant"}
              </button>
            )}
          </div>
        </div>

        {/* ── Footer ──────────────────────────────────────────────── */}
        <div className="shrink-0 flex items-center justify-end px-6 py-3 border-t border-zinc-200 dark:border-zinc-800/40">
          <button
            onClick={onClose}
            className="px-5 py-2 bg-zinc-900 dark:bg-zinc-100 hover:bg-zinc-800 dark:hover:bg-zinc-200 text-white dark:text-zinc-900 text-xs font-semibold rounded-lg transition-colors"
          >
            Done
          </button>
        </div>
      </div>

      {/* ── Usage side panel ─────────────────────────────────────── */}
      {usageFor && (
        <div className="bg-white dark:bg-zinc-900 rounded-2xl border border-zinc-200 dark:border-zinc-800/60 shadow-2xl w-80 max-h-[85vh] flex flex-col overflow-hidden animate-in fade-in slide-in-from-right-4 duration-200 shrink-0">
          <div className="flex items-start gap-2 px-4 py-3 border-b border-zinc-200 dark:border-zinc-800/40 shrink-0">
            <div className="flex-1 min-w-0">
              <h3 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">Used in webinars</h3>
              <p className="text-[11px] text-zinc-500 mt-0.5 truncate" title={usageFor.text}>
                {usageFor.internalName || `${usageFor.text.slice(0, 60)}${usageFor.text.length > 60 ? "…" : ""}`}
              </p>
            </div>
            <button
              onClick={() => setUsageFor(null)}
              className="p-1 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
            </button>
          </div>
          <div className="flex-1 overflow-y-auto px-4 py-3">
            {usageError ? (
              <p className="text-xs text-red-500 py-4 text-center">{usageError}</p>
            ) : usageList === null ? (
              <div className="flex items-center justify-center gap-2 py-8 text-zinc-400 text-xs">
                <LoadingSpinner /> Loading…
              </div>
            ) : usageList.length === 0 ? (
              <p className="text-xs text-zinc-400 py-4 text-center">Not used on any webinar list.</p>
            ) : (
              <div className="space-y-2">
                {usageList.map((u) => (
                  <div key={`${u.assignment_id}`} className="rounded-lg border border-zinc-200 dark:border-zinc-800/40 px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-bold text-zinc-800 dark:text-zinc-200">
                        W{u.webinar.number}{u.webinar.variant_label ? ` · ${u.webinar.variant_label}` : ""}
                      </span>
                      <span className="text-[10px] text-zinc-400">{u.webinar.date}</span>
                      <span className={`ml-auto text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${
                        u.webinar.status === "sent"
                          ? "bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                          : u.webinar.status === "archived"
                            ? "bg-zinc-100 dark:bg-zinc-800 text-zinc-500"
                            : "bg-amber-100 dark:bg-amber-500/15 text-amber-600 dark:text-amber-400"
                      }`}>
                        {u.webinar.status}
                      </span>
                    </div>
                    {u.list_name && (
                      <p className="text-[11px] text-zinc-500 mt-1 truncate" title={u.list_name}>{u.list_name}</p>
                    )}
                    <p className="text-[10px] text-zinc-400 mt-0.5">as {u.used_as.join(" + ")}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
