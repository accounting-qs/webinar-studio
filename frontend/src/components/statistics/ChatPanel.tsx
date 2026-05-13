"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { streamChat, type ChatTurn, type ChatUsage } from "@/lib/chat";
import type { ApiStatisticsWebinar } from "@/lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Whatever the Statistics page has loaded right now — full webinars array
   * including summary + per-list rows. The panel passes this through to
   * Claude as a cached system block on every turn so the agent always sees
   * the same data the operator is looking at. */
  webinars: ApiStatisticsWebinar[];
}

interface Turn extends ChatTurn {
  /** Streaming flag — true on an assistant turn while text is still arriving. */
  streaming?: boolean;
  /** Final-turn token counts. Render `cache_read_input_tokens > 0` as a tiny
   * hint so the operator can confirm prompt caching is working. */
  usage?: ChatUsage;
}

// Panel width — operator-resizable via the left drag handle, persisted to
// localStorage so it's stable across page loads.
const WIDTH_STORAGE_KEY = "stats-chat-panel-width-px";
const DEFAULT_WIDTH = 420;
const MIN_WIDTH = 320;
// Hard cap so the panel can't fully cover the table. Drag is also clamped
// to 90% of viewport at drag time (handles narrow windows / window resize).
const MAX_WIDTH = 1100;

function readSavedWidth(): number {
  if (typeof window === "undefined") return DEFAULT_WIDTH;
  try {
    const raw = window.localStorage.getItem(WIDTH_STORAGE_KEY);
    const parsed = raw ? parseInt(raw, 10) : NaN;
    if (Number.isFinite(parsed) && parsed >= MIN_WIDTH) {
      return Math.min(parsed, MAX_WIDTH);
    }
  } catch {
    /* localStorage blocked — fall back to default */
  }
  return DEFAULT_WIDTH;
}

// Conversation persistence — survives both panel close/reopen (component
// unmount) and full page reloads. The `streaming` flag is stripped on save
// since any half-streamed turn is stale by the next mount.
const TURNS_STORAGE_KEY = "stats-chat-panel-turns";

function readSavedTurns(): Turn[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(TURNS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((t) => t && (t.role === "user" || t.role === "assistant") && typeof t.content === "string")
      .map((t): Turn => ({ role: t.role, content: t.content, usage: t.usage }));
  } catch {
    return [];
  }
}

export function ChatPanel({ open, onClose, webinars }: Props) {
  const [turns, setTurns] = useState<Turn[]>(readSavedTurns);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [width, setWidth] = useState<number>(readSavedWidth);
  const [resizing, setResizing] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Persist the conversation on every change so closing/reopening the panel
  // — or refreshing the whole page — keeps it. We strip the transient
  // `streaming` flag before writing; a partially-streamed message is just
  // saved as its current text and the in-flight stream is aborted on unmount.
  useEffect(() => {
    try {
      const stripped = turns.map((t) => ({ role: t.role, content: t.content, usage: t.usage }));
      window.localStorage.setItem(TURNS_STORAGE_KEY, JSON.stringify(stripped));
    } catch {
      /* quota exceeded or storage blocked — drop silently */
    }
  }, [turns]);

  // Auto-scroll to the latest message as text streams in.
  useEffect(() => {
    if (!scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [turns]);

  // Cancel any in-flight request when the panel closes.
  useEffect(() => {
    if (!open && abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
      setSending(false);
    }
  }, [open]);

  // Persist width changes so it's stable across reloads. We write on every
  // width change rather than only on drag-end because the drag itself
  // already calls setWidth on every move — one write per drag is fine.
  useEffect(() => {
    try {
      window.localStorage.setItem(WIDTH_STORAGE_KEY, String(width));
    } catch {
      /* localStorage blocked — ignore */
    }
  }, [width]);

  function startResize(e: React.MouseEvent<HTMLDivElement>) {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = width;
    setResizing(true);

    // Cap to 90% of the current viewport so the drag can't fully cover the
    // table even on narrow windows. Recomputing at drag-start handles
    // window resize between drags without extra resize listeners.
    const cap = Math.min(MAX_WIDTH, Math.floor(window.innerWidth * 0.9));

    function onMove(ev: MouseEvent) {
      // Panel is anchored to the right; dragging left grows the width.
      const delta = startX - ev.clientX;
      const next = Math.min(cap, Math.max(MIN_WIDTH, startWidth + delta));
      setWidth(next);
    }
    function onUp() {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      setResizing(false);
    }

    // While dragging, the cursor stays as col-resize even when it strays
    // off the handle; suppress text selection so the body doesn't highlight.
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  async function handleSend() {
    const text = input.trim();
    if (!text || sending) return;

    setError(null);
    setInput("");
    setSending(true);

    const userTurn: Turn = { role: "user", content: text };
    const assistantTurn: Turn = { role: "assistant", content: "", streaming: true };
    setTurns((prev) => [...prev, userTurn, assistantTurn]);

    // Resend the full history each turn — backend is stateless.
    const history: ChatTurn[] = [
      ...turns.map((t) => ({ role: t.role, content: t.content })),
      { role: "user", content: text },
    ];

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamChat({
        messages: history,
        statsContext: webinars,
        signal: controller.signal,
        onEvent: (event) => {
          if (event.type === "delta") {
            setTurns((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = { ...last, content: last.content + event.text };
              }
              return next;
            });
          } else if (event.type === "usage") {
            setTurns((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = { ...last, usage: event.usage };
              }
              return next;
            });
          } else if (event.type === "done") {
            setTurns((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = { ...last, streaming: false };
              }
              return next;
            });
          } else if (event.type === "error") {
            setError(event.message);
            // Drop the empty assistant placeholder so the user can retry cleanly.
            setTurns((prev) => prev.filter((t, i) => !(i === prev.length - 1 && t.role === "assistant" && !t.content)));
          }
        },
      });
    } catch (e) {
      if (e instanceof Error && e.name !== "AbortError") {
        setError(e.message);
      }
    } finally {
      setSending(false);
      abortRef.current = null;
    }
  }

  function handleReset() {
    if (!confirm("Clear this conversation? The webinar data on the page stays.")) return;
    if (abortRef.current) abortRef.current.abort();
    setTurns([]);
    setError(null);
    // The turns effect will also write [] — but be explicit so the storage
    // entry doesn't outlive the cleared state if the effect is skipped.
    try {
      window.localStorage.removeItem(TURNS_STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }

  if (!open) return null;

  return (
    // top-12 matches TopNav's h-12 (48px) so the panel sits flush under the
    // app header instead of being clipped behind it (the nav has z-50 and
    // would otherwise cover the panel's own header controls).
    <aside
      className="fixed top-12 right-0 bottom-0 z-40 max-w-[100vw] flex flex-col bg-white dark:bg-zinc-950 border-l border-zinc-200 dark:border-zinc-800/60 shadow-2xl"
      style={{ width: `${width}px` }}
      role="dialog"
      aria-label="Statistics chat assistant"
    >
      {/* Drag-to-resize handle on the left edge. The 6px hit area is wider
       * than the visible 2px line so the handle is easy to grab without
       * being intrusive. Highlights on hover and during an active drag. */}
      <div
        onMouseDown={startResize}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize chat panel"
        title="Drag to resize"
        className={`absolute top-0 bottom-0 left-0 w-1.5 -ml-0.5 cursor-col-resize group z-50 ${
          resizing ? "" : ""
        }`}
      >
        <div
          className={`absolute top-0 bottom-0 left-1/2 -translate-x-1/2 w-0.5 transition-colors ${
            resizing
              ? "bg-violet-500"
              : "bg-transparent group-hover:bg-violet-500/60"
          }`}
        />
      </div>
      {/* Header */}
      <header className="flex items-center justify-between px-4 py-3 border-b border-zinc-200 dark:border-zinc-800/60">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-md bg-violet-500/15 flex items-center justify-center">
            <svg className="w-4 h-4 text-violet-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
            </svg>
          </div>
          <div>
            <div className="text-sm font-bold text-zinc-900 dark:text-zinc-100">Stats Assistant</div>
            <div className="text-[10px] text-zinc-500">claude-opus-4-7 · sees {webinars.length} webinar{webinars.length === 1 ? "" : "s"}</div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {turns.length > 0 && (
            <button
              onClick={handleReset}
              title="Clear conversation — wipes saved history"
              className="p-1.5 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800/60 text-zinc-500 hover:text-red-500 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6M1 7h22M9 7V4a2 2 0 012-2h2a2 2 0 012 2v3" />
              </svg>
            </button>
          )}
          <button
            onClick={onClose}
            title="Minimize (conversation kept — reopen to continue)"
            className="p-1.5 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800/60 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
          >
            {/* Chevron pointing right — visually "tucks the panel to the
                edge", reads as collapse/minimize rather than destructive
                close. The conversation is preserved across this. */}
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M13 5l7 7-7 7M5 5l7 7-7 7" />
            </svg>
          </button>
        </div>
      </header>

      {/* Transcript */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-4 py-3 space-y-3 bg-zinc-50/50 dark:bg-zinc-950"
      >
        {turns.length === 0 && (
          <div className="text-xs text-zinc-500 leading-relaxed">
            <p className="mb-2 font-semibold text-zinc-700 dark:text-zinc-300">Try asking:</p>
            <ul className="space-y-1">
              <li>· Which webinar had the best Yes-to-attend rate?</li>
              <li>· Compare W{lastNumber(webinars)} and W{secondLastNumber(webinars)} on bookings per attended.</li>
              <li>· What&apos;s the trend in self-reg over the last 5 webinars?</li>
              <li>· Which lists or senders are underperforming?</li>
            </ul>
            <p className="mt-3 text-[10px] text-zinc-400">
              The assistant only sees the webinars currently loaded on this page. Conversations are saved in your browser; minimize anytime and pick up where you left off. Use the trash icon to clear.
            </p>
          </div>
        )}

        {turns.map((t, i) => (
          <TurnBubble key={i} turn={t} />
        ))}

        {error && (
          <div className="px-3 py-2 rounded-md border border-red-500/30 bg-red-500/10 text-xs text-red-500">
            {error}
          </div>
        )}
      </div>

      {/* Composer */}
      <div className="px-3 py-3 border-t border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-950">
        <div className="flex gap-2 items-end">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder="Ask about the webinars on this page…"
            rows={2}
            disabled={sending}
            className="flex-1 resize-none bg-zinc-50 dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700/60 rounded-md px-3 py-2 text-xs text-zinc-800 dark:text-zinc-200 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-violet-500 disabled:opacity-60"
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || sending}
            className="px-3 py-2 text-xs rounded-md bg-violet-600 hover:bg-violet-500 text-white font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {sending ? (
              <span className="inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin" />
            ) : (
              "Send"
            )}
          </button>
        </div>
        <div className="mt-1.5 text-[10px] text-zinc-400">Enter to send · Shift+Enter for newline</div>
      </div>
    </aside>
  );
}

function TurnBubble({ turn }: { turn: Turn }) {
  const isUser = turn.role === "user";
  return (
    <div className={isUser ? "flex justify-end" : "flex justify-start"}>
      <div
        className={`max-w-[88%] rounded-lg px-3 py-2 text-xs leading-relaxed break-words ${
          isUser
            ? "bg-violet-600 text-white whitespace-pre-wrap"
            : "bg-zinc-100 dark:bg-zinc-800/60 text-zinc-800 dark:text-zinc-200"
        }`}
      >
        {isUser ? (
          turn.content
        ) : turn.content ? (
          <AssistantMarkdown text={turn.content} streaming={!!turn.streaming} />
        ) : turn.streaming ? (
          <StreamingDots />
        ) : null}
        {turn.usage && (
          <div className="mt-1.5 text-[9px] opacity-60 font-mono">
            {turn.usage.cache_read_input_tokens > 0
              ? `${turn.usage.cache_read_input_tokens.toLocaleString()} cached · ${turn.usage.input_tokens.toLocaleString()} new · ${turn.usage.output_tokens.toLocaleString()} out`
              : `${turn.usage.input_tokens.toLocaleString()} in · ${turn.usage.output_tokens.toLocaleString()} out${turn.usage.cache_creation_input_tokens > 0 ? ` · ${turn.usage.cache_creation_input_tokens.toLocaleString()} cached` : ""}`}
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── Assistant markdown renderer ─────────────────────────────────────
 * The chat panel is narrow, so we tune typography for that:
 * - small headings (no oversized h1/h2 that eats the bubble width)
 * - tight list spacing
 * - tables scroll horizontally inside the bubble
 * - inline code highlighted; fenced code blocks wrap (no h-scroll line of
 *   one giant SQL query that the operator can't see).
 * GitHub-flavored markdown (tables, task lists, strikethrough) is enabled
 * via remark-gfm — Claude routinely uses tables for comparisons. */
function AssistantMarkdown({ text, streaming }: { text: string; streaming: boolean }) {
  return (
    <div className="chat-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => <h1 className="text-sm font-bold mt-2 mb-1 first:mt-0">{children}</h1>,
          h2: ({ children }) => <h2 className="text-[13px] font-bold mt-2 mb-1 first:mt-0">{children}</h2>,
          h3: ({ children }) => <h3 className="text-xs font-bold mt-2 mb-1 first:mt-0 uppercase tracking-wide text-zinc-600 dark:text-zinc-400">{children}</h3>,
          p: ({ children }) => <p className="my-1.5 first:mt-0 last:mb-0">{children}</p>,
          strong: ({ children }) => <strong className="font-semibold text-zinc-900 dark:text-zinc-100">{children}</strong>,
          em: ({ children }) => <em className="italic">{children}</em>,
          ul: ({ children }) => <ul className="list-disc pl-4 my-1.5 space-y-0.5 marker:text-zinc-400">{children}</ul>,
          ol: ({ children }) => <ol className="list-decimal pl-4 my-1.5 space-y-0.5 marker:text-zinc-400">{children}</ol>,
          li: ({ children }) => <li className="pl-0.5">{children}</li>,
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer noopener"
              className="text-violet-500 hover:text-violet-400 underline underline-offset-2 decoration-violet-500/40"
            >
              {children}
            </a>
          ),
          code: ({ className, children, ...rest }) => {
            // react-markdown 10 doesn't expose `inline` on the props — we
            // detect block code by the language- class set on fenced blocks.
            const isBlock = /language-/.test(className || "");
            if (isBlock) {
              return (
                <code className={`${className ?? ""} block`} {...rest}>
                  {children}
                </code>
              );
            }
            return (
              <code
                className="px-1 py-0.5 rounded bg-zinc-200/70 dark:bg-zinc-900/80 text-[11px] font-mono text-zinc-800 dark:text-zinc-100 break-words"
                {...rest}
              >
                {children}
              </code>
            );
          },
          pre: ({ children }) => (
            <pre className="my-2 px-2.5 py-2 rounded-md bg-zinc-900 text-zinc-100 text-[11px] font-mono leading-relaxed whitespace-pre-wrap break-words overflow-x-auto">
              {children}
            </pre>
          ),
          blockquote: ({ children }) => (
            <blockquote className="my-2 pl-3 border-l-2 border-violet-500/40 text-zinc-600 dark:text-zinc-400 italic">
              {children}
            </blockquote>
          ),
          table: ({ children }) => (
            <div className="my-2 -mx-1 overflow-x-auto">
              <table className="w-full text-[11px] border-collapse">{children}</table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-zinc-200/50 dark:bg-zinc-900/60">{children}</thead>,
          tr: ({ children }) => <tr className="border-b border-zinc-200 dark:border-zinc-800/60">{children}</tr>,
          th: ({ children }) => <th className="px-2 py-1 text-left font-semibold text-zinc-700 dark:text-zinc-200">{children}</th>,
          td: ({ children }) => <td className="px-2 py-1 align-top">{children}</td>,
          hr: () => <hr className="my-3 border-zinc-300 dark:border-zinc-700/60" />,
        }}
      >
        {text}
      </ReactMarkdown>
      {streaming && (
        // Caret follows the last character; the inline-block + align-baseline
        // keeps it on the final line rather than dropping to its own line.
        <span className="inline-block w-1.5 h-3 ml-0.5 bg-zinc-500 animate-pulse align-baseline" aria-hidden />
      )}
    </div>
  );
}

function StreamingDots() {
  return (
    <span className="inline-flex gap-1 items-center">
      <span className="w-1.5 h-1.5 rounded-full bg-zinc-500 animate-bounce" style={{ animationDelay: "0ms" }} />
      <span className="w-1.5 h-1.5 rounded-full bg-zinc-500 animate-bounce" style={{ animationDelay: "150ms" }} />
      <span className="w-1.5 h-1.5 rounded-full bg-zinc-500 animate-bounce" style={{ animationDelay: "300ms" }} />
    </span>
  );
}

function lastNumber(webinars: ApiStatisticsWebinar[]): number {
  return webinars[0]?.number ?? 0;
}
function secondLastNumber(webinars: ApiStatisticsWebinar[]): number {
  return webinars[1]?.number ?? 0;
}
