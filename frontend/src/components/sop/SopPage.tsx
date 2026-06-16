"use client";

import { useEffect, useState } from "react";
import { fetchSopVideos, updateSopVideo, type ApiSopVideo } from "@/lib/api";

const INPUT_CLS =
  "w-full bg-zinc-50 dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700/60 rounded-lg px-3 py-2.5 text-sm text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-violet-500/50 transition-colors";
const LABEL_CLS = "text-[11px] text-zinc-500 uppercase tracking-wider font-medium block mb-1.5";

/** Turn a Loom share/embed URL into an embeddable src. Returns null when the
 * URL isn't a recognizable Loom link (so we can show a fallback instead). */
function loomEmbedSrc(url: string): string | null {
  const m = url.match(/loom\.com\/(?:share|embed)\/([A-Za-z0-9]+)/);
  return m ? `https://www.loom.com/embed/${m[1]}` : null;
}

export function SopPage() {
  const [videos, setVideos] = useState<ApiSopVideo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<ApiSopVideo | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setVideos(await fetchSopVideos());
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load SOPs");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const onSaved = (updated: ApiSopVideo) =>
    setVideos((prev) => prev.map((v) => (v.id === updated.id ? updated : v)));

  return (
    <main className="min-h-[calc(100vh-48px)] bg-zinc-50 dark:bg-zinc-950">
      <div className="px-6 py-8 max-w-[900px] mx-auto">
        <header className="mb-6">
          <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">SOP</h1>
          <p className="text-sm text-zinc-500 mt-1">
            Standard operating procedures — watch how Webinar Studio works.
          </p>
        </header>

        {loading && <div className="text-sm text-zinc-500">Loading…</div>}
        {error && !loading && (
          <div className="text-sm text-rose-500 border border-rose-200 dark:border-rose-500/25 bg-rose-50 dark:bg-rose-500/10 rounded-lg px-4 py-3">
            {error}
          </div>
        )}

        <div className="space-y-6">
          {videos.map((v) => {
            const src = loomEmbedSrc(v.loom_url);
            return (
              <section
                key={v.id}
                className="rounded-xl border border-zinc-200 dark:border-zinc-800/40 bg-white dark:bg-zinc-900 overflow-hidden shadow-sm"
              >
                <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-zinc-200 dark:border-zinc-800/40">
                  <div>
                    <h2 className="text-base font-bold text-zinc-900 dark:text-zinc-100">{v.title}</h2>
                    {v.subtitle && <p className="text-sm text-zinc-500 mt-0.5">{v.subtitle}</p>}
                  </div>
                  <button
                    onClick={() => setEditing(v)}
                    className="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 hover:bg-zinc-100 dark:hover:bg-zinc-800 rounded-md transition-colors"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                    </svg>
                    Edit
                  </button>
                </div>
                {src ? (
                  <div className="relative aspect-video bg-black">
                    <iframe
                      src={src}
                      className="absolute inset-0 w-full h-full"
                      allowFullScreen
                      title={v.title}
                    />
                  </div>
                ) : (
                  <div className="px-5 py-8 text-sm text-zinc-500">
                    {v.loom_url ? (
                      <>
                        Not a recognized Loom link.{" "}
                        <a href={v.loom_url} target="_blank" rel="noreferrer" className="text-violet-500 hover:underline">
                          Open link
                        </a>
                      </>
                    ) : (
                      "No video set yet — click Edit to add a Loom link."
                    )}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      </div>

      {editing && (
        <SopEditModal video={editing} onClose={() => setEditing(null)} onSaved={onSaved} />
      )}
    </main>
  );
}

function SopEditModal({
  video, onClose, onSaved,
}: {
  video: ApiSopVideo;
  onClose: () => void;
  onSaved: (updated: ApiSopVideo) => void;
}) {
  const [title, setTitle] = useState(video.title);
  const [subtitle, setSubtitle] = useState(video.subtitle);
  const [loomUrl, setLoomUrl] = useState(video.loom_url);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
  }, [onClose]);

  const save = async () => {
    setSaving(true);
    try {
      const updated = await updateSopVideo(video.id, {
        title: title.trim(),
        subtitle: subtitle.trim(),
        loom_url: loomUrl.trim(),
      });
      onSaved(updated);
      onClose();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4 py-8" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded-xl shadow-2xl max-w-md w-full">
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-800">
          <h3 className="text-base font-bold text-zinc-900 dark:text-zinc-100">Edit SOP</h3>
          <button onClick={onClose} className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors" aria-label="Close">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        <div className="px-6 py-5 space-y-4">
          <div>
            <label className={LABEL_CLS}>Title</label>
            <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} className={INPUT_CLS} autoFocus />
          </div>
          <div>
            <label className={LABEL_CLS}>Subtitle</label>
            <input type="text" value={subtitle} onChange={(e) => setSubtitle(e.target.value)} className={INPUT_CLS} />
          </div>
          <div>
            <label className={LABEL_CLS}>Loom Link</label>
            <input type="url" value={loomUrl} onChange={(e) => setLoomUrl(e.target.value)} placeholder="https://www.loom.com/share/…" className={INPUT_CLS} />
            <div className="mt-1.5 text-[10px] text-zinc-500">Paste the Loom share link — it embeds and plays right on this page.</div>
          </div>
        </div>

        <div className="px-6 py-4 border-t border-zinc-200 dark:border-zinc-800/40 flex items-center justify-between">
          <button onClick={onClose} className="px-4 py-2 text-sm text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 transition-colors">Cancel</button>
          <button onClick={save} disabled={saving || !title.trim()}
            className="px-5 py-2 bg-violet-600 hover:bg-violet-500 disabled:bg-zinc-300 dark:disabled:bg-zinc-700 disabled:text-zinc-500 text-white text-sm font-semibold rounded-lg transition-colors">
            {saving ? "Saving…" : "Save Changes"}
          </button>
        </div>
      </div>
    </div>
  );
}
