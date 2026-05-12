"""Statistics chat agent — Claude Opus 4.7 with adaptive thinking + prompt caching.

The agent answers questions about whatever the operator is looking at on the
Statistics page. We send the loaded `webinars` snapshot as a cached system
block so it stays read-hot across the conversation's turns — only the
running message list and the new user question pay full-price input tokens
each turn.

Determinism for the cache prefix:
- The snapshot is serialized with `sort_keys=True` and a compact separator
  so the rendered bytes are identical across requests that share data.
- No timestamps, request IDs, or per-user fields are interpolated into the
  system prompt.
- The model + adaptive-thinking config are fixed.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ConnectorCredential

logger = logging.getLogger(__name__)

CHAT_MODEL = "claude-opus-4-7"
ANTHROPIC_PROVIDER = "anthropic"
DEFAULT_CREDENTIAL_NAME = "default"
MAX_TOKENS = 4096


# Stable preamble — frozen bytes so the system-prompt prefix caches cleanly.
# Any change here invalidates every cached conversation. Avoid timestamps,
# user IDs, or anything that varies per request.
_SYSTEM_PREAMBLE = """You are a webinar-statistics assistant for the Compete-IQ Statistics page.

You answer the operator's questions about the webinar data attached below.
The attached data is the EXACT same data the operator is currently looking
at on screen — the full set of webinars loaded into the Statistics page,
each with its summary row plus per-list child rows.

What you can do well:
- Compare metrics across webinars, lists, or A/B variants
- Spot trends (e.g. "self-reg attendance over the last 5 webinars")
- Explain why two webinars or variants differ, citing the underlying metric
- Suggest which lists or senders are underperforming and where the gap is
- Decode any field in the data when asked

How to behave:
- Answer with concrete numbers from the data. Cite webinar numbers, variant
  labels, and list descriptions so the operator can verify in the table.
- If the user asks about a webinar number not in the snapshot, say so
  explicitly — do not invent numbers.
- Prefer compact responses. Tables are great for multi-row comparisons;
  short paragraphs are great for "why" questions.
- When a metric uses fallback (usedFallback=true on the row or summary), the
  rate denominator is `invited` (planned) instead of `actuallyUsed` —
  mention this if the operator's question depends on the denominator.
- When a NO LIST DATA row has sharedAcrossVariants=true, the GHL Yes/Maybe
  signals can't be split between sibling variants; the row's counts appear
  on both. Surface this caveat if the operator is comparing variants.

Field guide:
- Each webinar has: number, variantLabel (null for non-A/B webinars),
  hasSiblingVariants, summary (metrics dict), rows (per-list children).
- Each row has: kind ("list" | "nonjoiners" | "no_list_data"), description,
  sendInfo (sender name), bucketName, assignmentId, metrics, usedFallback.
- Common metrics: invited (planned), actuallyUsed (live sent count),
  unsubscribes, yesMarked, maybeMarked, totalRegs, totalAttended,
  total10MinPlus, total30MinPlus, totalBookings, won, qualified,
  yesPercent, yesAttendPercent, invitedToRegPercent, invitedToAttendPercent,
  bookingsPerAttended, closeRatePercent, etc.

Data scope:
- You can only see what's loaded on screen — that's the data attached
  below. If the operator asks for "all webinars ever", note that you see
  only the loaded set."""


async def get_anthropic_client(db: AsyncSession) -> anthropic.AsyncAnthropic:
    """Resolve the Anthropic credential from connector_credentials.

    Raises ValueError if the key isn't configured — the chat endpoint
    translates that into a 400 with an actionable message.
    """
    row = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.provider == ANTHROPIC_PROVIDER,
            ConnectorCredential.name == DEFAULT_CREDENTIAL_NAME,
        )
    )).scalar_one_or_none()
    if not row:
        raise ValueError(
            "Anthropic API key not configured. Add one on the Connectors page."
        )
    return anthropic.AsyncAnthropic(api_key=row.api_key)


def _serialize_stats(stats_context: Any) -> str:
    """Compact, deterministic JSON for the cached stats block.

    `sort_keys=True` keeps the rendered bytes identical across requests
    that pass the same data — required for the prompt cache to hit.
    """
    return json.dumps(stats_context, separators=(",", ":"), sort_keys=True, default=str)


def build_system_blocks(stats_context: Any) -> list[dict[str, Any]]:
    """System prompt as two blocks:
      1) Stable preamble (rules + field guide).
      2) The current Statistics page snapshot, wrapped with cache_control so
         it's served from the prompt cache on subsequent turns of the same
         conversation.

    A single cache_control on the LAST system block is enough — the cache
    prefix covers `tools → system` together, and we have no tools yet.
    """
    return [
        {"type": "text", "text": _SYSTEM_PREAMBLE},
        {
            "type": "text",
            "text": (
                "Current Statistics page snapshot (JSON). Each entry is one "
                "webinar with its summary and per-list rows.\n\n"
                + _serialize_stats(stats_context)
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]


async def stream_chat_response(
    client: anthropic.AsyncAnthropic,
    *,
    messages: list[dict[str, Any]],
    stats_context: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Stream Claude's reply as a sequence of structured events:

      {"type": "delta", "text": "..."}      # incremental text
      {"type": "usage", "usage": {...}}     # final token counts (cache hit info)
      {"type": "done"}                      # turn complete
      {"type": "error", "message": "..."}   # something went wrong

    The endpoint serializes these as SSE.
    """
    system_blocks = build_system_blocks(stats_context)

    try:
        async with client.messages.stream(
            model=CHAT_MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=system_blocks,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                yield {"type": "delta", "text": text}

            final = await stream.get_final_message()
            usage = getattr(final, "usage", None)
            if usage is not None:
                yield {
                    "type": "usage",
                    "usage": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    },
                }
        yield {"type": "done"}
    except anthropic.RateLimitError:
        yield {"type": "error", "message": "Rate limit hit — wait a moment and try again."}
    except anthropic.AuthenticationError:
        yield {"type": "error", "message": "Anthropic key invalid or revoked. Update it on the Connectors page."}
    except anthropic.BadRequestError as exc:
        logger.exception("Chat bad-request")
        yield {"type": "error", "message": f"Request rejected: {exc}"}
    except Exception as exc:
        logger.exception("Chat streaming failed")
        yield {"type": "error", "message": f"Chat failed: {exc}"}
