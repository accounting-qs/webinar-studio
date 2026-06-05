"use client";

import { useEffect, useState } from "react";
import {
  fetchWgCredentials, fetchWgWebinars, refreshWgWebinars, updateWebinar,
  type ApiWebinar, type ApiWgCredential, type WgWebinar,
} from "@/lib/api";

/** Flat, camelCase editable view of a webinar — both Planning and Statistics
 * map their own webinar shape into this before opening the modal. */
export type EditableWebinar = {
  id: string;
  number: number;
  isoDate: string;                  // YYYY-MM-DD
  broadcastId: string;              // "" when none
  webinargeekCredentialId: string;  // "" → default credential
  nonjoinerSourceWebinarId: string; // "" when none
  status: string;                   // lowercase
  registrationLink: string;
  unsubscribeLink: string;
  variantLabel: string;
};

type WebinarOption = { id: string; number: number; variantLabel: string | null; date: string | null };

const SELECT_CLS =
  "w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors";
const INPUT_CLS = SELECT_CLS + " placeholder-zinc-500";
const LABEL_CLS = "text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5";

function fmtDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleDateString();
}

/** Shared "Edit Webinar" modal — WebinarGeek account/broadcast picker, the
 * Nonjoiner source (previous webinar), and the basic webinar fields. Used by
 * the Planning and Statistics pages. Saves with one PUT and hands the updated
 * row back via onSaved. */
export function WebinarEditModal({
  webinar, allWebinars, onClose, onSaved,
}: {
  webinar: EditableWebinar;
  allWebinars: WebinarOption[];
  onClose: () => void;
  onSaved: (updated: ApiWebinar) => void;
}) {
  const [edit, setEdit] = useState<EditableWebinar>(webinar);
  const [creds, setCreds] = useState<ApiWgCredential[]>([]);
  const [broadcasts, setBroadcasts] = useState<WgWebinar[]>([]);
  const [bcLoading, setBcLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  // Escape to close + body scroll lock.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
  }, [onClose]);

  const loadBroadcasts = async (credId: string | undefined, refresh: boolean) => {
    setBcLoading(true);
    try {
      if (refresh) { try { await refreshWgWebinars(); } catch (e) { console.error("WG refresh failed", e); } }
      const { broadcasts } = await fetchWgWebinars({ credential_id: credId, limit: 500 });
      setBroadcasts(broadcasts);
    } catch (e) {
      console.error("Failed to load broadcasts", e);
      setBroadcasts([]);
    } finally {
      setBcLoading(false);
    }
  };

  // On open: load WG accounts, then refresh + load this account's broadcasts.
  useEffect(() => {
    (async () => {
      let list: ApiWgCredential[] = [];
      try { list = (await fetchWgCredentials()).credentials; } catch (e) { console.error("Failed to load WG credentials", e); }
      setCreds(list);
      const credId = webinar.webinargeekCredentialId || list.find((c) => c.name === "default")?.id;
      loadBroadcasts(credId, true);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    const changes: Parameters<typeof updateWebinar>[1] = {};
    if (edit.number !== webinar.number) changes.number = edit.number;
    if (edit.isoDate !== webinar.isoDate) changes.date = edit.isoDate;
    if (edit.variantLabel.trim() !== webinar.variantLabel.trim()) changes.variant_label = edit.variantLabel.trim() || null;
    if (edit.broadcastId !== webinar.broadcastId) changes.broadcast_id = edit.broadcastId;
    if (edit.webinargeekCredentialId !== webinar.webinargeekCredentialId) changes.webinargeek_credential_id = edit.webinargeekCredentialId || null;
    if (edit.nonjoinerSourceWebinarId !== webinar.nonjoinerSourceWebinarId) changes.nonjoiner_source_webinar_id = edit.nonjoinerSourceWebinarId || null;
    if (edit.registrationLink !== webinar.registrationLink) changes.registration_link = edit.registrationLink;
    if (edit.unsubscribeLink !== webinar.unsubscribeLink) changes.unsubscribe_link = edit.unsubscribeLink;
    if (edit.status !== webinar.status) changes.status = edit.status;

    if (Object.keys(changes).length === 0) { onClose(); return; }
    setSaving(true);
    try {
      const updated = await updateWebinar(webinar.id, changes);
      onSaved(updated);
      onClose();
    } catch (e) {
      console.error("Failed to save webinar", e);
      alert(e instanceof Error ? e.message : "Failed to save webinar");
    } finally {
      setSaving(false);
    }
  };

  const prevOptions = allWebinars.filter((w) => w.id !== webinar.id).sort((a, b) => b.number - a.number);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-md w-full max-h-[88vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800 sticky top-0 bg-white dark:bg-zinc-900 z-10">
          <h3 className="text-base font-bold text-zinc-900 dark:text-zinc-100">Edit Webinar {edit.number || ""}</h3>
          <button onClick={onClose} className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors" aria-label="Close">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        <div className="px-6 py-5 space-y-4">
          <div>
            <label className={LABEL_CLS}>Webinar Number</label>
            <input type="number" value={edit.number}
              onChange={(e) => setEdit({ ...edit, number: parseInt(e.target.value) || 0 })}
              className={INPUT_CLS + " font-mono"} autoFocus />
          </div>
          <div>
            <label className={LABEL_CLS}>Webinar Date</label>
            <input type="date" value={edit.isoDate}
              onChange={(e) => setEdit({ ...edit, isoDate: e.target.value })}
              onClick={(e) => (e.target as HTMLInputElement).showPicker?.()}
              className={INPUT_CLS + " [color-scheme:dark] cursor-pointer [&::-webkit-calendar-picker-indicator]:opacity-70 [&::-webkit-calendar-picker-indicator]:invert [&::-webkit-calendar-picker-indicator]:cursor-pointer"} />
          </div>
          <div>
            <label className={LABEL_CLS}>Variant Label <span className="text-zinc-500 normal-case font-normal">(required for A/B variants)</span></label>
            <input type="text" value={edit.variantLabel}
              onChange={(e) => setEdit({ ...edit, variantLabel: e.target.value })}
              placeholder='e.g. "Account A" / "WG-Skarpe"' className={INPUT_CLS} />
          </div>

          <div>
            <label className={LABEL_CLS}>WebinarGeek Account</label>
            <select value={edit.webinargeekCredentialId}
              onChange={(e) => {
                const credId = e.target.value;
                setEdit({ ...edit, webinargeekCredentialId: credId, broadcastId: "" });
                loadBroadcasts(credId || creds.find((c) => c.name === "default")?.id, false);
              }}
              className={SELECT_CLS}>
              <option value="">Default credential</option>
              {creds.filter((c) => c.name !== "default").map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          </div>
          <div>
            <label className={LABEL_CLS + " flex items-center gap-2"}>
              WebinarGeek Broadcast
              {bcLoading && <span className="text-zinc-500 normal-case font-normal tracking-normal">loading…</span>}
            </label>
            <select value={edit.broadcastId} disabled={bcLoading}
              onChange={(e) => {
                const bid = e.target.value;
                const b = broadcasts.find((x) => x.broadcast_id === bid);
                setEdit({ ...edit, broadcastId: bid, isoDate: b?.starts_at ? new Date(b.starts_at).toISOString().slice(0, 10) : edit.isoDate });
              }}
              className={SELECT_CLS + " disabled:opacity-60"}>
              <option value="">— None —</option>
              {edit.broadcastId && !broadcasts.some((b) => b.broadcast_id === edit.broadcastId) && (
                <option value={edit.broadcastId}>Current · {edit.broadcastId}</option>
              )}
              {broadcasts.map((b) => (
                <option key={b.broadcast_id} value={b.broadcast_id}>
                  {(b.internal_title || b.name || `Broadcast ${b.broadcast_id}`)}{b.starts_at ? ` · ${new Date(b.starts_at).toLocaleDateString()}` : ""}
                </option>
              ))}
            </select>
            <div className="mt-1.5 text-[10px] text-zinc-500">Subscribers auto-sync once, ~2h after the broadcast start time. Picking a broadcast fills the date above (still editable).</div>
          </div>

          <div>
            <label className={LABEL_CLS}>Previous Webinar <span className="text-zinc-500 normal-case font-normal">(source for the Nonjoiners list)</span></label>
            <select value={edit.nonjoinerSourceWebinarId}
              onChange={(e) => setEdit({ ...edit, nonjoinerSourceWebinarId: e.target.value })}
              className={SELECT_CLS}>
              <option value="">— None (use GHL non-joiners) —</option>
              {prevOptions.map((w) => (
                <option key={w.id} value={w.id}>
                  W{w.number}{w.variantLabel ? ` · ${w.variantLabel}` : ""}{w.date ? ` — ${fmtDate(w.date)}` : ""}
                </option>
              ))}
            </select>
            <div className="mt-1.5 text-[10px] text-zinc-500">Nonjoiners = that webinar&apos;s broadcast registrants who did NOT watch live.</div>
          </div>

          <div>
            <label className={LABEL_CLS}>Registration Link</label>
            <input type="url" value={edit.registrationLink}
              onChange={(e) => setEdit({ ...edit, registrationLink: e.target.value })}
              placeholder="https://..." className={INPUT_CLS} />
          </div>
          <div>
            <label className={LABEL_CLS}>Unsubscribe Link</label>
            <input type="url" value={edit.unsubscribeLink}
              onChange={(e) => setEdit({ ...edit, unsubscribeLink: e.target.value })}
              placeholder="https://..." className={INPUT_CLS} />
          </div>
          <div>
            <label className={LABEL_CLS}>Status</label>
            <select value={edit.status} onChange={(e) => setEdit({ ...edit, status: e.target.value })} className={SELECT_CLS}>
              <option value="planning">Planning</option>
              <option value="sent">Sent</option>
              <option value="archived">Archived</option>
            </select>
          </div>
        </div>

        <div className="px-6 py-4 border-t border-zinc-200 dark:border-zinc-800/40 flex items-center justify-between sticky bottom-0 bg-white dark:bg-zinc-900">
          <button onClick={onClose} className="px-4 py-2 text-sm text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 transition-colors">Cancel</button>
          <button onClick={save} disabled={saving || !edit.number || !edit.isoDate}
            className="px-5 py-2 bg-violet-600 hover:bg-violet-500 disabled:bg-zinc-300 dark:disabled:bg-zinc-700 disabled:text-zinc-500 text-white text-sm font-semibold rounded-lg transition-colors">
            {saving ? "Saving…" : "Save Changes"}
          </button>
        </div>
      </div>
    </div>
  );
}
