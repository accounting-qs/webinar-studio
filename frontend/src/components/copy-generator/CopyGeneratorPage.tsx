"use client";

import React, { useState, useRef, useEffect, useCallback, useMemo } from "react";
import {
  fetchBuckets,
  generateCopiesBulk as apiGenerateCopiesBulk,
  fetchCopyGenerationStatus as apiFetchCopyGenStatus,
  retryCopyGenerationJob as apiRetryCopyGenJob,
  createCopy as apiCreateCopy,
  updateCopy as apiUpdateCopy,
  regenerateCopy as apiRegenerateCopy,
  deleteCopy as apiDeleteCopy,
  mergeBuckets as apiMergeBuckets,
  updateBucket as apiUpdateBucket,
  MergeBlockedError,
  type ApiBucket,
  type ApiCopy,
  type ApiCopyGenJob,
  type MergeBlockingBucket,
} from "@/lib/api";
import { BrainPanel } from "./BrainPanel";
import { VariationsModal, apiCopyToVariant, type CopyVariant } from "../shared/VariationsModal";

/* ─── Types ────────────────────────────────────────────────────────────── */

interface GeneratedCopy {
  bucketId: string;
  type: "title" | "description";
  variants: CopyVariant[];
  generatedAt: string;
}

type GenerationStatus = "idle" | "pending" | "generating" | "done" | "failed";

interface JobMeta {
  jobId: string;
  errorMessage?: string | null;
}

/* ─── Country Badge ────────────────────────────────────────────────────── */

function CountryBadge({ code }: { code: string }) {
  return (
    <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-zinc-100 dark:bg-zinc-800/60 text-zinc-500 dark:text-zinc-400 border border-zinc-200 dark:border-zinc-700/40">
      {code}
    </span>
  );
}

/* ─── Merge Modal ──────────────────────────────────────────────────────── */

function MergeBucketsModal({
  candidates,
  onClose,
  onConfirm,
}: {
  candidates: ApiBucket[];
  onClose: () => void;
  onConfirm: (keeperId: string) => Promise<void>;
}) {
  const [keeperId, setKeeperId] = useState<string>(candidates[0]?.id ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [blockingBuckets, setBlockingBuckets] = useState<MergeBlockingBucket[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const backdropRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  const keeper = candidates.find(b => b.id === keeperId);
  const sources = candidates.filter(b => b.id !== keeperId);
  const totalContactsToMove = sources.reduce((s, b) => s + (b.total_contacts || 0), 0);

  const handleConfirm = async () => {
    if (!keeperId) return;
    setSubmitting(true);
    setBlockingBuckets([]);
    setErrorMsg(null);
    try {
      await onConfirm(keeperId);
      onClose();
    } catch (err) {
      if (err instanceof MergeBlockedError) {
        setBlockingBuckets(err.blocking);
        setErrorMsg(err.message);
      } else {
        setErrorMsg(err instanceof Error ? err.message : "Merge failed");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      ref={backdropRef}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 backdrop-blur-sm"
      onClick={(e) => { if (e.target === backdropRef.current && !submitting) onClose(); }}
    >
      <div className="bg-white dark:bg-zinc-900 rounded-2xl border border-zinc-200 dark:border-zinc-800/60 shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden animate-in fade-in zoom-in-95 duration-200">
        {/* Header */}
        <div className="flex items-center gap-3 px-6 py-4 border-b border-zinc-200 dark:border-zinc-800/40 shrink-0">
          <div className="flex-1 min-w-0">
            <h2 className="text-base font-bold text-zinc-900 dark:text-zinc-100">Merge buckets</h2>
            <p className="text-xs text-zinc-500 mt-0.5">
              Choose which bucket to keep. All other contacts will move into it, and the other buckets will be deleted.
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={submitting}
            className="p-1.5 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors disabled:opacity-50"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        {/* Blocking error */}
        {blockingBuckets.length > 0 && (
          <div className="mx-6 mt-4 rounded-lg border border-red-300 dark:border-red-500/40 bg-red-50 dark:bg-red-500/10 px-4 py-3">
            <p className="text-xs font-semibold text-red-700 dark:text-red-400 mb-2">
              Can&apos;t merge — these buckets have webinar assignments:
            </p>
            <ul className="text-xs text-red-700 dark:text-red-300 space-y-1">
              {blockingBuckets.map(b => (
                <li key={b.id}>
                  <span className="font-semibold">{b.name}</span>
                  <span className="text-red-600 dark:text-red-400"> — {b.assignment_count} assignment{b.assignment_count !== 1 ? "s" : ""}</span>
                </li>
              ))}
            </ul>
            <p className="text-[11px] text-red-600 dark:text-red-400 mt-2">
              Either remove those assignments first, or select one of those buckets as the keeper.
            </p>
          </div>
        )}

        {errorMsg && blockingBuckets.length === 0 && (
          <div className="mx-6 mt-4 rounded-lg border border-red-300 dark:border-red-500/40 bg-red-50 dark:bg-red-500/10 px-4 py-2.5">
            <p className="text-xs text-red-700 dark:text-red-400">{errorMsg}</p>
          </div>
        )}

        {/* Candidate list */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500 mb-2">
            Pick keeper ({candidates.length} buckets selected)
          </p>
          {candidates.map(b => {
            const isKeeper = b.id === keeperId;
            return (
              <label
                key={b.id}
                className={`flex items-center gap-3 rounded-lg border px-3 py-2.5 cursor-pointer transition-all ${
                  isKeeper
                    ? "border-violet-400 dark:border-violet-500/50 bg-violet-50/50 dark:bg-violet-500/10"
                    : "border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/60 hover:border-zinc-300 dark:hover:border-zinc-700"
                }`}
              >
                <input
                  type="radio"
                  name="keeper"
                  checked={isKeeper}
                  onChange={() => setKeeperId(b.id)}
                  className="accent-violet-600"
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-zinc-800 dark:text-zinc-200 truncate">{b.name}</span>
                    {isKeeper && (
                      <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-200/60 dark:bg-violet-500/20 text-violet-600 dark:text-violet-400">
                        Keep
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 mt-0.5 text-[11px] text-zinc-500">
                    <span>{b.industry || "—"}</span>
                    <span>·</span>
                    <span className="font-mono">{(b.total_contacts || 0).toLocaleString()} contacts</span>
                    <span>·</span>
                    <span className="font-mono">{b.copies_count.titles}T / {b.copies_count.descriptions}D variants</span>
                  </div>
                </div>
              </label>
            );
          })}
        </div>

        {/* Summary + actions */}
        <div className="shrink-0 border-t border-zinc-200 dark:border-zinc-800/40 px-6 py-4 bg-zinc-50/50 dark:bg-zinc-800/20">
          {keeper ? (
            <p className="text-xs text-zinc-600 dark:text-zinc-400 mb-3">
              Move <span className="font-semibold text-zinc-800 dark:text-zinc-200">{totalContactsToMove.toLocaleString()}</span> contacts
              from <span className="font-semibold text-zinc-800 dark:text-zinc-200">{sources.length}</span> bucket{sources.length !== 1 ? "s" : ""}
              into <span className="font-semibold text-violet-600 dark:text-violet-400">{keeper.name}</span>.
              The other bucket{sources.length !== 1 ? "s" : ""} will be deleted.
            </p>
          ) : (
            <p className="text-xs text-zinc-500 mb-3">Select a keeper above.</p>
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={onClose}
              disabled={submitting}
              className="px-4 py-2 text-xs font-medium text-zinc-600 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              onClick={handleConfirm}
              disabled={!keeperId || submitting || sources.length === 0}
              className="px-4 py-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5"
            >
              {submitting && <LoadingSpinner />}
              {submitting ? "Merging…" : "Merge buckets"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ─── Custom Lists Copy Section ────────────────────────────────────────── */

function CustomListsCopySection() {
  const [lists, setLists] = useState<import("@/lib/api").ApiCustomList[]>([]);
  const [loading, setLoading] = useState(true);
  const [modalUploadId, setModalUploadId] = useState<string | null>(null);
  const [modalTab, setModalTab] = useState<"title" | "description">("title");
  const [modalTitles, setModalTitles] = useState<CopyVariant[]>([]);
  const [modalDescs, setModalDescs] = useState<CopyVariant[]>([]);
  const [modalBucket, setModalBucket] = useState<import("@/lib/api").ApiBucket | null>(null);
  const [generating, setGenerating] = useState<string | null>(null);

  useEffect(() => {
    import("@/lib/api").then(({ fetchCustomLists }) =>
      fetchCustomLists().then(({ lists: l }) => { setLists(l); setLoading(false); })
    ).catch(() => setLoading(false));
  }, []);

  const openModal = async (cl: import("@/lib/api").ApiCustomList, tab: "title" | "description") => {
    const { fetchCustomListCopies } = await import("@/lib/api");
    const copies = await fetchCustomListCopies(cl.id);
    setModalUploadId(cl.id);
    setModalTab(tab);
    setModalTitles(copies.titles.map(apiCopyToVariant));
    setModalDescs(copies.descriptions.map(apiCopyToVariant));
    setModalBucket({
      id: cl.id,
      name: cl.name,
      industry: null,
      total_contacts: cl.total_contacts,
      remaining_contacts: cl.available_contacts,
      countries: [],
      emp_range: null,
      source_file: null,
      copies_count: { titles: copies.titles.length, descriptions: copies.descriptions.length },
      has_primary_title: copies.titles.some(c => c.is_primary),
      has_primary_description: copies.descriptions.some(c => c.is_primary),
      title_primary_picked: false,
      desc_primary_picked: false,
      created_at: cl.created_at,
    });
  };

  const handleGenerate = async (clId: string) => {
    setGenerating(clId);
    try {
      const { generateCustomListCopies, fetchCustomListCopies } = await import("@/lib/api");
      await generateCustomListCopies(clId, { copy_type: "both", variant_count: 3 });
      // Refresh lists to update copy counts
      const { fetchCustomLists } = await import("@/lib/api");
      const { lists: refreshed } = await fetchCustomLists();
      setLists(refreshed);
    } catch (err) {
      console.error("Failed to generate:", err);
      alert(err instanceof Error ? err.message : "Generation failed");
    } finally {
      setGenerating(null);
    }
  };

  const handleModalUpdate = async (bucketId: string, type: "title" | "description", variantId: string, newText: string) => {
    const { updateCopy } = await import("@/lib/api");
    const updated = await updateCopy(variantId, { text: newText });
    if (type === "title") setModalTitles(prev => prev.map(v => v.id === variantId ? { ...v, text: updated.text } : v));
    else setModalDescs(prev => prev.map(v => v.id === variantId ? { ...v, text: updated.text } : v));
  };

  const handleModalSetPrimary = async (bucketId: string, type: "title" | "description", variantId: string) => {
    const { updateCopy } = await import("@/lib/api");
    await updateCopy(variantId, { is_primary: true });
    if (type === "title") setModalTitles(prev => prev.map(v => ({ ...v, isPrimary: v.id === variantId })));
    else setModalDescs(prev => prev.map(v => ({ ...v, isPrimary: v.id === variantId })));
  };

  const handleModalRegenerate = async (bucketId: string, type: "title" | "description", copyId: string, feedback: string) => {
    const { regenerateCopy } = await import("@/lib/api");
    const newCopy = await regenerateCopy(copyId, feedback);
    const v = apiCopyToVariant(newCopy);
    if (type === "title") setModalTitles(prev => [...prev, v]);
    else setModalDescs(prev => [...prev, v]);
  };

  const handleModalAdd = async (bucketId: string, type: "title" | "description", text: string) => {
    const { createCustomListCopy } = await import("@/lib/api");
    const newCopy = await createCustomListCopy(bucketId, { copy_type: type, text });
    const v = apiCopyToVariant(newCopy);
    if (type === "title") setModalTitles(prev => [...prev, v]);
    else setModalDescs(prev => [...prev, v]);
  };

  const handleModalDelete = async (bucketId: string, type: "title" | "description", variantId: string) => {
    const { deleteCopy } = await import("@/lib/api");
    await deleteCopy(variantId);
    if (type === "title") setModalTitles(prev => prev.filter(v => v.id !== variantId));
    else setModalDescs(prev => prev.filter(v => v.id !== variantId));
  };

  if (loading) return <div className="text-center py-12 text-zinc-500">Loading custom lists...</div>;
  if (lists.length === 0) return <div className="text-center py-12 text-zinc-500">No custom lists yet. Upload a CSV in Custom List mode.</div>;

  return (
    <>
      <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800/40 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800/40 bg-zinc-50 dark:bg-zinc-800/30">
              <th className="text-left px-4 py-3 font-semibold text-zinc-500 text-[11px] uppercase tracking-wider">List Name</th>
              <th className="text-right px-4 py-3 font-semibold text-zinc-500 text-[11px] uppercase tracking-wider">Total</th>
              <th className="text-right px-4 py-3 font-semibold text-zinc-500 text-[11px] uppercase tracking-wider">Available</th>
              <th className="text-center px-4 py-3 font-semibold text-zinc-500 text-[11px] uppercase tracking-wider">Titles</th>
              <th className="text-center px-4 py-3 font-semibold text-zinc-500 text-[11px] uppercase tracking-wider">Descriptions</th>
              <th className="text-center px-4 py-3 font-semibold text-zinc-500 text-[11px] uppercase tracking-wider">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800/30">
            {lists.map((cl) => (
              <tr key={cl.id} className="hover:bg-zinc-50 dark:hover:bg-zinc-800/20 transition-colors">
                <td className="px-4 py-3 font-medium text-zinc-800 dark:text-zinc-200 max-w-[400px] truncate" title={cl.name}>{cl.name}</td>
                <td className="px-4 py-3 text-right font-mono text-zinc-600 dark:text-zinc-400">{cl.total_contacts.toLocaleString()}</td>
                <td className="px-4 py-3 text-right font-mono text-violet-500">{cl.available_contacts.toLocaleString()}</td>
                <td className="px-4 py-3 text-center">
                  <button onClick={() => openModal(cl, "title")} className="text-[10px] font-semibold text-violet-500 hover:text-violet-400 transition-colors">
                    View →
                  </button>
                </td>
                <td className="px-4 py-3 text-center">
                  <button onClick={() => openModal(cl, "description")} className="text-[10px] font-semibold text-blue-500 hover:text-blue-400 transition-colors">
                    View →
                  </button>
                </td>
                <td className="px-4 py-3 text-center">
                  <button
                    onClick={() => handleGenerate(cl.id)}
                    disabled={generating === cl.id}
                    className="px-3 py-1 text-[10px] font-semibold bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white rounded-md transition-colors"
                  >
                    {generating === cl.id ? "Generating..." : "Generate Copies"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {modalUploadId && modalBucket && (
        <VariationsModal
          bucket={modalBucket}
          initialTab={modalTab}
          titles={modalTitles}
          descriptions={modalDescs}
          onClose={() => { setModalUploadId(null); setModalBucket(null); }}
          onUpdateVariant={handleModalUpdate}
          onSetPrimary={handleModalSetPrimary}
          onRegenerate={handleModalRegenerate}
          onAddVariant={handleModalAdd}
          onDeleteVariant={handleModalDelete}
        />
      )}
    </>
  );
}


/* ─── Main Component ───────────────────────────────────────────────────── */

export function CopyGeneratorPage() {
  const [sourceTab, setSourceTab] = useState<"buckets" | "custom_lists">("buckets");
  const [buckets, setBuckets] = useState<ApiBucket[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [variantCount, setVariantCount] = useState(3);
  const [generatedCopies, setGeneratedCopies] = useState<Map<string, GeneratedCopy[]>>(new Map());
  const [statusMap, setStatusMap] = useState<Map<string, GenerationStatus>>(new Map());
  // Maps `${bucketId}-${type}` → latest job info (for retry + error display)
  const [jobMap, setJobMap] = useState<Map<string, JobMeta>>(new Map());
  const [activeAction, setActiveAction] = useState<"title" | "description" | "both" | null>(null);
  const [modalState, setModalState] = useState<{ bucketId: string; tab: "title" | "description" } | null>(null);
  const [mergeModalOpen, setMergeModalOpen] = useState(false);
  const [editingCell, setEditingCell] = useState<{ bucketId: string; type: "title" | "description" } | null>(null);
  const [editCellText, setEditCellText] = useState("");
  const editRef = useRef<HTMLTextAreaElement>(null);
  /** Inline-rename state for the bucket-name cell. `editingBucketName.id`
   * is the id of the bucket currently being renamed. Errors (e.g. duplicate
   * name from the backend's 409) surface inline below the input. */
  const [editingBucketName, setEditingBucketName] = useState<{ id: string; value: string; error: string | null; saving: boolean } | null>(null);
  const bucketNameInputRef = useRef<HTMLInputElement>(null);

  function startBucketRename(b: ApiBucket) {
    setEditingBucketName({ id: b.id, value: b.name, error: null, saving: false });
    // Focus + select the input on the next tick after it mounts.
    setTimeout(() => {
      bucketNameInputRef.current?.focus();
      bucketNameInputRef.current?.select();
    }, 0);
  }

  async function commitBucketRename() {
    const edit = editingBucketName;
    if (!edit) return;
    const trimmed = edit.value.trim();
    const original = buckets.find((b) => b.id === edit.id);
    // Empty / unchanged → just close the editor without an API call.
    if (!trimmed || trimmed === original?.name) {
      setEditingBucketName(null);
      return;
    }
    setEditingBucketName({ ...edit, saving: true, error: null });
    try {
      const updated = await apiUpdateBucket(edit.id, { name: trimmed });
      setBuckets((prev) => prev.map((b) => (b.id === edit.id ? { ...b, name: updated.name } : b)));
      setEditingBucketName(null);
    } catch (e) {
      setEditingBucketName({
        ...edit,
        saving: false,
        error: e instanceof Error ? e.message : "Rename failed",
      });
    }
  }

  // Filters
  const [genFilter, setGenFilter] = useState<"all" | "missing_title" | "missing_desc" | "missing_any" | "complete">("all");
  const [primaryFilter, setPrimaryFilter] = useState<"all" | "picked" | "not_picked">("all");
  const [groupSimilar, setGroupSimilar] = useState(false);

  /* ── Load buckets with copies from API on mount ─────────────────────── */
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [{ buckets: apiBuckets }, jobResp] = await Promise.all([
          fetchBuckets(true),
          apiFetchCopyGenStatus().catch(() => ({ jobs: [] as ApiCopyGenJob[] })),
        ]);
        if (cancelled) return;
        setBuckets(apiBuckets);

        // Restore generated copies from API data
        const restored = new Map<string, GeneratedCopy[]>();
        const restoredStatus = new Map<string, GenerationStatus>();
        for (const b of apiBuckets) {
          const copies: GeneratedCopy[] = [];
          if (b.titles && b.titles.length > 0) {
            copies.push({
              bucketId: b.id, type: "title",
              variants: b.titles.map(apiCopyToVariant),
              generatedAt: b.titles[0]?.created_at || "",
            });
            restoredStatus.set(`${b.id}-title`, "done");
          }
          if (b.descriptions && b.descriptions.length > 0) {
            copies.push({
              bucketId: b.id, type: "description",
              variants: b.descriptions.map(apiCopyToVariant),
              generatedAt: b.descriptions[0]?.created_at || "",
            });
            restoredStatus.set(`${b.id}-description`, "done");
          }
          if (copies.length > 0) restored.set(b.id, copies);
        }

        // Overlay job status (pending/generating/failed override "done")
        const restoredJobs = new Map<string, JobMeta>();
        for (const j of jobResp.jobs) {
          const key = `${j.bucket_id}-${j.copy_type}`;
          restoredJobs.set(key, { jobId: j.id, errorMessage: j.error_message });
          if (j.status === "pending" || j.status === "generating" || j.status === "failed") {
            restoredStatus.set(key, j.status);
          }
        }

        setGeneratedCopies(restored);
        setStatusMap(restoredStatus);
        setJobMap(restoredJobs);
      } catch (err) {
        console.error("Failed to load buckets:", err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  /* ── Filtering ───────────────────────────────────────────────────────── */

  const filteredBuckets = buckets.filter(b => {
    const hasTitle = b.copies_count.titles > 0;
    const hasDesc = b.copies_count.descriptions > 0;
    if (genFilter === "missing_title" && hasTitle) return false;
    if (genFilter === "missing_desc" && hasDesc) return false;
    if (genFilter === "missing_any" && hasTitle && hasDesc) return false;
    if (genFilter === "complete" && (!hasTitle || !hasDesc)) return false;

    const bothPicked = b.title_primary_picked && b.desc_primary_picked;
    if (primaryFilter === "picked" && !bothPicked) return false;
    if (primaryFilter === "not_picked" && bothPicked) return false;
    return true;
  });

  /* ── Similar-name grouping (client-only view aid) ─────────────────────
   * Cluster buckets by shared topic word in their names. Tokenize each
   * name (drop stopwords + qualifiers like "USA"/"emp"/"5-50"), then
   * union-find any two buckets that share ≥1 token. The cluster label is
   * the most-common token across the cluster's names — so "Accounting,
   * Audit & Tax Services" and "Accounting Firms USA" cluster under
   * "Accounting", and the various "… Agency" buckets cluster under
   * "Agency". */
  const groupedBuckets = useMemo(() => {
    if (!groupSimilar) return null;

    const STOP = new Set<string>([
      "and", "or", "of", "for", "the", "a", "an", "with", "to", "in", "at", "on", "by",
      "services", "service", "solutions", "solution", "group", "groups",
      "company", "companies", "firm", "firms", "llc", "inc", "ltd", "co", "corp",
      "us", "usa", "uk", "eu", "emea", "apac", "na",
      "emp", "employee", "employees",
    ]);
    const tokensFor = (name: string): Set<string> => {
      const out = new Set<string>();
      for (const t of name.toLowerCase().split(/[^a-z0-9]+/)) {
        if (t.length < 3) continue;
        if (/^\d+$/.test(t)) continue;
        if (STOP.has(t)) continue;
        out.add(t);
      }
      return out;
    };

    const tokenMap = new Map<string, Set<string>>();
    filteredBuckets.forEach((b) => tokenMap.set(b.id, tokensFor(b.name)));

    const parent = new Map<string, string>();
    filteredBuckets.forEach((b) => parent.set(b.id, b.id));
    const find = (x: string): string => {
      let r = x;
      while (parent.get(r)! !== r) r = parent.get(r)!;
      let c = x;
      while (parent.get(c)! !== c) {
        const n = parent.get(c)!;
        parent.set(c, r);
        c = n;
      }
      return r;
    };
    const union = (a: string, b: string) => {
      const ra = find(a);
      const rb = find(b);
      if (ra !== rb) parent.set(ra, rb);
    };

    for (let i = 0; i < filteredBuckets.length; i++) {
      const ti = tokenMap.get(filteredBuckets[i].id)!;
      if (ti.size === 0) continue;
      for (let j = i + 1; j < filteredBuckets.length; j++) {
        const tj = tokenMap.get(filteredBuckets[j].id)!;
        let shared = false;
        for (const t of ti) {
          if (tj.has(t)) { shared = true; break; }
        }
        if (shared) union(filteredBuckets[i].id, filteredBuckets[j].id);
      }
    }

    const clusters = new Map<string, ApiBucket[]>();
    filteredBuckets.forEach((b) => {
      const r = find(b.id);
      const arr = clusters.get(r);
      if (arr) arr.push(b);
      else clusters.set(r, [b]);
    });

    const labelFor = (items: ApiBucket[]): string => {
      const counts = new Map<string, number>();
      for (const it of items) {
        for (const t of tokenMap.get(it.id)!) {
          counts.set(t, (counts.get(t) ?? 0) + 1);
        }
      }
      let best = "";
      let bestCount = -1;
      for (const [t, c] of counts) {
        if (c > bestCount || (c === bestCount && best && t < best)) {
          best = t;
          bestCount = c;
        }
      }
      if (!best) {
        const first = items[0].name.split(/[^a-zA-Z0-9]+/).find((w) => w.length > 0);
        return first ?? items[0].name;
      }
      return best.charAt(0).toUpperCase() + best.slice(1);
    };

    return Array.from(clusters.values())
      .map((items) => ({ key: labelFor(items), items }))
      .sort((a, b) => b.items.length - a.items.length || a.key.localeCompare(b.key));
  }, [filteredBuckets, groupSimilar]);

  /* ── Selection ───────────────────────────────────────────────────────── */

  const allSelected = filteredBuckets.length > 0 && filteredBuckets.every(b => selectedIds.has(b.id));
  const someSelected = selectedIds.size > 0;

  const toggleSelect = (id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setSelectedIds(prev => {
      if (allSelected) {
        const next = new Set(prev);
        filteredBuckets.forEach(b => next.delete(b.id));
        return next;
      }
      const next = new Set(prev);
      filteredBuckets.forEach(b => next.add(b.id));
      return next;
    });
  };

  /* ── Generation (background, poll-based) ────────────────────────────── */

  const doGenerateCopies = async (type: "title" | "description" | "both") => {
    if (selectedIds.size === 0) return;
    setActiveAction(type);

    const ids = Array.from(selectedIds);
    // Optimistic: mark selected as pending immediately
    setStatusMap(prev => {
      const next = new Map(prev);
      ids.forEach(id => {
        if (type === "title" || type === "both") next.set(`${id}-title`, "pending");
        if (type === "description" || type === "both") next.set(`${id}-description`, "pending");
      });
      return next;
    });

    try {
      await apiGenerateCopiesBulk({
        bucket_ids: ids,
        copy_type: type,
        variant_count: variantCount,
      });
    } catch (err) {
      console.error("Failed to start bulk generation:", err);
      // Roll back optimistic state
      setStatusMap(prev => {
        const next = new Map(prev);
        ids.forEach(id => {
          if (type === "title" || type === "both") next.set(`${id}-title`, "idle");
          if (type === "description" || type === "both") next.set(`${id}-description`, "idle");
        });
        return next;
      });
    } finally {
      setActiveAction(null);
    }
  };

  const doRetryGeneration = async (bucketId: string, type: "title" | "description") => {
    const key = `${bucketId}-${type}`;
    const meta = jobMap.get(key);
    if (!meta) return;
    // Optimistic
    setStatusMap(prev => {
      const next = new Map(prev);
      next.set(key, "pending");
      return next;
    });
    try {
      const newJob = await apiRetryCopyGenJob(meta.jobId);
      setJobMap(prev => {
        const next = new Map(prev);
        next.set(key, { jobId: newJob.id, errorMessage: null });
        return next;
      });
    } catch (err) {
      console.error("Retry failed:", err);
      setStatusMap(prev => {
        const next = new Map(prev);
        next.set(key, "failed");
        return next;
      });
    }
  };

  /* ── Merge buckets ──────────────────────────────────────────────────── */

  const doMergeBuckets = async (keeperId: string) => {
    const sourceIds = Array.from(selectedIds).filter(id => id !== keeperId);
    await apiMergeBuckets({
      keeper_bucket_id: keeperId,
      source_bucket_ids: sourceIds,
    });
    // Refresh buckets + drop merged ones from state
    const { buckets: refreshed } = await fetchBuckets(true);
    setBuckets(refreshed);
    setGeneratedCopies(prev => {
      const next = new Map(prev);
      for (const id of sourceIds) next.delete(id);
      return next;
    });
    setStatusMap(prev => {
      const next = new Map(prev);
      for (const id of sourceIds) {
        next.delete(`${id}-title`);
        next.delete(`${id}-description`);
      }
      return next;
    });
    setJobMap(prev => {
      const next = new Map(prev);
      for (const id of sourceIds) {
        next.delete(`${id}-title`);
        next.delete(`${id}-description`);
      }
      return next;
    });
    setSelectedIds(new Set([keeperId]));
  };

  /* ── Poll job status while any are in-flight ────────────────────────── */

  useEffect(() => {
    const hasActive = Array.from(statusMap.values()).some(
      s => s === "pending" || s === "generating"
    );
    if (!hasActive) return;

    let cancelled = false;
    const interval = setInterval(async () => {
      if (cancelled) return;
      try {
        const { jobs } = await apiFetchCopyGenStatus();
        if (cancelled) return;

        // Detect newly-done jobs by comparing against the current statusMap
        // closure. We must do this BEFORE any setState — otherwise the
        // statusMap update triggers an effect re-run whose cleanup sets
        // `cancelled = true`, and the bucket re-fetch below silently bails.
        const newlyDone: Array<{ bucketId: string; type: "title" | "description" }> = [];
        for (const j of jobs) {
          const key = `${j.bucket_id}-${j.copy_type}`;
          const prevStatus = statusMap.get(key);
          if (prevStatus !== "done" && j.status === "done") {
            newlyDone.push({ bucketId: j.bucket_id, type: j.copy_type });
          }
        }

        // For freshly-completed jobs, fetch the new bucket copies first —
        // still no setState calls yet, so no risk of mid-flight cancellation.
        let refreshedBuckets: ApiBucket[] | null = null;
        if (newlyDone.length > 0) {
          const { buckets: refreshed } = await fetchBuckets(true);
          if (cancelled) return;
          refreshedBuckets = refreshed;
        }

        // Now batch all state updates together. React 18 auto-batches these,
        // so the effect re-runs at most once after this callback returns.
        if (refreshedBuckets) {
          setBuckets(refreshedBuckets);
          const touched = new Set(newlyDone.map(d => d.bucketId));
          const fresh = refreshedBuckets;
          setGeneratedCopies(prev => {
            const next = new Map(prev);
            for (const b of fresh) {
              if (!touched.has(b.id)) continue;
              const copies: GeneratedCopy[] = [];
              if (b.titles && b.titles.length > 0) {
                copies.push({
                  bucketId: b.id, type: "title",
                  variants: b.titles.map(apiCopyToVariant),
                  generatedAt: b.titles[0]?.created_at || "",
                });
              }
              if (b.descriptions && b.descriptions.length > 0) {
                copies.push({
                  bucketId: b.id, type: "description",
                  variants: b.descriptions.map(apiCopyToVariant),
                  generatedAt: b.descriptions[0]?.created_at || "",
                });
              }
              next.set(b.id, copies);
            }
            return next;
          });
        }

        setStatusMap(prev => {
          const next = new Map(prev);
          for (const j of jobs) {
            const key = `${j.bucket_id}-${j.copy_type}`;
            if (j.status === "pending" || j.status === "generating" || j.status === "failed") {
              next.set(key, j.status);
            } else if (j.status === "done") {
              next.set(key, "done");
            }
          }
          return next;
        });

        setJobMap(prev => {
          const next = new Map(prev);
          for (const j of jobs) {
            next.set(`${j.bucket_id}-${j.copy_type}`, {
              jobId: j.id,
              errorMessage: j.error_message,
            });
          }
          return next;
        });
      } catch (err) {
        console.error("Polling failed:", err);
      }
    }, 2500);

    return () => { cancelled = true; clearInterval(interval); };
  }, [statusMap]);

  /* ── Helpers ─────────────────────────────────────────────────────────── */

  const getStatus = (bucketId: string, type: string): GenerationStatus =>
    statusMap.get(`${bucketId}-${type}`) || "idle";

  const getCopies = (bucketId: string, type: "title" | "description"): CopyVariant[] => {
    const copies = generatedCopies.get(bucketId);
    if (!copies) return [];
    const found = copies.find(c => c.type === type);
    return found ? found.variants : [];
  };

  const getPrimary = (bucketId: string, type: "title" | "description"): CopyVariant | null => {
    const variants = getCopies(bucketId, type);
    return variants.find(v => v.isPrimary) || variants[0] || null;
  };

  const hasCopies = (bucketId: string): boolean => {
    const copies = generatedCopies.get(bucketId);
    return !!copies && copies.length > 0;
  };

  /* ── Mutation helpers (API-backed) ──────────────────────────────────── */

  const updateVariantText = useCallback(async (bucketId: string, type: "title" | "description", variantId: string, newText: string) => {
    // Optimistic update
    setGeneratedCopies(prev => {
      const next = new Map(prev);
      const copies = (next.get(bucketId) || []).map(c => {
        if (c.type !== type) return c;
        return { ...c, variants: c.variants.map(v => v.id === variantId ? { ...v, text: newText } : v) };
      });
      next.set(bucketId, copies);
      return next;
    });
    // Fire API call
    try {
      await apiUpdateCopy(variantId, { text: newText });
    } catch (err) {
      console.error("Failed to update copy:", err);
    }
  }, []);

  const setPrimaryVariant = useCallback(async (bucketId: string, type: "title" | "description", variantId: string) => {
    // Optimistic update — variants
    setGeneratedCopies(prev => {
      const next = new Map(prev);
      const copies = (next.get(bucketId) || []).map(c => {
        if (c.type !== type) return c;
        return { ...c, variants: c.variants.map(v => ({ ...v, isPrimary: v.id === variantId })) };
      });
      next.set(bucketId, copies);
      return next;
    });
    // Optimistic update — bucket-level picked flag
    setBuckets(prev => prev.map(b => {
      if (b.id !== bucketId) return b;
      return type === "title"
        ? { ...b, title_primary_picked: true }
        : { ...b, desc_primary_picked: true };
    }));
    // Fire API call
    try {
      await apiUpdateCopy(variantId, { is_primary: true });
    } catch (err) {
      console.error("Failed to set primary:", err);
    }
  }, []);

  const handleRegenerate = useCallback(async (bucketId: string, type: "title" | "description", copyId: string, feedback: string) => {
    try {
      const newCopy = await apiRegenerateCopy(copyId, feedback);
      // Add the new variant to local state
      setGeneratedCopies(prev => {
        const next = new Map(prev);
        const copies = (next.get(bucketId) || []).map(c => {
          if (c.type !== type) return c;
          return { ...c, variants: [...c.variants, apiCopyToVariant(newCopy)] };
        });
        next.set(bucketId, copies);
        return next;
      });
    } catch (err) {
      console.error("Failed to regenerate copy:", err);
    }
  }, []);

  const handleAddVariant = useCallback(async (bucketId: string, type: "title" | "description", text: string) => {
    try {
      const newCopy = await apiCreateCopy(bucketId, { copy_type: type, text });
      setGeneratedCopies(prev => {
        const next = new Map(prev);
        const existing = next.get(bucketId) || [];
        const found = existing.find(c => c.type === type);
        if (found) {
          const copies = existing.map(c => {
            if (c.type !== type) return c;
            return { ...c, variants: [...c.variants, apiCopyToVariant(newCopy)] };
          });
          next.set(bucketId, copies);
        } else {
          existing.push({
            bucketId, type,
            variants: [apiCopyToVariant(newCopy)],
            generatedAt: new Date().toLocaleTimeString(),
          });
          next.set(bucketId, existing);
        }
        return next;
      });
    } catch (err) {
      console.error("Failed to add variant:", err);
    }
  }, []);

  const handleDeleteVariant = useCallback(async (bucketId: string, type: "title" | "description", variantId: string) => {
    try {
      await apiDeleteCopy(variantId);
      setGeneratedCopies(prev => {
        const next = new Map(prev);
        const copies = (next.get(bucketId) || []).map(c => {
          if (c.type !== type) return c;
          const remaining = c.variants.filter(v => v.id !== variantId);
          // If the deleted variant was primary, auto-select the first remaining
          const deletedWasPrimary = c.variants.find(v => v.id === variantId)?.isPrimary;
          if (deletedWasPrimary && remaining.length > 0) {
            remaining[0] = { ...remaining[0], isPrimary: true };
          }
          return { ...c, variants: remaining };
        });
        next.set(bucketId, copies);
        return next;
      });
    } catch (err) {
      console.error("Failed to delete variant:", err);
    }
  }, []);

  /* ── Inline edit handlers ────────────────────────────────────────────── */

  const startInlineEdit = (bucketId: string, type: "title" | "description", text: string) => {
    setEditingCell({ bucketId, type });
    setEditCellText(text);
  };

  const saveInlineEdit = () => {
    if (!editingCell) return;
    const primary = getPrimary(editingCell.bucketId, editingCell.type);
    if (primary) {
      updateVariantText(editingCell.bucketId, editingCell.type, primary.id, editCellText);
    }
    setEditingCell(null);
    setEditCellText("");
  };

  useEffect(() => {
    if (editingCell && editRef.current) editRef.current.focus();
  }, [editingCell]);

  const totalGenerated = Array.from(generatedCopies.values()).reduce((sum, copies) => sum + copies.length, 0);

  // Get modal bucket data
  const modalBucket = modalState ? buckets.find(b => b.id === modalState.bucketId) : null;

  /* ── Render ──────────────────────────────────────────────────────────── */

  if (loading) {
    return (
      <main className="flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0 flex items-center justify-center">
        <div className="flex items-center gap-3 text-zinc-400">
          <LoadingSpinner /> Loading buckets…
        </div>
      </main>
    );
  }

  return (
    <main className="flex-1 bg-zinc-50 dark:bg-zinc-950 min-h-0">
      <div className="px-6 py-5 max-w-[1600px] mx-auto">

        {/* ── Page Header ──────────────────────────────────────────────── */}
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <a
              href="/studio"
              className="px-3 py-1.5 bg-zinc-100 dark:bg-zinc-800 hover:bg-zinc-200 dark:hover:bg-zinc-700 border border-zinc-200 dark:border-zinc-700/60 rounded-lg text-xs font-semibold text-zinc-700 dark:text-zinc-300 transition-colors flex items-center gap-1.5"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
              Studio
            </a>
            <div>
              <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Calendar Invite Copy Generator</h1>
              <p className="text-sm text-zinc-500 dark:text-zinc-400 mt-0.5">Generate calendar invite titles and descriptions for your outreach buckets.</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2 bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800/40 rounded-lg px-3 py-1.5">
              <span className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium">Buckets</span>
              <span className="text-sm font-bold text-zinc-800 dark:text-zinc-200 font-mono">
                {filteredBuckets.length !== buckets.length ? `${filteredBuckets.length}/${buckets.length}` : buckets.length}
              </span>
            </div>
            {totalGenerated > 0 && (
              <div className="flex items-center gap-2 bg-violet-50 dark:bg-violet-500/10 border border-violet-200 dark:border-violet-500/20 rounded-lg px-3 py-1.5">
                <span className="text-[11px] text-violet-600 dark:text-violet-400 uppercase tracking-wider font-medium">Generated</span>
                <span className="text-sm font-bold text-violet-600 dark:text-violet-400 font-mono">{totalGenerated}</span>
              </div>
            )}
          </div>
        </div>

        {/* ── Source Tab Switcher ───────────────────────────────────────── */}
        <div className="flex items-center gap-2 mb-4">
          <div className="flex gap-1 bg-zinc-200 dark:bg-zinc-800 rounded-lg p-0.5">
            <button
              onClick={() => setSourceTab("buckets")}
              className={`px-4 py-1.5 rounded-md text-xs font-semibold transition-all ${
                sourceTab === "buckets"
                  ? "bg-white dark:bg-zinc-700 text-zinc-900 dark:text-zinc-100 shadow-sm"
                  : "text-zinc-500 dark:text-zinc-400 hover:text-zinc-700"
              }`}
            >Buckets</button>
            <button
              onClick={() => setSourceTab("custom_lists")}
              className={`px-4 py-1.5 rounded-md text-xs font-semibold transition-all ${
                sourceTab === "custom_lists"
                  ? "bg-white dark:bg-zinc-700 text-zinc-900 dark:text-zinc-100 shadow-sm"
                  : "text-zinc-500 dark:text-zinc-400 hover:text-zinc-700"
              }`}
            >Custom Lists</button>
          </div>
        </div>

        {/* ── Brain Panel ──────────────────────────────────────────────── */}
        <div className="mb-4">
          <BrainPanel />
        </div>

        {/* ── Custom Lists Tab Content ─────────────────────────────────── */}
        {sourceTab === "custom_lists" && (
          <CustomListsCopySection />
        )}

        {/* ── Buckets Tab Content ─────────────────────────────────────── */}
        {sourceTab === "buckets" && <>

        {/* ── Floating Action Bar ──────────────────────────────────────── */}
        {someSelected && (
          <div className="fixed left-1/2 -translate-x-1/2 bottom-5 z-40 flex items-center gap-3 bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800/40 rounded-xl px-4 py-3 shadow-lg">
            <span className="text-sm font-semibold text-zinc-700 dark:text-zinc-300">
              {selectedIds.size} bucket{selectedIds.size !== 1 ? "s" : ""} selected
            </span>
            <button
              onClick={() => setSelectedIds(new Set())}
              disabled={activeAction !== null}
              className="text-xs text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              title="Clear selection"
            >
              Clear
            </button>
            <div className="w-px h-5 bg-zinc-200 dark:bg-zinc-700" />
            <div className="flex items-center gap-2">
              <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium">Variations</label>
              <input
                type="number" min={1} max={10} value={variantCount}
                onChange={(e) => setVariantCount(Math.max(1, Math.min(10, parseInt(e.target.value) || 3)))}
                className="w-14 bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2 py-1 text-sm text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500"
              />
            </div>
            <div className="w-px h-5 bg-zinc-200 dark:bg-zinc-700" />
            <button onClick={() => doGenerateCopies("title")} disabled={activeAction !== null}
              className="px-4 py-1.5 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 disabled:cursor-wait text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5">
              {activeAction === "title" && <LoadingSpinner />} Generate Titles
            </button>
            <button onClick={() => doGenerateCopies("description")} disabled={activeAction !== null}
              className="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-wait text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5">
              {activeAction === "description" && <LoadingSpinner />} Generate Descriptions
            </button>
            <button onClick={() => doGenerateCopies("both")} disabled={activeAction !== null}
              className="px-4 py-1.5 bg-gradient-to-r from-violet-600 to-blue-600 hover:from-violet-500 hover:to-blue-500 disabled:opacity-50 disabled:cursor-wait text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5">
              {activeAction === "both" && <LoadingSpinner />} Generate Both
            </button>
            {selectedIds.size >= 2 && (
              <>
                <div className="w-px h-5 bg-zinc-200 dark:bg-zinc-700" />
                <button
                  onClick={() => setMergeModalOpen(true)}
                  disabled={activeAction !== null}
                  className="px-4 py-1.5 border border-zinc-300 dark:border-zinc-700/60 hover:border-violet-400 dark:hover:border-violet-500/50 hover:bg-violet-50 dark:hover:bg-violet-500/10 hover:text-violet-600 dark:hover:text-violet-400 disabled:opacity-50 disabled:cursor-wait text-zinc-700 dark:text-zinc-300 text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5"
                  title="Merge selected buckets into one"
                >
                  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16V4m0 0L3 8m4-4l4 4m6 0v12m0 0l4-4m-4 4l-4-4" /></svg>
                  Merge
                </button>
              </>
            )}
          </div>
        )}

        {/* ── Filter Bar ──────────────────────────────────────────────── */}
        <div className="mb-3 flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium">Generation</label>
            <select
              value={genFilter}
              onChange={(e) => setGenFilter(e.target.value as typeof genFilter)}
              className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800/40 rounded-lg px-2.5 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 cursor-pointer"
            >
              <option value="all">All</option>
              <option value="missing_title">Missing titles</option>
              <option value="missing_desc">Missing descriptions</option>
              <option value="missing_any">Missing either</option>
              <option value="complete">Fully generated</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-zinc-500 uppercase tracking-wider font-medium">Primary</label>
            <select
              value={primaryFilter}
              onChange={(e) => setPrimaryFilter(e.target.value as typeof primaryFilter)}
              className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800/40 rounded-lg px-2.5 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500 cursor-pointer"
            >
              <option value="all">All</option>
              <option value="picked">Picked by user</option>
              <option value="not_picked">Not picked</option>
            </select>
          </div>
          {(genFilter !== "all" || primaryFilter !== "all") && (
            <button
              onClick={() => { setGenFilter("all"); setPrimaryFilter("all"); }}
              className="text-[11px] text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 underline underline-offset-2 decoration-zinc-400/40 hover:decoration-zinc-600 transition-colors"
            >
              Clear filters
            </button>
          )}
          <div className="ml-auto flex items-center gap-2">
            <label className="flex items-center gap-1.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium cursor-pointer select-none">
              <input
                type="checkbox"
                checked={groupSimilar}
                onChange={(e) => setGroupSimilar(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-zinc-300 dark:border-zinc-600 text-violet-600 focus:ring-violet-500 cursor-pointer accent-violet-600"
              />
              Group similar names
            </label>
          </div>
        </div>

        {/* ── Buckets Table ────────────────────────────────────────────── */}
        <div className="rounded-xl border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900 overflow-hidden shadow-sm">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-zinc-50 dark:bg-zinc-800/30 border-b border-zinc-200 dark:border-zinc-800/40">
                <th className="w-10 px-3 py-2.5">
                  <input type="checkbox" checked={allSelected} onChange={toggleAll}
                    className="w-3.5 h-3.5 rounded border-zinc-300 dark:border-zinc-600 text-violet-600 focus:ring-violet-500 cursor-pointer accent-violet-600" />
                </th>
                <th className="text-left px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium w-[180px]">Bucket</th>
                <th className="text-left px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium w-[90px]">Industry</th>
                <th className="text-right px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium w-[70px]">Total</th>
                <th className="text-right px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium w-[80px]">Remaining</th>
                <th className="text-center px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium w-[100px]">Countries</th>
                <th className="text-center px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium w-[60px]">Emp</th>
                <th className="text-left px-3 py-2.5 text-[11px] text-zinc-500 uppercase tracking-wider font-medium w-[90px]">Created</th>
                <th className="text-left px-3 py-2.5 text-[11px] text-violet-500 dark:text-violet-400 uppercase tracking-wider font-medium">Title</th>
                <th className="text-left px-3 py-2.5 text-[11px] text-blue-500 dark:text-blue-400 uppercase tracking-wider font-medium">Description</th>
              </tr>
            </thead>
            <tbody>
              {filteredBuckets.length === 0 && buckets.length > 0 && (
                <tr>
                  <td colSpan={10} className="px-3 py-10 text-center text-xs text-zinc-500">
                    No buckets match the current filters.
                  </td>
                </tr>
              )}
              {(() => {
              const renderBucketRow = (bucket: ApiBucket) => {
                const isSelected = selectedIds.has(bucket.id);
                const titleStatus = getStatus(bucket.id, "title");
                const descStatus = getStatus(bucket.id, "description");
                const primaryTitle = getPrimary(bucket.id, "title");
                const primaryDesc = getPrimary(bucket.id, "description");
                const titleVariants = getCopies(bucket.id, "title");
                const descVariants = getCopies(bucket.id, "description");
                const primaryTitleIdx = titleVariants.findIndex(v => v.isPrimary);
                const primaryDescIdx = descVariants.findIndex(v => v.isPrimary);
                const isTitleEditing = editingCell?.bucketId === bucket.id && editingCell?.type === "title";
                const isDescEditing = editingCell?.bucketId === bucket.id && editingCell?.type === "description";

                return (
                  <tr
                    key={bucket.id}
                    className={`border-b border-zinc-100 dark:border-zinc-800/30 transition-colors ${
                      isSelected ? "bg-violet-50/50 dark:bg-violet-500/5" : "hover:bg-zinc-50 dark:hover:bg-zinc-800/20"
                    }`}
                  >
                    <td className="px-3 py-3">
                      <input type="checkbox" checked={isSelected} onChange={() => toggleSelect(bucket.id)}
                        className="w-3.5 h-3.5 rounded border-zinc-300 dark:border-zinc-600 text-violet-600 focus:ring-violet-500 cursor-pointer accent-violet-600" />
                    </td>
                    <td className="px-3 py-3 font-medium text-zinc-800 dark:text-zinc-200 text-[13px] group/name">
                      {editingBucketName?.id === bucket.id ? (
                        <div className="flex flex-col gap-1">
                          <div className="flex items-center gap-1.5">
                            <input
                              ref={bucketNameInputRef}
                              type="text"
                              value={editingBucketName.value}
                              disabled={editingBucketName.saving}
                              onChange={(e) =>
                                setEditingBucketName((prev) => prev && { ...prev, value: e.target.value, error: null })
                              }
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  e.preventDefault();
                                  commitBucketRename();
                                } else if (e.key === "Escape") {
                                  e.preventDefault();
                                  setEditingBucketName(null);
                                }
                              }}
                              onBlur={() => {
                                // Defer slightly so a click on Save lands first.
                                setTimeout(() => {
                                  if (!editingBucketName?.saving) commitBucketRename();
                                }, 100);
                              }}
                              className="flex-1 min-w-0 bg-white dark:bg-zinc-900 border border-violet-500/60 focus:border-violet-500 rounded px-2 py-1 text-[13px] text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
                            />
                            {editingBucketName.saving && (
                              <span className="inline-block w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
                            )}
                          </div>
                          {editingBucketName.error && (
                            <span className="text-[10px] text-red-500">{editingBucketName.error}</span>
                          )}
                        </div>
                      ) : (
                        <button
                          type="button"
                          onClick={() => startBucketRename(bucket)}
                          title="Rename bucket"
                          className="inline-flex items-center gap-1.5 text-left rounded px-1 -mx-1 py-0.5 hover:bg-zinc-100 dark:hover:bg-zinc-800/40 transition-colors"
                        >
                          <span className="truncate">{bucket.name}</span>
                          <svg
                            width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                            className="text-zinc-400 opacity-0 group-hover/name:opacity-100 transition-opacity flex-shrink-0"
                          >
                            <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
                            <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
                          </svg>
                        </button>
                      )}
                    </td>
                    <td className="px-3 py-3 text-zinc-500 dark:text-zinc-400 text-xs">{bucket.industry}</td>
                    <td className="px-3 py-3 text-right font-mono text-zinc-700 dark:text-zinc-300 text-xs">{bucket.total_contacts.toLocaleString()}</td>
                    <td className="px-3 py-3 text-right font-mono text-violet-600 dark:text-violet-400 text-xs">{bucket.remaining_contacts.toLocaleString()}</td>
                    <td className="px-3 py-3 text-center">
                      <div className="flex gap-1 justify-center">{(bucket.countries || []).map(c => <CountryBadge key={c} code={c} />)}</div>
                    </td>
                    <td className="px-3 py-3 text-center text-xs text-zinc-500 font-mono">{bucket.emp_range}</td>
                    <td className="px-3 py-3 text-xs text-zinc-500 dark:text-zinc-400 font-mono whitespace-nowrap">
                      {bucket.created_at ? new Date(bucket.created_at).toLocaleDateString() : "—"}
                    </td>

                    {/* ── Title Cell ───────────────────────────────── */}
                    <td className="px-3 py-2">
                      {titleStatus === "pending" ? (
                        <span className="inline-flex items-center gap-1.5 text-[10px] text-zinc-500 font-medium">
                          <LoadingSpinner /> Queued…
                        </span>
                      ) : titleStatus === "generating" ? (
                        <span className="inline-flex items-center gap-1.5 text-[10px] text-amber-500 font-medium">
                          <LoadingSpinner /> Generating…
                        </span>
                      ) : titleStatus === "failed" ? (
                        <div className="flex flex-col gap-1">
                          <span
                            className="inline-flex items-center gap-1 text-[10px] text-red-500 font-medium"
                            title={jobMap.get(`${bucket.id}-title`)?.errorMessage || "Generation failed"}
                          >
                            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                            Failed
                          </span>
                          <button
                            onClick={() => doRetryGeneration(bucket.id, "title")}
                            className="text-[10px] font-medium text-violet-500 hover:text-violet-400 self-start"
                          >
                            Retry
                          </button>
                        </div>
                      ) : primaryTitle ? (
                        <div className="max-w-[280px]">
                          {isTitleEditing ? (
                            <textarea
                              ref={editRef}
                              value={editCellText}
                              onChange={(e) => setEditCellText(e.target.value)}
                              onBlur={saveInlineEdit}
                              onKeyDown={(e) => { if (e.key === "Escape") setEditingCell(null); if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); saveInlineEdit(); } }}
                              className="w-full bg-white dark:bg-zinc-800 border border-violet-400 dark:border-violet-500/50 rounded px-2 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 leading-relaxed focus:outline-none focus:ring-1 focus:ring-violet-500 resize-none"
                              rows={2}
                            />
                          ) : (
                            <>
                              {(titleVariants.length > 1 || bucket.title_primary_picked) && (
                                <span className="inline-flex items-center gap-1 mb-1 flex-wrap">
                                  {titleVariants.length > 1 && primaryTitleIdx >= 0 && (
                                    <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400">
                                      Variant {primaryTitleIdx + 1}
                                    </span>
                                  )}
                                  {bucket.title_primary_picked && (
                                    <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
                                      Primary picked
                                    </span>
                                  )}
                                </span>
                              )}
                              <p
                                className="text-xs text-zinc-700 dark:text-zinc-300 leading-snug line-clamp-2 cursor-text hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors"
                                onClick={() => startInlineEdit(bucket.id, "title", primaryTitle.text)}
                                title={primaryTitle.text}
                              >
                                {primaryTitle.text}
                              </p>
                            </>
                          )}
                          {titleVariants.length > 1 && (
                            <button
                              onClick={() => setModalState({ bucketId: bucket.id, tab: "title" })}
                              className="text-[10px] text-violet-500 hover:text-violet-400 font-medium mt-1 transition-colors"
                            >
                              {titleVariants.length} variations →
                            </button>
                          )}
                          {titleVariants.length === 1 && (
                            <button
                              onClick={() => setModalState({ bucketId: bucket.id, tab: "title" })}
                              className="text-[10px] text-zinc-400 hover:text-violet-400 font-medium mt-1 transition-colors"
                            >
                              View & edit →
                            </button>
                          )}
                        </div>
                      ) : (
                        <span className="text-zinc-300 dark:text-zinc-600">—</span>
                      )}
                    </td>

                    {/* ── Description Cell ─────────────────────────── */}
                    <td className="px-3 py-2">
                      {descStatus === "pending" ? (
                        <span className="inline-flex items-center gap-1.5 text-[10px] text-zinc-500 font-medium">
                          <LoadingSpinner /> Queued…
                        </span>
                      ) : descStatus === "generating" ? (
                        <span className="inline-flex items-center gap-1.5 text-[10px] text-amber-500 font-medium">
                          <LoadingSpinner /> Generating…
                        </span>
                      ) : descStatus === "failed" ? (
                        <div className="flex flex-col gap-1">
                          <span
                            className="inline-flex items-center gap-1 text-[10px] text-red-500 font-medium"
                            title={jobMap.get(`${bucket.id}-description`)?.errorMessage || "Generation failed"}
                          >
                            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                            Failed
                          </span>
                          <button
                            onClick={() => doRetryGeneration(bucket.id, "description")}
                            className="text-[10px] font-medium text-blue-500 hover:text-blue-400 self-start"
                          >
                            Retry
                          </button>
                        </div>
                      ) : primaryDesc ? (
                        <div className="max-w-[320px]">
                          {isDescEditing ? (
                            <textarea
                              ref={editRef}
                              value={editCellText}
                              onChange={(e) => setEditCellText(e.target.value)}
                              onBlur={saveInlineEdit}
                              onKeyDown={(e) => { if (e.key === "Escape") setEditingCell(null); if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); saveInlineEdit(); } }}
                              className="w-full bg-white dark:bg-zinc-800 border border-blue-400 dark:border-blue-500/50 rounded px-2 py-1.5 text-xs text-zinc-800 dark:text-zinc-200 leading-relaxed focus:outline-none focus:ring-1 focus:ring-blue-500 resize-none"
                              rows={3}
                            />
                          ) : (
                            <>
                              {(descVariants.length > 1 || bucket.desc_primary_picked) && (
                                <span className="inline-flex items-center gap-1 mb-1 flex-wrap">
                                  {descVariants.length > 1 && primaryDescIdx >= 0 && (
                                    <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-500/15 text-blue-600 dark:text-blue-400">
                                      Variant {primaryDescIdx + 1}
                                    </span>
                                  )}
                                  {bucket.desc_primary_picked && (
                                    <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
                                      Primary picked
                                    </span>
                                  )}
                                </span>
                              )}
                              <p
                                className="text-xs text-zinc-700 dark:text-zinc-300 leading-snug line-clamp-2 cursor-text hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors"
                                onClick={() => startInlineEdit(bucket.id, "description", primaryDesc.text)}
                                title={primaryDesc.text}
                              >
                                {primaryDesc.text}
                              </p>
                            </>
                          )}
                          {descVariants.length > 1 && (
                            <button
                              onClick={() => setModalState({ bucketId: bucket.id, tab: "description" })}
                              className="text-[10px] text-blue-500 hover:text-blue-400 font-medium mt-1 transition-colors"
                            >
                              {descVariants.length} variations →
                            </button>
                          )}
                          {descVariants.length === 1 && (
                            <button
                              onClick={() => setModalState({ bucketId: bucket.id, tab: "description" })}
                              className="text-[10px] text-zinc-400 hover:text-blue-400 font-medium mt-1 transition-colors"
                            >
                              View & edit →
                            </button>
                          )}
                        </div>
                      ) : (
                        <span className="text-zinc-300 dark:text-zinc-600">—</span>
                      )}
                    </td>
                  </tr>
                );
              };
              if (groupSimilar && groupedBuckets) {
                return groupedBuckets.map((g) => (
                  <React.Fragment key={`grp-${g.key}`}>
                    <tr className="bg-zinc-50 dark:bg-zinc-800/40 border-b border-zinc-200 dark:border-zinc-700/40">
                      <td colSpan={10} className="px-3 py-1.5 text-[11px] uppercase tracking-wider text-zinc-600 dark:text-zinc-300">
                        <span className="font-semibold">{g.key}</span>
                        <span className="ml-2 text-zinc-400 dark:text-zinc-500 normal-case">
                          · {g.items.length} bucket{g.items.length === 1 ? "" : "s"} · {g.items.reduce((s, b) => s + b.total_contacts, 0).toLocaleString()} contacts
                        </span>
                        {g.items.length > 1 && (
                          <button
                            onClick={() => {
                              setSelectedIds((prev) => {
                                const next = new Set(prev);
                                g.items.forEach((b) => next.add(b.id));
                                return next;
                              });
                            }}
                            className="ml-3 text-[10px] normal-case text-violet-600 dark:text-violet-400 hover:underline"
                          >
                            Select group
                          </button>
                        )}
                      </td>
                    </tr>
                    {g.items.map(renderBucketRow)}
                  </React.Fragment>
                ));
              }
              return filteredBuckets.map(renderBucketRow);
              })()}
            </tbody>
          </table>
        </div>

      </>}
      </div>

      {/* ── Modal ──────────────────────────────────────────────────────── */}
      {modalState && modalBucket && (
        <VariationsModal
          bucket={modalBucket}
          initialTab={modalState.tab}
          titles={getCopies(modalState.bucketId, "title")}
          descriptions={getCopies(modalState.bucketId, "description")}
          onClose={() => setModalState(null)}
          onUpdateVariant={updateVariantText}
          onSetPrimary={setPrimaryVariant}
          onRegenerate={handleRegenerate}
          onAddVariant={handleAddVariant}
          onDeleteVariant={handleDeleteVariant}
        />
      )}

      {/* ── Merge Modal ────────────────────────────────────────────────── */}
      {mergeModalOpen && (
        <MergeBucketsModal
          candidates={buckets.filter(b => selectedIds.has(b.id))}
          onClose={() => setMergeModalOpen(false)}
          onConfirm={doMergeBuckets}
        />
      )}
    </main>
  );
}

/* ─── Sub-components ───────────────────────────────────────────────────── */

function LoadingSpinner() {
  return (
    <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}
