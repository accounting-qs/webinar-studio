"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  fetchPrinciples, createPrinciple, updatePrinciple, deletePrinciple,
  proposePrincipleChanges, applyPrincipleChanges,
  fetchCaseStudies, createCaseStudy as apiCreateCaseStudy, updateCaseStudy as apiUpdateCaseStudy, deleteCaseStudy as apiDeleteCaseStudy,
  importCaseStudyFromUrl, reextractCaseStudy,
  fetchBrainContent, updateUniversalBrain, updateFormatBrain,
  type ApiPrinciple, type ApiCaseStudy, type ApiBrainContent,
  type ApiReconcileProposal,
} from "@/lib/api";

/* ─── Spinner ─────────────────────────────────────────────────────────── */

function Spinner() {
  return (
    <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold">{label}</div>
      <div className="text-zinc-700 dark:text-zinc-300 truncate">{value}</div>
    </div>
  );
}

/* ─── Principles Tab ──────────────────────────────────────────────────── */

const OP_BADGE: Record<string, string> = {
  add: "bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  edit: "bg-amber-100 dark:bg-amber-500/15 text-amber-600 dark:text-amber-400",
  delete: "bg-red-100 dark:bg-red-500/15 text-red-600 dark:text-red-400",
};
const OP_LABEL: Record<string, string> = { add: "Add", edit: "Edit", delete: "Delete" };

function PrinciplesTab() {
  const [principles, setPrinciples] = useState<ApiPrinciple[]>([]);
  const [loading, setLoading] = useState(true);
  const [newText, setNewText] = useState("");
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  // AI reconcile (chat-to-update) state
  const [chatInput, setChatInput] = useState("");
  const [proposing, setProposing] = useState(false);
  const [proposal, setProposal] = useState<ApiReconcileProposal | null>(null);
  const [accepted, setAccepted] = useState<boolean[]>([]);
  const [applying, setApplying] = useState(false);
  const [reconcileError, setReconcileError] = useState<string | null>(null);

  useEffect(() => {
    fetchPrinciples().then(setPrinciples).catch(console.error).finally(() => setLoading(false));
  }, []);

  const handlePropose = useCallback(async () => {
    const instruction = chatInput.trim();
    if (!instruction) return;
    setProposing(true);
    setReconcileError(null);
    setProposal(null);
    try {
      const p = await proposePrincipleChanges(instruction);
      setProposal(p);
      setAccepted(p.operations.map(() => true));
    } catch (err) {
      setReconcileError(err instanceof Error ? err.message : "Failed to get proposed changes");
    } finally {
      setProposing(false);
    }
  }, [chatInput]);

  const handleApplyProposal = useCallback(async () => {
    if (!proposal) return;
    const ops = proposal.operations.filter((_, i) => accepted[i]);
    if (ops.length === 0) { setProposal(null); setChatInput(""); return; }
    setApplying(true);
    setReconcileError(null);
    try {
      const updated = await applyPrincipleChanges(ops);
      setPrinciples(updated);
      setProposal(null);
      setAccepted([]);
      setChatInput("");
    } catch (err) {
      setReconcileError(err instanceof Error ? err.message : "Failed to apply changes");
    } finally {
      setApplying(false);
    }
  }, [proposal, accepted]);

  const handleDiscardProposal = useCallback(() => {
    setProposal(null);
    setAccepted([]);
    setReconcileError(null);
  }, []);

  const handleAdd = useCallback(async () => {
    if (!newText.trim()) return;
    setAdding(true);
    try {
      const p = await createPrinciple({ principle_text: newText.trim() });
      setPrinciples(prev => [...prev, p]);
      setNewText("");
    } catch (err) { console.error(err); }
    setAdding(false);
  }, [newText]);

  const handleToggle = useCallback(async (p: ApiPrinciple) => {
    try {
      const updated = await updatePrinciple(p.id, { is_active: !p.is_active });
      setPrinciples(prev => prev.map(x => x.id === p.id ? updated : x));
    } catch (err) { console.error(err); }
  }, []);

  const handleSaveEdit = useCallback(async (id: string) => {
    if (!editText.trim()) return;
    try {
      const updated = await updatePrinciple(id, { principle_text: editText.trim() });
      setPrinciples(prev => prev.map(x => x.id === id ? updated : x));
      setEditingId(null);
    } catch (err) { console.error(err); }
  }, [editText]);

  const handleDelete = useCallback(async (id: string) => {
    try {
      await deletePrinciple(id);
      setPrinciples(prev => prev.filter(x => x.id !== id));
    } catch (err) { console.error(err); }
  }, []);

  if (loading) return <div className="flex items-center gap-2 py-8 justify-center text-zinc-400 text-sm"><Spinner /> Loading…</div>;

  const active = principles.filter(p => p.is_active);
  const inactive = principles.filter(p => !p.is_active);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs text-zinc-500">{active.length} active, {inactive.length} inactive</span>
      </div>

      {/* AI: chat to update the brain */}
      <div className="rounded-xl border border-violet-200 dark:border-violet-500/25 bg-violet-50/30 dark:bg-violet-500/5 p-4 space-y-3">
        <div className="flex items-center gap-2">
          <svg className="w-4 h-4 text-violet-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
          </svg>
          <h3 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200">Update with AI</h3>
          <span className="text-[10px] text-zinc-500">
            Describe a change — the AI reviews every principle and proposes edits. Nothing changes until you approve.
          </span>
        </div>

        <textarea
          value={chatInput}
          onChange={(e) => setChatInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !proposing) handlePropose(); }}
          placeholder="e.g. 'Stop using dollar amounts in calendar invites' or 'Always name the webinar host in the description'"
          rows={2}
          className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500 resize-vertical leading-relaxed"
        />

        <div className="flex items-center gap-2">
          <button
            onClick={handlePropose}
            disabled={!chatInput.trim() || proposing}
            className="px-4 py-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5"
          >
            {proposing && <Spinner />} Review changes
          </button>
          <span className="text-[10px] text-zinc-400">⌘/Ctrl + Enter</span>
          {reconcileError && <span className="text-[11px] text-red-500">{reconcileError}</span>}
        </div>

        {/* Proposed changes — review & approve */}
        {proposal && (
          <div className="space-y-2 pt-1 border-t border-violet-200/60 dark:border-violet-500/20">
            {proposal.summary && (
              <p className="text-xs text-zinc-600 dark:text-zinc-300 pt-2">{proposal.summary}</p>
            )}
            {proposal.operations.length === 0 ? (
              <p className="text-xs text-zinc-500">No changes needed — this is already covered by the existing principles.</p>
            ) : (
              <>
                <div className="space-y-1.5">
                  {proposal.operations.map((op, i) => (
                    <div
                      key={i}
                      className={`rounded-lg border p-2.5 transition-opacity ${
                        accepted[i]
                          ? "border-zinc-200 dark:border-zinc-700/50 bg-white dark:bg-zinc-900/60"
                          : "border-zinc-100 dark:border-zinc-800/30 opacity-45"
                      }`}
                    >
                      <div className="flex items-start gap-2">
                        <input
                          type="checkbox"
                          checked={accepted[i] ?? false}
                          onChange={() => setAccepted(a => a.map((v, idx) => idx === i ? !v : v))}
                          className="mt-0.5 accent-violet-600 shrink-0"
                        />
                        <div className="flex-1 min-w-0 space-y-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${OP_BADGE[op.op] ?? ""}`}>
                              {OP_LABEL[op.op] ?? op.op}
                            </span>
                            {op.reason && <span className="text-[11px] text-zinc-500">{op.reason}</span>}
                          </div>
                          {(op.op === "edit" || op.op === "delete") && op.current_text && (
                            <p className="text-xs text-zinc-500 line-through decoration-red-400/60 leading-relaxed">{op.current_text}</p>
                          )}
                          {(op.op === "edit" || op.op === "add") && op.new_text && (
                            <p className="text-xs text-zinc-800 dark:text-zinc-200 leading-relaxed">{op.new_text}</p>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
                <div className="flex items-center gap-2 pt-1">
                  <button
                    onClick={handleApplyProposal}
                    disabled={applying || accepted.every(a => !a)}
                    className="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5"
                  >
                    {applying && <Spinner />} Apply {accepted.filter(Boolean).length} change{accepted.filter(Boolean).length === 1 ? "" : "s"}
                  </button>
                  <button onClick={handleDiscardProposal} className="px-3 py-2 text-xs text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300">Discard</button>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {/* Add new */}
      <div className="flex gap-2">
        <input
          type="text"
          value={newText}
          onChange={(e) => setNewText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleAdd(); }}
          placeholder="Add a new copywriting principle…"
          className="flex-1 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500"
        />
        <button
          onClick={handleAdd}
          disabled={!newText.trim() || adding}
          className="px-3 py-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5"
        >
          {adding && <Spinner />} Add
        </button>
      </div>

      {/* List */}
      <div className="space-y-1.5 max-h-[400px] overflow-y-auto">
        {principles.map((p) => (
          <div
            key={p.id}
            className={`flex items-start gap-2 px-3 py-2 rounded-lg border transition-all ${
              p.is_active
                ? "border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/60"
                : "border-zinc-100 dark:border-zinc-800/20 bg-zinc-50 dark:bg-zinc-900/30 opacity-60"
            }`}
          >
            <button
              onClick={() => handleToggle(p)}
              className={`mt-0.5 shrink-0 w-4 h-4 rounded border flex items-center justify-center transition-colors ${
                p.is_active
                  ? "bg-violet-600 border-violet-600 text-white"
                  : "border-zinc-300 dark:border-zinc-600"
              }`}
            >
              {p.is_active && (
                <svg className="w-2.5 h-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>
              )}
            </button>

            {editingId === p.id ? (
              <div className="flex-1 flex gap-1.5">
                <input
                  type="text"
                  value={editText}
                  onChange={(e) => setEditText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") handleSaveEdit(p.id); if (e.key === "Escape") setEditingId(null); }}
                  className="flex-1 bg-white dark:bg-zinc-800 border border-violet-400 rounded px-2 py-1 text-xs text-zinc-800 dark:text-zinc-200 focus:outline-none"
                  autoFocus
                />
                <button onClick={() => handleSaveEdit(p.id)} className="text-[10px] text-emerald-500 font-medium px-1.5">Save</button>
                <button onClick={() => setEditingId(null)} className="text-[10px] text-zinc-400 font-medium px-1.5">Cancel</button>
              </div>
            ) : (
              <>
                <p className="flex-1 text-xs text-zinc-700 dark:text-zinc-300 leading-relaxed">{p.principle_text}</p>
                <div className="flex items-center gap-1 shrink-0">
                  <button
                    onClick={() => { setEditingId(p.id); setEditText(p.principle_text); }}
                    className="text-[10px] text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 px-1"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => handleDelete(p.id)}
                    className="text-[10px] text-zinc-400 hover:text-red-500 px-1"
                  >
                    Delete
                  </button>
                </div>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ─── Case Studies Tab ────────────────────────────────────────────────── */

type ImportRowStatus = "pending" | "running" | "done" | "error";
type ImportRow = { url: string; status: ImportRowStatus; message?: string };

function CaseStudiesTab() {
  const [studies, setStudies] = useState<ApiCaseStudy[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState({ title: "", client_name: "", industry: "", tags: "", content: "" });
  const [saving, setSaving] = useState(false);

  // URL importer state
  const [urlInput, setUrlInput] = useState("");
  const [notesInput, setNotesInput] = useState("");
  const [bulkMode, setBulkMode] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [importRows, setImportRows] = useState<ImportRow[]>([]);

  // Per-row UI state
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [reextractingId, setReextractingId] = useState<string | null>(null);

  useEffect(() => {
    fetchCaseStudies().then(setStudies).catch(console.error).finally(() => setLoading(false));
  }, []);

  const resetForm = () => {
    setForm({ title: "", client_name: "", industry: "", tags: "", content: "" });
    setShowForm(false);
    setEditingId(null);
  };

  const handleSave = useCallback(async () => {
    if (!form.title.trim() || !form.content.trim()) return;
    setSaving(true);
    const tags = form.tags.split(",").map(t => t.trim()).filter(Boolean);
    try {
      if (editingId) {
        const updated = await apiUpdateCaseStudy(editingId, {
          title: form.title, client_name: form.client_name || undefined,
          industry: form.industry || undefined, tags, content: form.content,
        });
        setStudies(prev => prev.map(s => s.id === editingId ? updated : s));
      } else {
        const created = await apiCreateCaseStudy({
          title: form.title, client_name: form.client_name || undefined,
          industry: form.industry || undefined, tags, content: form.content,
        });
        setStudies(prev => [created, ...prev]);
      }
      resetForm();
    } catch (err) { console.error(err); }
    setSaving(false);
  }, [form, editingId]);

  const handleEdit = (cs: ApiCaseStudy) => {
    setForm({
      title: cs.title || "", client_name: cs.client_name || "",
      industry: cs.industry || "", tags: (cs.tags || []).join(", "), content: cs.content,
    });
    setEditingId(cs.id);
    setShowForm(true);
  };

  const handleToggle = useCallback(async (cs: ApiCaseStudy) => {
    try {
      const updated = await apiUpdateCaseStudy(cs.id, { is_active: !cs.is_active });
      setStudies(prev => prev.map(s => s.id === cs.id ? updated : s));
    } catch (err) { console.error(err); }
  }, []);

  const handleDelete = useCallback(async (id: string) => {
    try {
      await apiDeleteCaseStudy(id);
      setStudies(prev => prev.filter(s => s.id !== id));
    } catch (err) { console.error(err); }
  }, []);

  const handleReextract = useCallback(async (id: string) => {
    setReextractingId(id);
    try {
      const updated = await reextractCaseStudy(id);
      setStudies(prev => prev.map(s => s.id === id ? updated : s));
    } catch (err) {
      alert(err instanceof Error ? err.message : "Re-extract failed");
    } finally {
      setReextractingId(null);
    }
  }, []);

  const handleImportSingle = useCallback(async () => {
    const url = urlInput.trim();
    if (!url) return;
    setImportError(null);
    setImporting(true);
    try {
      const created = await importCaseStudyFromUrl({
        url,
        notes: notesInput.trim() || undefined,
      });
      setStudies(prev => [created, ...prev]);
      setUrlInput("");
      setNotesInput("");
    } catch (err) {
      setImportError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setImporting(false);
    }
  }, [urlInput, notesInput]);

  const handleImportBulk = useCallback(async () => {
    const urls = urlInput
      .split("\n")
      .map(s => s.trim())
      .filter(Boolean);
    if (urls.length === 0) return;
    setImportError(null);
    setImporting(true);
    const initial: ImportRow[] = urls.map(u => ({ url: u, status: "pending" }));
    setImportRows(initial);
    const sharedNotes = notesInput.trim() || undefined;

    for (let i = 0; i < urls.length; i++) {
      setImportRows(prev => prev.map((r, idx) => idx === i ? { ...r, status: "running" } : r));
      try {
        const created = await importCaseStudyFromUrl({ url: urls[i], notes: sharedNotes });
        setStudies(prev => [created, ...prev]);
        setImportRows(prev => prev.map((r, idx) => idx === i ? { ...r, status: "done" } : r));
      } catch (err) {
        setImportRows(prev => prev.map((r, idx) => idx === i
          ? { ...r, status: "error", message: err instanceof Error ? err.message : "Import failed" }
          : r));
      }
    }
    setImporting(false);
  }, [urlInput, notesInput]);

  if (loading) return <div className="flex items-center gap-2 py-8 justify-center text-zinc-400 text-sm"><Spinner /> Loading…</div>;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs text-zinc-500">{studies.filter(s => s.is_active).length} active case studies</span>
        <div className="flex items-center gap-3">
          <button
            onClick={() => { setBulkMode(b => !b); setImportRows([]); setImportError(null); }}
            className="text-[11px] font-medium text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
          >
            {bulkMode ? "Single URL" : "Bulk paste"}
          </button>
          {!showForm && (
            <button
              onClick={() => { resetForm(); setShowForm(true); }}
              className="text-xs font-medium text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 flex items-center gap-1"
            >
              Add manually
            </button>
          )}
        </div>
      </div>

      {/* URL Importer */}
      <div className="rounded-xl border border-violet-200 dark:border-violet-500/25 bg-violet-50/30 dark:bg-violet-500/5 p-4 space-y-3">
        <div className="flex items-center gap-2">
          <svg className="w-4 h-4 text-violet-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
          </svg>
          <h3 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200">
            Import from URL
          </h3>
          <span className="text-[10px] text-zinc-500">
            Fetches the page and extracts client, industry, metrics & story via OpenAI.
          </span>
        </div>

        {bulkMode ? (
          <textarea
            value={urlInput}
            onChange={(e) => setUrlInput(e.target.value)}
            placeholder={"Paste one URL per line\nhttps://qs-institute.com/elevatefinancialpartners\nhttps://qs-institute.com/ken-tyborski--code-of-the-north"}
            rows={6}
            className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500 font-mono leading-relaxed resize-vertical"
          />
        ) : (
          <input
            type="url"
            value={urlInput}
            onChange={(e) => setUrlInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !importing) handleImportSingle(); }}
            placeholder="https://qs-institute.com/elevatefinancialpartners"
            className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500"
          />
        )}

        <input
          type="text"
          value={notesInput}
          onChange={(e) => setNotesInput(e.target.value)}
          placeholder="Optional hints for the extractor (e.g. 'Coaching & Training, focus on revenue lift')"
          className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-xs text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500"
        />

        <div className="flex items-center gap-2">
          <button
            onClick={bulkMode ? handleImportBulk : handleImportSingle}
            disabled={!urlInput.trim() || importing}
            className="px-4 py-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5"
          >
            {importing && <Spinner />}
            {bulkMode ? "Import all" : "Import"}
          </button>
          {importError && (
            <span className="text-[11px] text-red-500">{importError}</span>
          )}
        </div>

        {importRows.length > 0 && (
          <div className="space-y-1 max-h-40 overflow-y-auto pt-1">
            {importRows.map((r, i) => (
              <div key={i} className="flex items-center gap-2 text-[11px]">
                <span className={
                  r.status === "done" ? "text-emerald-500" :
                  r.status === "error" ? "text-red-500" :
                  r.status === "running" ? "text-violet-500" :
                  "text-zinc-400"
                }>
                  {r.status === "done" ? "✓"
                    : r.status === "error" ? "✕"
                    : r.status === "running" ? "…"
                    : "·"}
                </span>
                <span className="font-mono truncate flex-1 text-zinc-600 dark:text-zinc-400">{r.url}</span>
                {r.message && <span className="text-red-500 truncate max-w-xs" title={r.message}>{r.message}</span>}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Add/Edit Form (manual fallback) */}
      {showForm && (
        <div className="rounded-xl border border-violet-200 dark:border-violet-500/25 bg-violet-50/30 dark:bg-violet-500/5 p-4 space-y-3">
          <div className="grid grid-cols-3 gap-2">
            <input value={form.title} onChange={(e) => setForm(f => ({ ...f, title: e.target.value }))}
              placeholder="Title *" className="col-span-1 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500" />
            <input value={form.client_name} onChange={(e) => setForm(f => ({ ...f, client_name: e.target.value }))}
              placeholder="Client name" className="col-span-1 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500" />
            <input value={form.industry} onChange={(e) => setForm(f => ({ ...f, industry: e.target.value }))}
              placeholder="Industry (for matching)" className="col-span-1 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500" />
          </div>
          <input value={form.tags} onChange={(e) => setForm(f => ({ ...f, tags: e.target.value }))}
            placeholder="Tags (comma-separated, e.g. SaaS, B2B, coaching)" className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500" />
          <textarea value={form.content} onChange={(e) => setForm(f => ({ ...f, content: e.target.value }))}
            placeholder="Case study content — include client results, metrics, story… *" rows={5}
            className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500 resize-vertical" />
          <div className="flex gap-2">
            <button onClick={handleSave} disabled={!form.title.trim() || !form.content.trim() || saving}
              className="px-4 py-2 bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white text-xs font-semibold rounded-lg transition-colors flex items-center gap-1.5">
              {saving && <Spinner />} {editingId ? "Update" : "Add"} case study
            </button>
            <button onClick={resetForm} className="px-3 py-2 text-xs text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300">Cancel</button>
          </div>
        </div>
      )}

      {/* List */}
      <div className="space-y-2 max-h-[400px] overflow-y-auto">
        {studies.map((cs) => {
          const isExpanded = expandedId === cs.id;
          const s = cs.structured;
          const hasStructured = !!(s && (s.quote || (s.metrics && s.metrics.length) || (s.pain_points && s.pain_points.length) || (s.outcomes && s.outcomes.length) || (s.persona && Object.keys(s.persona).length)));
          return (
            <div
              key={cs.id}
              className={`rounded-xl border px-4 py-3 transition-all ${
                cs.is_active
                  ? "border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900/60"
                  : "border-zinc-100 dark:border-zinc-800/20 bg-zinc-50 dark:bg-zinc-900/30 opacity-60"
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <h4 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200 truncate">{cs.title}</h4>
                    {cs.source_url && (
                      <a
                        href={cs.source_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="text-[10px] text-violet-500 hover:text-violet-400"
                        title={cs.source_url}
                      >
                        ↗ source
                      </a>
                    )}
                    {hasStructured && (
                      <span className="text-[9px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-emerald-100 dark:bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
                        Structured
                      </span>
                    )}
                    {cs.client_name && <span className="text-[10px] text-zinc-500">{cs.client_name}</span>}
                    {cs.industry && (
                      <span className="text-[9px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-500/15 text-blue-600 dark:text-blue-400">
                        {cs.industry}
                      </span>
                    )}
                    {(cs.tags || []).map(tag => (
                      <span key={tag} className="text-[9px] font-medium px-1.5 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800/60 text-zinc-500 dark:text-zinc-400">
                        {tag}
                      </span>
                    ))}
                  </div>
                  {!isExpanded && (
                    <p className="text-xs text-zinc-500 dark:text-zinc-400 mt-1 line-clamp-2 leading-relaxed">{cs.content}</p>
                  )}
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <button
                    onClick={() => setExpandedId(isExpanded ? null : cs.id)}
                    className="text-[10px] text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 px-1"
                  >
                    {isExpanded ? "Collapse" : "Expand"}
                  </button>
                  {cs.source_url && (
                    <button
                      onClick={() => handleReextract(cs.id)}
                      disabled={reextractingId === cs.id}
                      title="Re-fetch the source URL and re-run extraction"
                      className="text-[10px] text-violet-500 hover:text-violet-400 disabled:opacity-50 px-1 flex items-center gap-1"
                    >
                      {reextractingId === cs.id && <Spinner />}
                      Re-extract
                    </button>
                  )}
                  <button onClick={() => handleToggle(cs)}
                    className={`text-[10px] font-medium px-1.5 py-0.5 rounded transition-colors ${cs.is_active ? "text-amber-500 hover:bg-amber-50 dark:hover:bg-amber-500/10" : "text-emerald-500 hover:bg-emerald-50 dark:hover:bg-emerald-500/10"}`}>
                    {cs.is_active ? "Disable" : "Enable"}
                  </button>
                  <button onClick={() => handleEdit(cs)} className="text-[10px] text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 px-1">Edit</button>
                  <button onClick={() => handleDelete(cs.id)} className="text-[10px] text-zinc-400 hover:text-red-500 px-1">Delete</button>
                </div>
              </div>

              {isExpanded && (
                <div className="mt-3 pt-3 border-t border-zinc-100 dark:border-zinc-800/40 space-y-3 text-xs">
                  {s?.headline && (
                    <Field label="Offer / program on the page" value={s.headline} />
                  )}

                  {s?.industry_aliases && s.industry_aliases.length > 0 && (
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">Industry synonyms (used for matching)</div>
                      <div className="flex flex-wrap gap-1">
                        {s.industry_aliases.map(a => (
                          <span key={a} className="text-[10px] px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400">
                            {a}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  {s?.persona && (s.persona.role || s.persona.company_size || s.persona.target_market) && (
                    <div className="grid grid-cols-3 gap-2">
                      {s.persona.role && (
                        <Field label="Role" value={s.persona.role} />
                      )}
                      {s.persona.company_size && (
                        <Field label="Company size" value={s.persona.company_size} />
                      )}
                      {s.persona.target_market && (
                        <Field label="Target market" value={s.persona.target_market} />
                      )}
                    </div>
                  )}

                  {s?.metrics && s.metrics.length > 0 && (
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">Metrics</div>
                      <div className="space-y-0.5">
                        {s.metrics.map((m, i) => (
                          <div key={i} className="flex items-center gap-2 text-zinc-700 dark:text-zinc-300">
                            <span className="text-zinc-500 min-w-0 flex-1 truncate">{m.label}</span>
                            <span className="font-mono text-zinc-400">{m.before || "—"}</span>
                            <span className="text-zinc-400">→</span>
                            <span className="font-mono font-semibold text-emerald-600 dark:text-emerald-400">{m.after || "—"}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {s?.pain_points && s.pain_points.length > 0 && (
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">Pain points</div>
                      <ul className="list-disc list-inside text-zinc-600 dark:text-zinc-400 space-y-0.5">
                        {s.pain_points.map((p, i) => <li key={i}>{p}</li>)}
                      </ul>
                    </div>
                  )}

                  {s?.outcomes && s.outcomes.length > 0 && (
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">Outcomes</div>
                      <ul className="list-disc list-inside text-zinc-600 dark:text-zinc-400 space-y-0.5">
                        {s.outcomes.map((o, i) => <li key={i}>{o}</li>)}
                      </ul>
                    </div>
                  )}

                  {s?.quote && (
                    <blockquote className="border-l-2 border-violet-400 pl-3 text-zinc-700 dark:text-zinc-300 italic leading-relaxed">
                      “{s.quote}”
                    </blockquote>
                  )}

                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-semibold mb-1">Narrative</div>
                    <p className="text-zinc-600 dark:text-zinc-400 leading-relaxed whitespace-pre-line">{cs.content}</p>
                  </div>
                </div>
              )}
            </div>
          );
        })}
        {studies.length === 0 && (
          <div className="text-center py-8 text-zinc-400 text-sm">No case studies yet. Add one to use in copy generation.</div>
        )}
      </div>
    </div>
  );
}

/* ─── Brain Content Tab ───────────────────────────────────────────────── */

function BrainContentTab() {
  const [content, setContent] = useState<ApiBrainContent | null>(null);
  const [loading, setLoading] = useState(true);
  const [universalText, setUniversalText] = useState("");
  const [formatText, setFormatText] = useState("");
  const [savingU, setSavingU] = useState(false);
  const [savingF, setSavingF] = useState(false);
  const [savedU, setSavedU] = useState(false);
  const [savedF, setSavedF] = useState(false);

  useEffect(() => {
    fetchBrainContent().then(c => {
      setContent(c);
      setUniversalText(c.universal_brain);
      setFormatText(c.format_brain);
    }).catch(console.error).finally(() => setLoading(false));
  }, []);

  const handleSaveUniversal = useCallback(async () => {
    setSavingU(true);
    try {
      await updateUniversalBrain(universalText);
      setSavedU(true);
      setTimeout(() => setSavedU(false), 2000);
    } catch (err) { console.error(err); }
    setSavingU(false);
  }, [universalText]);

  const handleSaveFormat = useCallback(async () => {
    setSavingF(true);
    try {
      await updateFormatBrain(formatText);
      setSavedF(true);
      setTimeout(() => setSavedF(false), 2000);
    } catch (err) { console.error(err); }
    setSavingF(false);
  }, [formatText]);

  if (loading) return <div className="flex items-center gap-2 py-8 justify-center text-zinc-400 text-sm"><Spinner /> Loading…</div>;

  const universalChanged = universalText !== (content?.universal_brain ?? "");
  const formatChanged = formatText !== (content?.format_brain ?? "");

  return (
    <div className="space-y-4">
      {/* Universal Brain */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <label className="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold">Business Context (Universal Brain)</label>
          <button onClick={handleSaveUniversal} disabled={!universalChanged || savingU}
            className="text-xs font-medium px-3 py-1 rounded-md bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white transition-colors flex items-center gap-1.5">
            {savingU && <Spinner />} {savedU ? "Saved!" : "Save"}
          </button>
        </div>
        <textarea
          value={universalText}
          onChange={(e) => setUniversalText(e.target.value)}
          rows={8}
          placeholder="Describe your business, offer, target audience, unique selling points…"
          className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-violet-500 resize-vertical font-mono leading-relaxed"
        />
      </div>

      {/* Format Brain */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <label className="text-[10px] text-zinc-500 uppercase tracking-wider font-semibold">Format Rules (Calendar Invite Structure)</label>
          <button onClick={handleSaveFormat} disabled={!formatChanged || savingF}
            className="text-xs font-medium px-3 py-1 rounded-md bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white transition-colors flex items-center gap-1.5">
            {savingF && <Spinner />} {savedF ? "Saved!" : "Save"}
          </button>
        </div>
        <textarea
          value={formatText}
          onChange={(e) => setFormatText(e.target.value)}
          rows={8}
          placeholder="Define the structure and format rules for calendar invite descriptions…"
          className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-blue-500 resize-vertical font-mono leading-relaxed"
        />
      </div>
    </div>
  );
}

/* ─── Main BrainPanel ─────────────────────────────────────────────────── */

type BrainTab = "principles" | "case-studies" | "content";

export function BrainPanel() {
  const [open, setOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<BrainTab>("principles");

  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900 overflow-hidden shadow-sm">
      {/* Toggle Header */}
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-5 py-3 hover:bg-zinc-50 dark:hover:bg-zinc-800/20 transition-colors"
      >
        <div className="flex items-center gap-2.5">
          <span className="text-sm font-bold text-zinc-800 dark:text-zinc-200">Copy Brain</span>
          <span className="text-[10px] text-zinc-400">Principles, case studies & business context</span>
        </div>
        <svg className={`w-4 h-4 text-zinc-400 transition-transform ${open ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Collapsible Content */}
      {open && (
        <div className="border-t border-zinc-200 dark:border-zinc-800/40">
          {/* Tab Bar */}
          <div className="flex items-center gap-1 px-5 py-2.5 border-b border-zinc-100 dark:border-zinc-800/30">
            {([
              { key: "principles" as BrainTab, label: "Principles" },
              { key: "case-studies" as BrainTab, label: "Case Studies" },
              { key: "content" as BrainTab, label: "Brain Content" },
            ]).map(tab => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={`px-4 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                  activeTab === tab.key
                    ? "bg-violet-100 dark:bg-violet-500/15 text-violet-600 dark:text-violet-400 border border-violet-200 dark:border-violet-500/25"
                    : "text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800/50"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Tab Content */}
          <div className="px-5 py-4">
            {activeTab === "principles" && <PrinciplesTab />}
            {activeTab === "case-studies" && <CaseStudiesTab />}
            {activeTab === "content" && <BrainContentTab />}
          </div>
        </div>
      )}
    </div>
  );
}
