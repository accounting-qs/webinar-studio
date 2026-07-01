"""
Generation service — loads brain context from Supabase and calls Claude.
Supports streaming (SSE) for real-time frontend output.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator

import anthropic
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import UniversalBrain, FormatBrain, CopywritingPrinciple, CaseStudy
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

CLAUDE_PRICING = {
    "claude-sonnet-4-6": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
    "claude-haiku-4-5-20251001": {"input_per_mtok": 0.80, "output_per_mtok": 4.00},
    "claude-opus-4-6": {"input_per_mtok": 15.00, "output_per_mtok": 75.00},
}


async def _log_claude_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    session_id: str | None = None,
    session_label: str | None = None,
) -> None:
    """Fire-and-forget — scheduled as asyncio.create_task(). Never raises."""
    try:
        from sqlalchemy import text
        pricing = CLAUDE_PRICING.get(model, {"input_per_mtok": 3.00, "output_per_mtok": 15.00})
        cost_usd = (
            (input_tokens / 1_000_000) * pricing["input_per_mtok"]
            + (output_tokens / 1_000_000) * pricing["output_per_mtok"]
        )
        async with AsyncSessionLocal() as db:
            await db.execute(text("""
                INSERT INTO api_cost_log
                    (api_name, model, input_tokens, output_tokens, cost_usd, session_id, session_label)
                VALUES
                    (:api_name, :model, :input_tokens, :output_tokens, :cost_usd, :session_id, :session_label)
            """), {
                "api_name": "claude", "model": model,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cost_usd": float(cost_usd), "session_id": session_id,
                "session_label": session_label,
            })
            await db.commit()
    except Exception as e:
        logger.warning("[cost_log] Failed to log Claude cost: %s", e)

_client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


# Shared authority hierarchy injected into every generation prompt. Principles
# are supreme; examples are structural references only (never a source of values).
_AUTHORITY_NOTE = """## Authority Order (read this first)
When the sections below conflict, the higher-ranked one always wins — no exceptions:
1. Copywriting Principles — the HIGHEST authority. They override the Format Rules, the Business Context, this prompt's own style labels, and especially the Examples. If a principle contradicts anything else, follow the principle.
2. Client Case Studies / the provided brief — the only source of real client numbers, used verbatim.
3. Format Rules and Business Context.
4. Real Examples — the LOWEST authority. Use them for STRUCTURE, FORMAT, RHYTHM, and VOICE only. Never copy a specific value out of an example: no numbers, percentages, metrics, dollar amounts, names, dates, or claims. Treat every value shown in an example as a placeholder, not a fact to reuse — all real values come from the Principles and Case Studies above."""


# ── Deterministic deliverability validator ──────────────────────────────────
# Soft principles in the prompt don't reliably enforce; this is a hard gate that
# checks generated copy and triggers a targeted repair pass on any violation.
_BANNED_JARGON = ("pipeline", "funnel", "acquisition engine", "demand on repeat", "qualified sales calls")
_RE_REVENUE = re.compile(r"\$\s?\d|\bARR\b|\b\d+\s*-?\s*figure\b", re.I)
_RE_NEG_COMPARISON = re.compile(r"\b(?:without|no)\s+(?:cold outreach|cold call[s]?|paid ads|outbound|retainer[s]?)\b", re.I)
_RE_NOT_X_NOT_Y = re.compile(r"\bnot\s+[\w' /-]+,\s*not\s+", re.I)
_RE_REVEAL = re.compile(r"^\s*(?:revealed|exposed|the secret)\b[:\-]?", re.I)
_RE_THE_USE_TO = re.compile(r"\bthe\b[\w ]+\buse[s]?\s+to\b", re.I)


def _validate_copy(text: str, copy_type: str) -> list[str]:
    """Deterministic deliverability gate mirroring the calendar-invite principles.
    Returns a list of violation messages (empty list = clean)."""
    violations: list[str] = []
    low = text.lower()
    for kw in _BANNED_JARGON:
        if kw in low:
            violations.append(f'banned jargon "{kw}"')
    if copy_type == "description":
        if _RE_REVENUE.search(text):
            violations.append('revenue/$ figure — use activity metrics (calls/month, attendees/month, conversion %)')
        m = _RE_NEG_COMPARISON.search(text) or _RE_NOT_X_NOT_Y.search(text)
        if m:
            violations.append(f'negative-comparison phrasing "{m.group(0).strip()}"')
    elif copy_type == "title":
        n = len(text)
        if not (40 <= n <= 60):
            violations.append(f"length {n} chars (must be 40-60)")
        if _RE_REVEAL.search(text):
            violations.append("curiosity/reveal opener")
        if _RE_THE_USE_TO.search(text):
            violations.append('banned "The ... Use to" construction')
    return violations


async def _repair_copy(text: str, copy_type: str, violations: list[str]) -> str:
    """One targeted repair pass — fix only the flagged violations, keep everything else."""
    fixes = "\n".join(f"- {v}" for v in violations)
    prompt = f"""Rewrite this calendar-invite {copy_type} to fix ONLY these deliverability violations. Change nothing else about the meaning, proof, or structure.

VIOLATIONS TO FIX:
{fixes}

RULES TO SATISFY:
- Description: never contain a "$" or revenue figure — express proof as activity metrics only (calls booked/month, attendees/month, conversion %). No banned jargon (pipeline, funnel, acquisition engine, demand on repeat, qualified sales calls). No negative-comparison ("without ...", "no retainer", "not X, not Y") — state mechanisms affirmatively.
- Title: "How [audience] [do activity] using [tool]" framing, 40-60 characters, no curiosity/reveal opener, no "The ... Use to" construction, no emoji.

Return ONLY the rewritten {copy_type} text — no preamble, no quotes, no code fences.

CURRENT {copy_type.upper()}:
{text}"""
    msg = await _client.messages.create(
        model=settings.CLAUDE_MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    asyncio.create_task(_log_claude_cost(
        model=settings.CLAUDE_MODEL,
        input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
        session_id="copy_repair", session_label=f"Copy repair ({copy_type})",
    ))
    out = msg.content[0].text.strip()
    if out.startswith("```"):
        out = out.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return out.strip().strip('"').strip()


async def _validate_and_repair(copies: list[str], copy_type: str) -> list[str]:
    """Validate each variant; run one repair pass on any with violations, keeping
    whichever version has fewer violations."""
    out: list[str] = []
    for c in copies:
        viol = _validate_copy(c, copy_type)
        if viol:
            logger.warning("[copy_validator] %s has %d violation(s): %s", copy_type, len(viol), viol)
            try:
                repaired = await _repair_copy(c, copy_type, viol)
                if repaired and len(_validate_copy(repaired, copy_type)) < len(viol):
                    c = repaired
                    residual = _validate_copy(c, copy_type)
                    if residual:
                        logger.warning("[copy_validator] residual %s violation(s) after repair: %s", copy_type, residual)
            except Exception as e:
                logger.warning("[copy_validator] repair failed, keeping original: %s", e)
        out.append(c)
    return out


def _first_word(t: str) -> str:
    return (t.strip().split() or [""])[0].lower().strip(":,")


async def _repair_title_structure(text: str, avoid_word: str) -> str | None:
    """Rewrite a title with a different opening structure to break variant sameness.
    Returns the raw rewrite (length/bans are enforced by the caller)."""
    prompt = (
        f'Rewrite this calendar-invite title with a COMPLETELY DIFFERENT opening. It currently starts with '
        f'"{avoid_word}" — the new version must NOT start with "{avoid_word}". Use an outcome-first statement '
        f'(lead with the result) or a segment-tension/question opener. Keep it under 60 characters, name the audience '
        f'segment and the tool (AI Webinars), no curiosity/reveal opener, no "The ... Use to" construction, no emoji. '
        f'Return ONLY the rewritten title.\n\nTITLE: {text}'
    )
    msg = await _client.messages.create(
        model=settings.CLAUDE_MODEL, max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    out = msg.content[0].text.strip().strip('"').strip()
    return out or None


async def _diversify_titles(titles: list[str]) -> list[str]:
    """Ensure title variants don't all share the same opening word (e.g. all 'How ...').
    Repairs later duplicates into a different opening; retries, then enforces length/bans."""
    seen: set[str] = set()
    out: list[str] = []
    for t in titles:
        first = _first_word(t)
        attempts = 0
        while first in seen and attempts < 2:
            attempts += 1
            try:
                repaired = await _repair_title_structure(t, first)
            except Exception as e:
                logger.warning("[title_diversify] repair failed: %s", e)
                break
            if not repaired:
                break
            repaired = (await _validate_and_repair([repaired], "title"))[0]
            t = repaired
            first = _first_word(t)
        seen.add(first)
        out.append(t)
    return out


async def _gate_variants(result: dict) -> dict:
    """Run the deliverability validator/repair over each A/B/C variant's title +
    description, then diversify the titles (used by the segment-based generator)."""
    variants = result.get("variants") if isinstance(result, dict) else None
    if isinstance(variants, list):
        dict_variants = [v for v in variants if isinstance(v, dict)]
        for v in dict_variants:
            if v.get("title"):
                v["title"] = (await _validate_and_repair([str(v["title"])], "title"))[0]
            if v.get("description"):
                v["description"] = (await _validate_and_repair([str(v["description"])], "description"))[0]
        titled = [v for v in dict_variants if v.get("title")]
        if len(titled) >= 2:
            div = await _diversify_titles([str(v["title"]) for v in titled])
            for v, nt in zip(titled, div):
                v["title"] = nt
    return result


async def _load_brain_context(
    db: AsyncSession,
    user_id: str,
    format_key: str,
) -> tuple[str, str, list[str], list[dict]]:
    """
    Returns (universal_content, format_content, principles, example_outputs).
    Raises ValueError if brain is missing or too thin.
    """
    # Universal brain
    ub_row = await db.scalar(
        select(UniversalBrain).where(UniversalBrain.user_id == user_id)
    )
    universal_content = ub_row.brain_content if ub_row else ""

    # Format brain
    fb_row = await db.scalar(
        select(FormatBrain).where(
            and_(
                FormatBrain.user_id == user_id,
                FormatBrain.format_key == format_key,
                FormatBrain.is_active == True,
                FormatBrain.deleted_at == None,
            )
        )
    )
    if not fb_row:
        raise ValueError(f"No active format brain found for format_key='{format_key}'")

    format_content = fb_row.brain_content or ""
    example_outputs = fb_row.example_outputs or []

    # All principles: universal + format-specific
    principles_rows = await db.scalars(
        select(CopywritingPrinciple).where(
            and_(
                CopywritingPrinciple.user_id == user_id,
                CopywritingPrinciple.is_active == True,
                CopywritingPrinciple.deleted_at == None,
            )
        ).order_by(CopywritingPrinciple.display_order)
    )
    principles = [r.principle_text for r in principles_rows.all()]

    if len(principles) < settings.BRAIN_MINIMUM_PRINCIPLES:
        raise ValueError(
            f"Brain has only {len(principles)} principles (minimum {settings.BRAIN_MINIMUM_PRINCIPLES}). "
            "Seed the brain before generating."
        )

    return universal_content, format_content, principles, example_outputs


async def _load_case_studies(
    db: AsyncSession,
    user_id: str,
    industry: str | None = None,
    bucket_name: str | None = None,
) -> list[dict]:
    """
    Load active case studies, prioritising those that match the bucket's
    industry or whose tags overlap with the bucket name / industry.
    Returns a list of dicts with title, client_name, industry, content.
    """
    result = await db.execute(
        select(CaseStudy).where(
            CaseStudy.user_id == user_id,
            CaseStudy.is_active == True,
        ).order_by(CaseStudy.created_at.desc())
    )
    all_studies = result.scalars().all()
    if not all_studies:
        return []

    # Score each study for relevance to this bucket. Industry is the strongest
    # signal; tags are a soft match; persona.target_market lets us match
    # bucket-name niches that aren't expressed as a clean industry label.
    def relevance(cs: CaseStudy) -> int:
        score = 0
        cs_industry = (cs.industry or "").lower()
        cs_tags = [t.lower() for t in (cs.tags or [])]
        bucket_industry = (industry or "").lower()
        bucket = (bucket_name or "").lower()

        # Direct industry match
        if cs_industry and bucket_industry and cs_industry == bucket_industry:
            score += 10
        # Industry appears in bucket name
        if cs_industry and bucket and cs_industry in bucket:
            score += 5
        # Tag matches
        for tag in cs_tags:
            if tag and bucket_industry and tag in bucket_industry:
                score += 3
            if tag and bucket and tag in bucket:
                score += 3

        # Structured signals from the URL importer let us match more reliably
        # than the literal industry label: aliases catch synonyms (e.g. a
        # "Consulting & Professional Services" study still matches a bucket
        # called "Advisory" or "Wealth Management"), and persona target_market
        # / role catch niches that aren't in the industry label at all.
        structured = cs.structured or {}

        aliases = [
            str(a).lower()
            for a in (structured.get("industry_aliases") or [])
            if str(a or "").strip()
        ] if isinstance(structured.get("industry_aliases"), list) else []
        for alias in aliases:
            if not alias:
                continue
            if bucket and alias in bucket:
                score += 4
            if bucket_industry and (alias == bucket_industry or alias in bucket_industry):
                score += 4

        persona = structured.get("persona") if isinstance(structured.get("persona"), dict) else {}
        if persona:
            target = (persona.get("target_market") or "").lower()
            role = (persona.get("role") or "").lower()
            for needle in (target, role):
                if not needle:
                    continue
                if bucket and needle in bucket:
                    score += 2
                if bucket_industry and needle in bucket_industry:
                    score += 2

        return score

    scored = [(relevance(cs), cs) for cs in all_studies]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Return top 3 (prioritise matched, then fill with others). Surface
    # `structured` (verbatim quote, before/after metrics, pain points, outcomes,
    # persona) so the copy generator has clean signal instead of having to
    # re-parse the narrative `content` blob.
    top = scored[:3]
    return [
        {
            "title": cs.title or "",
            "client_name": cs.client_name or "",
            "industry": cs.industry or "",
            "tags": cs.tags or [],
            "content": cs.content,
            "structured": cs.structured or None,
        }
        for _, cs in top
    ]


def _build_system_prompt(
    universal_content: str,
    format_content: str,
    principles: list[str],
    example_outputs: list[dict],
) -> str:
    principles_block = "\n".join(f"- {p}" for p in principles)

    examples_block = ""
    if example_outputs:
        examples = []
        for ex in example_outputs[:3]:  # max 3 few-shot examples to keep context lean
            examples.append(
                f"### {ex.get('label', 'Example')}\n"
                f"**TITLE:** {ex.get('title', '')}\n\n"
                f"**DESCRIPTION:**\n{ex.get('description', '')}"
            )
        examples_block = "\n\n---\n\n".join(examples)

    return f"""You are a direct-response copywriter for Quantum Scaling (QS), a B2B growth agency that helps coaches, consultants, and service businesses scale with AI-powered webinar acquisition systems.

You generate calendar blocker copy (LinkedIn calendar invites): a title and description that get professionals to click YES or MAYBE to attend a webinar.

{_AUTHORITY_NOTE}

## Business Context
{universal_content}

## Format Rules
{format_content}

## Copywriting Principles (HIGHEST AUTHORITY — these override everything else, including the Examples)
{principles_block}

## Real Examples (STRUCTURE & VOICE ONLY — match their shape and tone; never copy their numbers, percentages, metrics, names, or claims)
{examples_block}

## Output Format
Respond with valid JSON only. No markdown, no explanation, no preamble.

{{
  "variants": [
    {{
      "variant": "A",
      "style": "Outcome-first (Hormozi style)",
      "title": "...",
      "description": "..."
    }},
    {{
      "variant": "B",
      "style": "Mechanism-first (Kennedy style)",
      "title": "...",
      "description": "..."
    }},
    {{
      "variant": "C",
      "style": "Audience segment-tension",
      "title": "...",
      "description": "..."
    }}
  ]
}}

Rules:
- Generate exactly 3 variants (A, B, C) as specified above
- Copywriting Principles are the highest authority — if a style label above or an example conflicts with a principle, follow the principle
- Examples are structural templates only — never reuse their numbers, percentages, or metrics; every value comes from the Principles and the provided brief
- All client proof numbers must be verbatim from the provided brief — never fabricate
- If the proof-story client's industry doesn't match the target segment, recast them into the segment's industry (keep the real name, realistic numbers) — never surface a different industry
- Deliverability (hard): descriptions never contain a "$" or revenue figure — use activity metrics (calls/month, attendees/month, conversion %); no banned jargon (pipeline, funnel, acquisition engine, demand on repeat, qualified sales calls); no negative-comparison ("without ...", "no retainer"). Titles are "How ... using [tool]", 40-60 chars, no curiosity opener, no emoji.
- Each description must follow the 9-part structure from the format rules
- Titles must pass the gut check: target segment reads it and thinks "oh shit, that's for me"
"""


def _build_user_prompt(
    segment: str,
    sub_niche: str | None,
    topic: str | None,
    client_story: str | None,
) -> str:
    parts = [f"Generate a calendar blocker for this target segment: **{segment}**"]

    if sub_niche:
        parts.append(f"Sub-niche: {sub_niche}")

    if topic:
        parts.append(f"Webinar topic: {topic}")
    else:
        parts.append("Webinar topic: AI-powered webinar growth system (default)")

    if client_story:
        parts.append(
            f"\nClient proof story to use (verbatim numbers only):\n{client_story}"
        )
    else:
        parts.append(
            "\nNo specific client story provided — select the best matching case study "
            "from the examples, or use segment-agnostic framing if no match exists."
        )

    return "\n".join(parts)


async def generate_calendar_blocker(
    db: AsyncSession,
    user_id: str,
    segment: str,
    sub_niche: str | None = None,
    topic: str | None = None,
    client_story: str | None = None,
) -> dict:
    """Non-streaming generation. Returns parsed JSON with 3 variants."""
    universal, format_rules, principles, examples = await _load_brain_context(
        db, user_id, "calendar_event"
    )

    system = _build_system_prompt(universal, format_rules, principles, examples)
    user_msg = _build_user_prompt(segment, sub_niche, topic, client_story)

    logger.info(
        "Generating calendar blocker — segment=%s sub_niche=%s", segment, sub_niche
    )

    message = await _client.messages.create(
        model=settings.CLAUDE_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    asyncio.create_task(_log_claude_cost(
        model=settings.CLAUDE_MODEL,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        session_id=f"{user_id}:{segment}",
        session_label=f"Calendar blocker — {segment}",
    ))

    raw = message.content[0].text.strip()

    # Strip markdown code fences if model wraps in ```json
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("JSON parse failed: %s\nRaw: %s", e, raw[:500])
        raise ValueError(f"Model returned invalid JSON: {e}") from e

    return await _gate_variants(result)


async def stream_calendar_blocker(
    db: AsyncSession,
    user_id: str,
    segment: str,
    sub_niche: str | None = None,
    topic: str | None = None,
    client_story: str | None = None,
) -> AsyncIterator[str]:
    """
    Streaming SSE generator. Yields text chunks as they arrive from Claude.
    Final chunk is a JSON object with the full parsed result.
    """
    universal, format_rules, principles, examples = await _load_brain_context(
        db, user_id, "calendar_event"
    )

    system = _build_system_prompt(universal, format_rules, principles, examples)
    user_msg = _build_user_prompt(segment, sub_niche, topic, client_story)

    full_text = []

    async with _client.messages.stream(
        model=settings.CLAUDE_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    ) as stream:
        async for chunk in stream.text_stream:
            full_text.append(chunk)
            yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"
        _final_msg = await stream.get_final_message()

    asyncio.create_task(_log_claude_cost(
        model=settings.CLAUDE_MODEL,
        input_tokens=_final_msg.usage.input_tokens,
        output_tokens=_final_msg.usage.output_tokens,
        session_id=f"{user_id}:{segment}",
        session_label=f"Calendar blocker (stream) — {segment}",
    ))

    # Parse and emit final result
    raw = "".join(full_text).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
        result = await _gate_variants(result)
        yield f"data: {json.dumps({'type': 'done', 'result': result})}\n\n"
    except json.JSONDecodeError as e:
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


# ── Bucket Copy Generation ────────────────────────────────────────────────

def _format_case_studies(case_studies: list[dict] | None) -> str:
    """
    Format case studies for inclusion in prompts. When a case study has a
    `structured` payload (from the URL importer) we render it as discrete
    fields so the LLM can lift verbatim quotes and exact before→after metrics
    instead of reverse-engineering them from the narrative blob.
    """
    if not case_studies:
        return "No case studies available."

    parts: list[str] = []
    for cs in case_studies:
        label = cs.get("title", "Case Study")
        if cs.get("client_name"):
            label += f" ({cs['client_name']})"
        if cs.get("industry"):
            label += f" — {cs['industry']}"

        section: list[str] = [f"### {label}"]

        structured = cs.get("structured") if isinstance(cs.get("structured"), dict) else None
        if structured:
            aliases = [a for a in (structured.get("industry_aliases") or []) if str(a).strip()]
            if aliases:
                section.append("**Industry synonyms:** " + ", ".join(aliases))

            persona = structured.get("persona") if isinstance(structured.get("persona"), dict) else {}
            persona_bits = [
                f"{key.replace('_', ' ').title()}: {val}"
                for key, val in (persona or {}).items()
                if val
            ]
            if persona_bits:
                section.append("**Persona:** " + " · ".join(persona_bits))

            metrics = [m for m in (structured.get("metrics") or []) if isinstance(m, dict)]
            if metrics:
                lines = []
                for m in metrics:
                    label_m = (m.get("label") or "").strip()
                    before = (m.get("before") or "").strip()
                    after = (m.get("after") or "").strip()
                    if before and after:
                        lines.append(f"- {label_m}: {before} → {after}")
                    elif after:
                        lines.append(f"- {label_m}: {after}")
                    elif label_m:
                        lines.append(f"- {label_m}")
                if lines:
                    section.append("**Metrics (use verbatim — do not round or change units):**\n" + "\n".join(lines))

            pain = [p for p in (structured.get("pain_points") or []) if str(p).strip()]
            if pain:
                section.append("**Pain points (client voice):**\n" + "\n".join(f"- {p}" for p in pain))

            outcomes = [o for o in (structured.get("outcomes") or []) if str(o).strip()]
            if outcomes:
                section.append("**Outcomes:**\n" + "\n".join(f"- {o}" for o in outcomes))

            quote = (structured.get("quote") or "").strip()
            if quote:
                section.append(f'**Verbatim testimonial (quote exactly if used):**\n> {quote}')

        narrative = (cs.get("content") or "").strip()
        if narrative:
            section.append(f"**Narrative:**\n{narrative}")

        parts.append("\n\n".join(section))

    return "\n\n---\n\n".join(parts)


def _build_copy_system_prompt(
    universal_content: str,
    format_content: str,
    principles: list[str],
    example_outputs: list[dict],
    copy_type: str,
    count: int,
    case_studies: list[dict] | None = None,
) -> str:
    """Build a system prompt for generating bucket-level title or description copies."""
    principles_block = "\n".join(f"- {p}" for p in principles)

    examples_block = ""
    if example_outputs:
        examples = []
        for ex in example_outputs[:3]:
            if copy_type == "title":
                examples.append(f"- {ex.get('title', '')}")
            else:
                examples.append(f"**{ex.get('label', 'Example')}:**\n{ex.get('description', '')}")
        examples_block = "\n\n".join(examples)

    type_label = "calendar invite titles" if copy_type == "title" else "calendar invite descriptions"
    type_instruction = (
        "Each title must be a single line, punchy, and pass the gut check: "
        "the target segment reads it and thinks 'oh shit, that's for me'."
        if copy_type == "title"
        else "Each description must follow the 9-part structure from the format rules. "
        "It should be compelling enough to make the prospect click YES or MAYBE."
    )

    return f"""You are a direct-response copywriter for Quantum Scaling (QS), a B2B growth agency that helps coaches, consultants, and service businesses scale with AI-powered webinar acquisition systems.

You generate {type_label} for LinkedIn calendar invites that get professionals to attend a webinar.

{_AUTHORITY_NOTE}

## Business Context
{universal_content}

## Format Rules
{format_content}

## Copywriting Principles (HIGHEST AUTHORITY — these override everything else, including the Examples)
{principles_block}

## Real Examples (STRUCTURE & VOICE ONLY — match their shape and tone; never copy their numbers, percentages, metrics, names, or claims)
{examples_block}

## Client Case Studies (proof — adapt to the target bucket)
Pick the single best-matching case study and build the proof story from it.
- If the client's industry MATCHES the target bucket: use their numbers verbatim (expressed as activity metrics per the guardrails) and you may quote the testimonial word-for-word.
- If the client's industry does NOT match the bucket (adjacent or different): RECAST the client into the bucket's industry. Present the person under their real full name, but rewrite their firm/occupation/before-state so they read as operating in the target bucket's industry, and use realistic activity numbers for that niche (keep the shape of the transformation — don't inflate). Do NOT surface the client's original industry anywhere, and adapt or drop any testimonial quote that would reveal it. The reader must see someone in their OWN industry.

{_format_case_studies(case_studies)}

## Output Format
Respond with valid JSON only. No markdown, no explanation, no preamble.

{{
  "copies": [
    "First {copy_type} variant...",
    "Second {copy_type} variant...",
    "Third {copy_type} variant..."
  ]
}}

Rules:
- Generate exactly {count} unique {copy_type} variants
- Copywriting Principles are the highest authority — if an example or angle conflicts with a principle, follow the principle
- Examples are structural templates only — never reuse their numbers, percentages, or metrics; every value comes from the Principles and the provided brief
- Each variant must be meaningfully different in angle/style (outcome, mechanism, segment-tension, etc.)
- The bucket name describes the unique audience segment — use it to tailor the copy so it speaks directly to that specific niche/vertical
- Client proof numbers: verbatim when the client's industry matches the bucket; when recasting a non-matching client into this bucket, use realistic activity numbers for the niche (never inflate)
- Deliverability (hard): descriptions never contain a "$" or revenue figure — express proof as activity metrics (calls/month, attendees/month, conversion %); no banned jargon (pipeline, funnel, acquisition engine, demand on repeat, qualified sales calls); no negative-comparison ("without ...", "no retainer"). Titles are "How ... using [tool]", 40-60 chars, no curiosity opener, no emoji.
- {type_instruction}
"""


def _build_copy_user_prompt(
    bucket_name: str,
    industry: str | None,
    countries: list[str] | None,
    emp_range: str | None,
    copy_type: str,
) -> str:
    """Build the user prompt for bucket copy generation."""
    parts = [f"Generate {copy_type}s for outreach bucket: **{bucket_name}**"]
    parts.append(
        f"\nThe bucket name \"{bucket_name}\" uniquely identifies this audience segment. "
        f"Use it as the primary lens for tailoring the {copy_type} — the copy should feel "
        f"like it was written specifically for people in this niche."
    )

    if industry:
        parts.append(f"Industry/Segment: {industry}")
    if countries:
        parts.append(f"Countries: {', '.join(countries)}")
    if emp_range:
        parts.append(f"Company size: {emp_range}")

    parts.append(
        "\nUse the business context and examples to craft copy that resonates "
        "with this specific audience segment."
    )
    parts.append(
        f"\nProof-story adaptation: the client in the proof story MUST read as operating in \"{bucket_name}\". "
        f"If the best-matching case study's client is in a different industry, recast their firm as a \"{bucket_name}\" business — "
        f"keep the person's real full name and the shape of their transformation, but rewrite their industry, occupation, and "
        f"before-state to fit this bucket, with realistic activity numbers for this niche. Never surface a different industry — "
        f"the reader must see someone exactly like them."
    )

    return "\n".join(parts)


async def generate_bucket_copies(
    db: AsyncSession,
    user_id: str,
    bucket_name: str,
    industry: str | None,
    countries: list[str] | None,
    emp_range: str | None,
    copy_type: str,
    count: int = 3,
) -> list[str]:
    """
    Generate title or description copies for a bucket using the AI brain.
    Returns a list of text strings.
    """
    universal, format_rules, principles, examples = await _load_brain_context(
        db, user_id, "calendar_event"
    )
    case_studies = await _load_case_studies(db, user_id, industry, bucket_name)

    system = _build_copy_system_prompt(
        universal, format_rules, principles, examples, copy_type, count,
        case_studies=case_studies,
    )
    user_msg = _build_copy_user_prompt(
        bucket_name, industry, countries, emp_range, copy_type
    )

    logger.info(
        "Generating %d %s copies — bucket=%s industry=%s case_studies=%d",
        count, copy_type, bucket_name, industry, len(case_studies),
    )

    message = await _client.messages.create(
        model=settings.CLAUDE_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    asyncio.create_task(_log_claude_cost(
        model=settings.CLAUDE_MODEL,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        session_id=f"{user_id}:{bucket_name}",
        session_label=f"Bucket copy ({copy_type}) — {bucket_name}",
    ))

    raw = message.content[0].text.strip()

    # Strip markdown code fences if model wraps in ```json
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("JSON parse failed for bucket copies: %s\nRaw: %s", e, raw[:500])
        raise ValueError(f"Model returned invalid JSON: {e}") from e

    copies = result.get("copies", [])
    if not isinstance(copies, list) or len(copies) == 0:
        raise ValueError("Model did not return any copies")

    variants = [str(c) for c in copies[:count]]
    gated = await _validate_and_repair(variants, copy_type)
    if copy_type == "title":
        gated = await _diversify_titles(gated)
    return gated


async def regenerate_bucket_copy(
    db: AsyncSession,
    user_id: str,
    original_text: str,
    copy_type: str,
    feedback: str,
    bucket_name: str,
    industry: str | None,
) -> str:
    """
    Regenerate a single copy variant using AI, incorporating user feedback.
    Returns the new text string.
    """
    universal, format_rules, principles, examples = await _load_brain_context(
        db, user_id, "calendar_event"
    )
    case_studies = await _load_case_studies(db, user_id, industry, bucket_name)

    principles_block = "\n".join(f"- {p}" for p in principles)
    case_studies_block = _format_case_studies(case_studies)
    type_label = "calendar invite title" if copy_type == "title" else "calendar invite description"

    system = f"""You are a direct-response copywriter for Quantum Scaling (QS).

You are refining a {type_label} based on user feedback.

## Business Context
{universal}

## Format Rules
{format_rules}

## Copywriting Principles (HIGHEST AUTHORITY — these override everything else, including the Examples)
{principles_block}

## Client Case Studies (proof — adapt to the target bucket)
Use the best-matching case study. If the client's industry matches the bucket, use their numbers verbatim; if it does NOT match, recast the client into the bucket's industry (keep the real full name, realistic activity numbers for the niche) and never surface their original industry.
{case_studies_block}

## Output Format
Respond with valid JSON only. No markdown, no explanation.

{{
  "copy": "The refined {copy_type} text..."
}}

Rules:
- Apply the feedback precisely
- Copywriting Principles are the highest authority — keep the refined copy fully compliant with them, even over the original text
- Keep the same general format and structure
- Present the proof-story client as operating in "{bucket_name}"; recast a non-matching client into this industry with realistic numbers (never inflate)
- Deliverability (hard): no "$"/revenue figure in descriptions (use activity metrics); no banned jargon (pipeline, funnel, acquisition engine, demand on repeat, qualified sales calls); no negative-comparison ("without ...", "no retainer"). Titles are "How ... using [tool]", 40-60 chars, no curiosity opener
"""

    user_msg = f"""Original {copy_type}:
\"\"\"{original_text}\"\"\"

Bucket: {bucket_name}
Industry: {industry or 'General'}

User feedback: {feedback}

Generate an improved version that addresses this feedback."""

    logger.info("Regenerating %s — bucket=%s feedback=%s", copy_type, bucket_name, feedback[:100])

    message = await _client.messages.create(
        model=settings.CLAUDE_MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    asyncio.create_task(_log_claude_cost(
        model=settings.CLAUDE_MODEL,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        session_id=f"{user_id}:{bucket_name}",
        session_label=f"Regenerate {copy_type} — {bucket_name}",
    ))

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("JSON parse failed for regeneration: %s\nRaw: %s", e, raw[:500])
        raise ValueError(f"Model returned invalid JSON: {e}") from e

    text = str(result.get("copy", raw))
    return (await _validate_and_repair([text], copy_type))[0]
