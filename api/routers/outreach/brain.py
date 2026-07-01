"""Outreach sub-router: Brain management (principles, case studies, brain content)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from api.routers.outreach._helpers import LLOYD_USER_ID
from api.schemas import (
    PrincipleCreate, PrincipleUpdate,
    PrincipleReconcileRequest, PrincipleReconcileApply,
    CaseStudyCreate, CaseStudyUpdate, CaseStudyImportRequest,
    BrainContentUpdate,
)
from db.models import CopywritingPrinciple, CaseStudy, UniversalBrain, FormatBrain
from db.session import get_db
from services.brain_reconcile import propose_principle_changes
from services.case_study_import import (
    CaseStudyImportError, import_case_study_from_url,
)

VALID_KNOWLEDGE_TYPES = ("brand", "copy_general", "copy_format", "learned")

logger = logging.getLogger(__name__)

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════
# PRINCIPLES
# ═══════════════════════════════════════════════════════════════════════════

def _principle_dict(p: CopywritingPrinciple) -> dict:
    return {
        "id": p.id,
        "principle_text": p.principle_text,
        "knowledge_type": p.knowledge_type,
        "category": p.category,
        "is_active": p.is_active,
        "display_order": p.display_order,
        "times_applied": p.times_applied,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/brain/principles")
async def list_principles(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(CopywritingPrinciple).where(
            CopywritingPrinciple.user_id == LLOYD_USER_ID,
            CopywritingPrinciple.deleted_at.is_(None),
        ).order_by(CopywritingPrinciple.display_order, CopywritingPrinciple.created_at)
    )
    return [_principle_dict(p) for p in result.scalars().all()]


@router.post("/brain/principles", status_code=201)
async def create_principle(
    body: PrincipleCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    # Get next display_order
    max_order = await db.scalar(
        select(CopywritingPrinciple.display_order).where(
            CopywritingPrinciple.user_id == LLOYD_USER_ID,
            CopywritingPrinciple.deleted_at.is_(None),
        ).order_by(CopywritingPrinciple.display_order.desc()).limit(1)
    )
    next_order = (max_order or 0) + 1

    principle = CopywritingPrinciple(
        user_id=LLOYD_USER_ID,
        principle_text=body.principle_text,
        knowledge_type=body.knowledge_type,
        category=body.category,
        source="authored",
        display_order=next_order,
        is_active=True,
    )
    db.add(principle)
    await db.flush()
    return _principle_dict(principle)


@router.put("/brain/principles/{principle_id}")
async def update_principle(
    principle_id: str,
    body: PrincipleUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(CopywritingPrinciple).where(
            CopywritingPrinciple.id == principle_id,
            CopywritingPrinciple.user_id == LLOYD_USER_ID,
        )
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Principle not found")

    if body.principle_text is not None:
        p.principle_text = body.principle_text
    if body.category is not None:
        p.category = body.category
    if body.is_active is not None:
        p.is_active = body.is_active

    await db.flush()
    return _principle_dict(p)


@router.delete("/brain/principles/{principle_id}", status_code=204)
async def delete_principle(
    principle_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    from datetime import datetime, timezone
    result = await db.execute(
        select(CopywritingPrinciple).where(
            CopywritingPrinciple.id == principle_id,
            CopywritingPrinciple.user_id == LLOYD_USER_ID,
        )
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Principle not found")
    p.deleted_at = datetime.now(timezone.utc)
    await db.flush()


@router.post("/brain/principles/reconcile")
async def reconcile_principles(
    body: PrincipleReconcileRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Given a natural-language instruction, return a proposed set of add/edit/delete
    operations reconciled against the whole principle library. Writes nothing."""
    instruction = (body.instruction or "").strip()
    if not instruction:
        raise HTTPException(400, "instruction is required")
    try:
        return await propose_principle_changes(db, LLOYD_USER_ID, instruction)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/brain/principles/reconcile/apply")
async def apply_reconcile(
    body: PrincipleReconcileApply,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Apply an approved set of reconcile operations in one transaction."""
    from datetime import datetime, timezone

    max_order = await db.scalar(
        select(CopywritingPrinciple.display_order).where(
            CopywritingPrinciple.user_id == LLOYD_USER_ID,
            CopywritingPrinciple.deleted_at.is_(None),
        ).order_by(CopywritingPrinciple.display_order.desc()).limit(1)
    )
    next_order = (max_order or 0) + 1

    for op in body.operations:
        kind = op.op
        if kind == "add":
            text = (op.new_text or "").strip()
            if not text:
                continue
            kt = op.knowledge_type if op.knowledge_type in VALID_KNOWLEDGE_TYPES else "copy_general"
            db.add(CopywritingPrinciple(
                user_id=LLOYD_USER_ID,
                principle_text=text,
                knowledge_type=kt,
                category=op.category,
                source="authored",
                display_order=next_order,
                is_active=True,
            ))
            next_order += 1
        elif kind in ("edit", "delete"):
            if not op.principle_id:
                continue
            p = await db.scalar(
                select(CopywritingPrinciple).where(
                    CopywritingPrinciple.id == op.principle_id,
                    CopywritingPrinciple.user_id == LLOYD_USER_ID,
                    CopywritingPrinciple.deleted_at.is_(None),
                )
            )
            if not p:
                continue
            if kind == "edit":
                text = (op.new_text or "").strip()
                if text:
                    p.principle_text = text
                if op.category is not None:
                    p.category = op.category
            else:  # delete
                p.deleted_at = datetime.now(timezone.utc)
                p.is_active = False

    await db.flush()

    result = await db.execute(
        select(CopywritingPrinciple).where(
            CopywritingPrinciple.user_id == LLOYD_USER_ID,
            CopywritingPrinciple.deleted_at.is_(None),
        ).order_by(CopywritingPrinciple.display_order, CopywritingPrinciple.created_at)
    )
    return [_principle_dict(p) for p in result.scalars().all()]


# ═══════════════════════════════════════════════════════════════════════════
# CASE STUDIES
# ═══════════════════════════════════════════════════════════════════════════

def _case_study_dict(cs: CaseStudy) -> dict:
    return {
        "id": cs.id,
        "title": cs.title,
        "client_name": cs.client_name,
        "industry": cs.industry,
        "tags": cs.tags or [],
        "content": cs.content,
        "is_active": cs.is_active,
        "source_url": cs.source_url,
        "structured": cs.structured,
        "created_at": cs.created_at.isoformat() if cs.created_at else None,
    }


@router.get("/brain/case-studies")
async def list_case_studies(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(CaseStudy).where(
            CaseStudy.user_id == LLOYD_USER_ID,
        ).order_by(CaseStudy.created_at.desc())
    )
    return [_case_study_dict(cs) for cs in result.scalars().all()]


@router.post("/brain/case-studies", status_code=201)
async def create_case_study(
    body: CaseStudyCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    cs = CaseStudy(
        user_id=LLOYD_USER_ID,
        title=body.title,
        client_name=body.client_name,
        industry=body.industry,
        tags=body.tags,
        content=body.content,
        is_active=True,
        source_url=body.source_url,
        structured=body.structured,
    )
    db.add(cs)
    await db.flush()
    return _case_study_dict(cs)


@router.post("/brain/case-studies/import", status_code=201)
async def import_case_study(
    body: CaseStudyImportRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "url must start with http:// or https://")

    try:
        extracted = await import_case_study_from_url(db, url, notes=body.notes)
    except CaseStudyImportError as exc:
        raise HTTPException(400, str(exc))

    cs = CaseStudy(
        user_id=LLOYD_USER_ID,
        title=extracted["title"],
        client_name=extracted.get("client_name"),
        industry=extracted.get("industry"),
        tags=extracted.get("tags") or [],
        content=extracted["content"],
        is_active=True,
        source_url=extracted.get("source_url"),
        structured=extracted.get("structured"),
    )
    db.add(cs)
    await db.flush()
    return _case_study_dict(cs)


@router.post("/brain/case-studies/{case_study_id}/reextract")
async def reextract_case_study(
    case_study_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Re-fetch the source URL and re-run extraction in place. Updates the
    existing row so existing references / IDs keep working."""
    result = await db.execute(
        select(CaseStudy).where(
            CaseStudy.id == case_study_id,
            CaseStudy.user_id == LLOYD_USER_ID,
        )
    )
    cs = result.scalar_one_or_none()
    if not cs:
        raise HTTPException(404, "Case study not found")
    if not cs.source_url:
        raise HTTPException(
            400, "This case study has no source URL — re-extract is only available for URL imports."
        )

    try:
        extracted = await import_case_study_from_url(db, cs.source_url)
    except CaseStudyImportError as exc:
        raise HTTPException(400, str(exc))

    cs.title = extracted["title"]
    cs.client_name = extracted.get("client_name")
    cs.industry = extracted.get("industry")
    cs.tags = extracted.get("tags") or []
    cs.content = extracted["content"]
    cs.structured = extracted.get("structured")
    await db.flush()
    return _case_study_dict(cs)


@router.put("/brain/case-studies/{case_study_id}")
async def update_case_study(
    case_study_id: str,
    body: CaseStudyUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(CaseStudy).where(
            CaseStudy.id == case_study_id,
            CaseStudy.user_id == LLOYD_USER_ID,
        )
    )
    cs = result.scalar_one_or_none()
    if not cs:
        raise HTTPException(404, "Case study not found")

    if body.title is not None:
        cs.title = body.title
    if body.client_name is not None:
        cs.client_name = body.client_name
    if body.industry is not None:
        cs.industry = body.industry
    if body.tags is not None:
        cs.tags = body.tags
    if body.content is not None:
        cs.content = body.content
    if body.is_active is not None:
        cs.is_active = body.is_active
    if body.source_url is not None:
        cs.source_url = body.source_url or None
    if body.structured is not None:
        cs.structured = body.structured or None

    await db.flush()
    return _case_study_dict(cs)


@router.delete("/brain/case-studies/{case_study_id}", status_code=204)
async def delete_case_study(
    case_study_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(CaseStudy).where(
            CaseStudy.id == case_study_id,
            CaseStudy.user_id == LLOYD_USER_ID,
        )
    )
    cs = result.scalar_one_or_none()
    if not cs:
        raise HTTPException(404, "Case study not found")
    await db.delete(cs)
    await db.flush()


# ═══════════════════════════════════════════════════════════════════════════
# BRAIN CONTENT (Universal + Format)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/brain/content")
async def get_brain_content(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    ub = await db.scalar(
        select(UniversalBrain).where(UniversalBrain.user_id == LLOYD_USER_ID)
    )
    fb = await db.scalar(
        select(FormatBrain).where(
            FormatBrain.user_id == LLOYD_USER_ID,
            FormatBrain.format_key == "calendar_event",
            FormatBrain.deleted_at.is_(None),
        )
    )
    return {
        "universal_brain": ub.brain_content if ub else "",
        "format_brain": fb.brain_content if fb else "",
        "format_brain_id": fb.id if fb else None,
    }


@router.put("/brain/content/universal")
async def update_universal_brain(
    body: BrainContentUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    ub = await db.scalar(
        select(UniversalBrain).where(UniversalBrain.user_id == LLOYD_USER_ID)
    )
    if ub:
        ub.brain_content = body.brain_content
        ub.version += 1
    else:
        ub = UniversalBrain(
            user_id=LLOYD_USER_ID,
            brain_content=body.brain_content,
            version=1,
        )
        db.add(ub)
    await db.flush()
    return {"brain_content": ub.brain_content, "version": ub.version}


@router.put("/brain/content/format")
async def update_format_brain(
    body: BrainContentUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    fb = await db.scalar(
        select(FormatBrain).where(
            FormatBrain.user_id == LLOYD_USER_ID,
            FormatBrain.format_key == "calendar_event",
            FormatBrain.deleted_at.is_(None),
        )
    )
    if fb:
        fb.brain_content = body.brain_content
    else:
        fb = FormatBrain(
            user_id=LLOYD_USER_ID,
            format_key="calendar_event",
            display_name="Calendar Event",
            brain_content=body.brain_content,
            is_active=True,
        )
        db.add(fb)
    await db.flush()
    return {"brain_content": fb.brain_content}
