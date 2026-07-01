"""
Brain reconciliation service — turns a natural-language instruction into a set of
add/edit/delete operations over the existing copywriting principles, WITHOUT
blindly appending. The LLM reviews every current principle first so new input is
merged/reconciled against the library instead of stacking conflicting rules.

Returns a *proposal* only; nothing is written here. The caller shows the proposed
operations as a reviewable diff and applies the approved subset separately.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import CopywritingPrinciple
# Anthropic key + model come from the Connectors config (same source the
# Statistics chat assistant uses), not the hardcoded env vars.
from services.chat_agent import get_anthropic_client, resolve_model_id
from services.generation import _log_claude_cost

logger = logging.getLogger(__name__)

VALID_KNOWLEDGE_TYPES = ("brand", "copy_general", "copy_format", "learned")

_TOOL = {
    "name": "propose_brain_changes",
    "description": (
        "Return the minimal set of operations that incorporate the user's instruction "
        "into the copywriting-principle library without creating duplicates or conflicts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One sentence describing what these operations do overall.",
            },
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["add", "edit", "delete"],
                        },
                        "principle_id": {
                            "type": "string",
                            "description": "Required for edit/delete. The id of the existing principle. Omit for add.",
                        },
                        "current_text": {
                            "type": "string",
                            "description": "For edit/delete: the existing principle text, copied verbatim, so the UI can show a before/after diff.",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "For add/edit: the full proposed principle text.",
                        },
                        "knowledge_type": {
                            "type": "string",
                            "enum": list(VALID_KNOWLEDGE_TYPES),
                            "description": "For add: which bucket the new principle belongs to. Default copy_general.",
                        },
                        "category": {
                            "type": "string",
                            "description": "Optional short category tag for an added principle (e.g. brand_voice, titles, cta).",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Short justification for this operation.",
                        },
                    },
                    "required": ["op", "reason"],
                },
            },
        },
        "required": ["summary", "operations"],
    },
}

_SYSTEM = """You are the librarian of a copywriting-principle "brain". This brain is a list of short, imperative principles that an AI later uses to write B2B calendar invites and Facebook ads for QS (Quantum Scaling).

Your job: given the FULL current list of principles (each with an id) and one instruction from the user, decide the SMALLEST set of operations that incorporates the instruction while keeping the library clean.

Rules:
- Prefer EDIT over ADD. If a principle already covers the topic, edit it in place rather than adding a near-duplicate.
- If the instruction CONFLICTS with an existing principle, resolve the conflict: edit (or delete) the stale principle so the two no longer contradict. Treat the user's new instruction as the newer, authoritative intent unless it is obviously a mistake.
- If two existing principles say the same thing, you may MERGE them (edit one to be the combined version, delete the other).
- Only ADD when the instruction is genuinely new. Pick knowledge_type from: brand (brand voice / ICP / positioning), copy_general (applies to all copy), copy_format (format-specific, e.g. calendar titles or FB ads), learned. Give a short category when useful.
- Keep every principle a single, clear, self-contained directive. Preserve existing scope carve-outs (e.g. rules that apply only to calendar invites vs. Facebook ads) — do not flatten them.
- NEVER invent factual claims, numbers, or client results. Only restructure/clarify wording and encode the user's instruction.
- For every edit/delete op you MUST include principle_id AND current_text copied verbatim from the list.
- If the instruction needs no change (already fully covered), return an empty operations list and say so in the summary.
- Keep the total number of operations minimal and each reason to one line."""


def _render_principles(rows: list[CopywritingPrinciple]) -> str:
    lines = []
    for p in rows:
        scope = p.knowledge_type + (f"/{p.category}" if p.category else "")
        lines.append(f"[id={p.id}] ({scope}) {p.principle_text}")
    return "\n".join(lines)


async def propose_principle_changes(
    db: AsyncSession,
    user_id: str,
    instruction: str,
    model_key: str | None = None,
) -> dict:
    """Ask Claude for a reconciled set of operations. Writes nothing.

    Uses the Anthropic key + model configured on the Connectors page. `model_key`
    is one of chat_agent.CHAT_MODELS ("sonnet"/"opus"); None falls back to the
    connector default (Sonnet 4.6).

    Returns {"summary": str, "operations": [ {op, principle_id?, current_text?,
    new_text?, knowledge_type?, category?, reason} ]}.
    """
    result = await db.execute(
        select(CopywritingPrinciple).where(
            CopywritingPrinciple.user_id == user_id,
            CopywritingPrinciple.deleted_at.is_(None),
        ).order_by(CopywritingPrinciple.display_order, CopywritingPrinciple.created_at)
    )
    rows = result.scalars().all()

    user_msg = (
        f"CURRENT PRINCIPLES ({len(rows)}):\n{_render_principles(rows)}\n\n"
        f"USER INSTRUCTION:\n{instruction.strip()}\n\n"
        "Return the operations via the propose_brain_changes tool."
    )

    client = await get_anthropic_client(db)
    model_id = resolve_model_id(model_key)

    message = await client.messages.create(
        model=model_id,
        max_tokens=4096,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "propose_brain_changes"},
    )

    asyncio.create_task(_log_claude_cost(
        model=model_id,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        session_id=f"{user_id}:brain_reconcile",
        session_label="Brain reconcile",
    ))

    tool_input = None
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "propose_brain_changes":
            tool_input = block.input
            break
    if tool_input is None:
        raise ValueError("Model did not return a proposal.")

    # Validate / normalize operations against the real principle set.
    valid_ids = {p.id: p for p in rows}
    clean_ops: list[dict] = []
    for op in tool_input.get("operations", []):
        kind = op.get("op")
        if kind not in ("add", "edit", "delete"):
            continue
        if kind in ("edit", "delete"):
            pid = op.get("principle_id")
            if pid not in valid_ids:
                logger.warning("[brain_reconcile] dropping op with unknown id: %s", pid)
                continue
            # Always trust the DB for current_text so the diff is accurate.
            op["current_text"] = valid_ids[pid].principle_text
        if kind in ("add", "edit") and not (op.get("new_text") or "").strip():
            continue
        if kind == "add":
            kt = op.get("knowledge_type")
            if kt not in VALID_KNOWLEDGE_TYPES:
                op["knowledge_type"] = "copy_general"
        clean_ops.append(op)

    return {"summary": tool_input.get("summary", ""), "operations": clean_ops}
