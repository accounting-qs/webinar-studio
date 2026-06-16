"""SOP videos router — list the SOP walkthroughs and edit them in-app.

The set of rows is fixed (seeded by migration 049); the UI only edits a row's
title, subtitle, and Loom URL, so there are no create/delete endpoints.
All routes require bearer auth.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from db.models import SopVideo
from db.session import get_db

router = APIRouter(dependencies=[Depends(require_auth)])


class SopVideoUpdate(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    loom_url: str | None = None


class SopVideoResponse(BaseModel):
    id: str
    title: str
    subtitle: str
    loom_url: str
    display_order: int
    created_at: datetime | None
    updated_at: datetime | None

    class Config:
        from_attributes = True


@router.get("", response_model=list[SopVideoResponse])
async def list_sop_videos(db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(SopVideo).order_by(SopVideo.display_order, SopVideo.created_at)
    )
    return list(rows.scalars().all())


@router.put("/{video_id}", response_model=SopVideoResponse)
async def update_sop_video(
    video_id: str,
    body: SopVideoUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SopVideo).where(SopVideo.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(404, "SOP video not found")

    fields = body.model_dump(exclude_unset=True)
    for field, val in fields.items():
        setattr(video, field, val)
    await db.flush()
    return video
