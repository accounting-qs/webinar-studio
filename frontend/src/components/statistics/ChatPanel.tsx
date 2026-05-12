"use client";

import { useEffect, useRef, useState } from "react";
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

export function ChatPanel({ open, onClose, webinars }: Props) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

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
    if (abortRef.current) abortRef.current.abort();
    setTurns([]);
    setError(null);
  }

  if (!open) return null;

  return (
    <aside
      className="fixed top-0 right-0 bottom-0 z-40 w-[420px] max-w-[100vw] flex flex-col bg-white dark:bg-zinc-950 border-l border-zinc-200 dark:border-zinc-800/60 shadow-2xl"
      role="dialog"
      aria-label="Statistics chat assistant"
    >
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
              title="Clear conversation"
              className="p-1.5 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800/60 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v6h6M20 20v-6h-6M4 10a8 8 0 0114-3M20 14a8 8 0 01-14 3" />
              </svg>
            </button>
          )}
          <button
            onClick={onClose}
            title="Close (Esc)"
            className="p-1.5 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800/60 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
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
              The assistant only sees the webinars currently loaded on this page. Conversations are not saved — refreshing the page starts fresh.
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
        className={`max-w-[88%] rounded-lg px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap break-words ${
          isUser
            ? "bg-violet-600 text-white"
            : "bg-zinc-100 dark:bg-zinc-800/60 text-zinc-800 dark:text-zinc-200"
        }`}
      >
        {turn.content || (turn.streaming ? <StreamingDots /> : null)}
        {turn.streaming && turn.content && <span className="inline-block w-1.5 h-3 ml-0.5 bg-zinc-500 animate-pulse align-middle" />}
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
