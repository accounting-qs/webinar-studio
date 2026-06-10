"use client";

import { useEffect, useState } from "react";

export type EditableSender = {
  id: string;
  name: string;
  accounts: number;
  sendPerAccount: number;
  daysPerWeek: number;
  color: string;
};

export type SenderNumField = "accounts" | "sendPerAccount" | "daysPerWeek";

// Small colored dot per sender color token (mirrors the badge palette).
const DOT_COLOR: Record<string, string> = {
  violet: "bg-violet-500",
  blue: "bg-blue-500",
  emerald: "bg-emerald-500",
  amber: "bg-amber-500",
  cyan: "bg-cyan-500",
  rose: "bg-rose-500",
  orange: "bg-orange-500",
  teal: "bg-teal-500",
  pink: "bg-pink-500",
  indigo: "bg-indigo-500",
};

const NUM_INPUT_CLS =
  "w-full bg-zinc-100 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2 py-1.5 text-sm text-zinc-800 dark:text-zinc-200 font-mono text-center focus:outline-none focus:ring-1 focus:ring-violet-500";

const FIELDS: { key: SenderNumField; label: string }[] = [
  { key: "accounts", label: "Accounts" },
  { key: "sendPerAccount", label: "Send / Acct" },
  { key: "daysPerWeek", label: "Days / Webinar" },
];

/** Modal for managing outreach senders — one row per sender, stacked
 * vertically, with a cleaner layout than the old inline horizontal bar. */
export function SendersEditModal({
  senders,
  onClose,
  onRename,
  onUpdateField,
  onAddSender,
}: {
  senders: EditableSender[];
  onClose: () => void;
  onRename: (id: string, name: string) => void;
  onUpdateField: (id: string, field: SenderNumField, value: number) => void;
  onAddSender: (data: { name: string; accounts: number; sendPerAccount: number; daysPerWeek: number }) => Promise<void>;
}) {
  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState("");
  const [newAccounts, setNewAccounts] = useState(5);
  const [newSendPerAcct, setNewSendPerAcct] = useState(50);
  const [newDaysPerWeb, setNewDaysPerWeb] = useState(5);
  const [creating, setCreating] = useState(false);

  // Escape to close + lock body scroll while open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
  }, [onClose]);

  const handleAdd = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      await onAddSender({
        name: newName.trim(),
        accounts: newAccounts,
        sendPerAccount: newSendPerAcct,
        daysPerWeek: newDaysPerWeb,
      });
      setNewName("");
      setNewAccounts(5);
      setNewSendPerAcct(50);
      setNewDaysPerWeb(5);
      setShowNew(false);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to create sender");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-lg w-full max-h-[85vh] overflow-y-auto"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800 sticky top-0 bg-white dark:bg-zinc-900 z-10">
          <div>
            <h3 className="text-base font-bold text-zinc-900 dark:text-zinc-100">Senders</h3>
            <p className="text-[11px] text-zinc-500 mt-0.5">{senders.length} sender{senders.length === 1 ? "" : "s"} · changes save automatically</p>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
            aria-label="Close"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        {/* Sender list — stacked vertically */}
        <div className="px-6 py-5 space-y-3">
          {senders.map((s) => (
            <div
              key={s.id}
              className="rounded-lg border border-zinc-200 dark:border-zinc-800/60 bg-zinc-50/60 dark:bg-zinc-900/40 px-4 py-3"
            >
              <div className="flex items-center gap-2.5 mb-3">
                <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${DOT_COLOR[s.color] ?? "bg-zinc-400"}`} />
                <input
                  type="text"
                  defaultValue={s.name}
                  key={`name-${s.id}-${s.name}`}
                  onBlur={(e) => {
                    const v = e.target.value.trim();
                    if (v && v !== s.name) onRename(s.id, v);
                  }}
                  onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                  className="flex-1 min-w-0 bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2.5 py-1.5 text-sm font-semibold text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
                />
                <span className="text-sm text-violet-600 dark:text-violet-400 font-mono font-bold whitespace-nowrap">
                  = {(s.accounts * s.sendPerAccount).toLocaleString()}/d
                </span>
              </div>
              <div className="grid grid-cols-3 gap-2">
                {FIELDS.map((f) => (
                  <div key={f.key}>
                    <label className="block text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide mb-1">{f.label}</label>
                    <input
                      type="number"
                      key={`${f.key}-${s.id}-${s[f.key]}`}
                      defaultValue={s[f.key]}
                      onBlur={(e) => {
                        const v = parseInt(e.target.value) || 0;
                        if (v !== s[f.key]) onUpdateField(s.id, f.key, v);
                      }}
                      onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                      className={NUM_INPUT_CLS}
                    />
                  </div>
                ))}
              </div>
            </div>
          ))}

          {/* Add sender */}
          {!showNew ? (
            <button
              onClick={() => setShowNew(true)}
              className="w-full flex items-center justify-center gap-1.5 px-3 py-2.5 border-2 border-dashed border-zinc-300 dark:border-zinc-700 rounded-lg text-xs text-zinc-500 hover:text-violet-400 hover:border-violet-500/40 transition-colors"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>
              Add Sender
            </button>
          ) : (
            <div className="rounded-lg border border-violet-200 dark:border-violet-500/20 bg-violet-50 dark:bg-violet-500/5 px-4 py-3 space-y-3">
              <div>
                <label className="block text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide mb-1">Name</label>
                <input
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="Sender name…"
                  autoFocus
                  className="w-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-2.5 py-1.5 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-violet-500"
                />
              </div>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="block text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide mb-1">Accounts</label>
                  <input type="number" value={newAccounts} onChange={(e) => setNewAccounts(parseInt(e.target.value) || 0)} className={NUM_INPUT_CLS} />
                </div>
                <div>
                  <label className="block text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide mb-1">Send / Acct</label>
                  <input type="number" value={newSendPerAcct} onChange={(e) => setNewSendPerAcct(parseInt(e.target.value) || 0)} className={NUM_INPUT_CLS} />
                </div>
                <div>
                  <label className="block text-[10px] text-zinc-500 dark:text-zinc-400 uppercase tracking-wide mb-1">Days / Webinar</label>
                  <input type="number" value={newDaysPerWeb} onChange={(e) => setNewDaysPerWeb(parseInt(e.target.value) || 0)} className={NUM_INPUT_CLS} />
                </div>
              </div>
              <div className="flex items-center justify-end gap-2">
                <button
                  onClick={() => setShowNew(false)}
                  className="px-3 py-1.5 text-xs text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleAdd}
                  disabled={!newName.trim() || creating}
                  className="px-4 py-1.5 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white text-xs font-semibold rounded-md transition-colors"
                >
                  {creating ? "Adding…" : "Add Sender"}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
