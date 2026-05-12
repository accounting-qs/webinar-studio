"""Statistics chat router — single streaming endpoint.

Holds no state. The operator's browser owns the conversation; each turn
posts the full message history plus the currently-loaded Statistics page
snapshot, and the response streams back as SSE.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from db.session import get_db
from services.chat_agent import get_anthropic_client, stream_chat_response

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_auth)])


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    # Full conversation history including the new user turn at the end.
    # We keep this stateless: the panel resends history each turn.
    messages: list[ChatMessage]
    # Whatever the Statistics page has loaded — a list of webinar dicts.
    # Shape mirrors ApiStatisticsWebinar[] from the frontend; we pass it
    # through to Claude as a cached system block, so determinism matters
    # (frontend serializes with sorted keys / no timestamps).
    stats_context: list[dict] | dict


def _sse(payload: dict) -> bytes:
    """Serialize an event dict as one SSE frame. Frames are newline-
    terminated; the empty trailing line is the delimiter."""
    return f"data: {json.dumps(payload, default=str)}\n\n".encode("utf-8")


@router.post("/messages")
async def post_chat_message(
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """Stream a Claude response to the conversation as SSE.

    Response envelope (one JSON object per `data:` line):
      {"type": "delta", "text": "..."}        — incremental text token(s)
      {"type": "usage", "usage": {...}}       — final token counts; the
                                                `cache_read_input_tokens`
                                                field confirms prompt-cache
                                                hits on follow-up turns
      {"type": "done"}                        — turn finished
      {"type": "error", "message": "..."}     — terminal error
    """
    if not body.messages:
        raise HTTPException(400, "messages cannot be empty")
    if body.messages[-1].role != "user":
        raise HTTPException(400, "last message must be from the user")

    try:
        client = await get_anthropic_client(db)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # Anthropic expects [{role, content}] — content can be a string. We keep
    # the same shape the frontend sends.
    api_messages = [{"role": m.role, "content": m.content} for m in body.messages]

    async def event_stream():
        try:
            async for event in stream_chat_response(
                client,
                messages=api_messages,
                stats_context=body.stats_context,
            ):
                yield _sse(event)
        except Exception as exc:
            logger.exception("Chat stream crashed")
            yield _sse({"type": "error", "message": f"Chat crashed: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # Tell intermediate proxies (nginx, Cloudflare) not to buffer.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
