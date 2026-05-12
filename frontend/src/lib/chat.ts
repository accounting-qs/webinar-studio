/**
 * SSE client for the Statistics chat assistant.
 *
 * The backend's POST /chat/messages returns a `text/event-stream` body.
 * Each frame is `data: {json}\n\n`. Frame shapes:
 *   {type: "delta", text: "..."}                      // incremental text
 *   {type: "usage", usage: {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}}
 *   {type: "done"}
 *   {type: "error", message: "..."}
 *
 * We use `fetch` + a manual reader rather than EventSource because EventSource
 * is GET-only and our request body holds the full conversation + stats blob.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const API_TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? "";

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatUsage {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
}

export type ChatEvent =
  | { type: "delta"; text: string }
  | { type: "usage"; usage: ChatUsage }
  | { type: "done" }
  | { type: "error"; message: string };

export interface SendChatParams {
  messages: ChatTurn[];
  statsContext: unknown;
  onEvent: (event: ChatEvent) => void;
  signal?: AbortSignal;
}

/** Stream a chat response. Resolves when the backend sends `done` or `error`,
 * or when the signal aborts. The caller drives UI state via `onEvent`. */
export async function streamChat(params: SendChatParams): Promise<void> {
  const { messages, statsContext, onEvent, signal } = params;

  const res = await fetch(`${API_URL}/chat/messages`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_TOKEN}`,
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ messages, stats_context: statsContext }),
    signal,
  });

  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const err = await res.json();
      if (err?.detail) detail = err.detail;
    } catch {
      /* keep statusText */
    }
    onEvent({ type: "error", message: detail });
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line. Walk forward, slicing each.
    let sep = buffer.indexOf("\n\n");
    while (sep !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const dataLine = frame.split("\n").find((l) => l.startsWith("data: "));
      if (dataLine) {
        const json = dataLine.slice(6).trim();
        if (json) {
          try {
            const event = JSON.parse(json) as ChatEvent;
            onEvent(event);
            if (event.type === "done" || event.type === "error") return;
          } catch {
            // Malformed frame — skip but don't crash the loop.
          }
        }
      }
      sep = buffer.indexOf("\n\n");
    }
  }
}
