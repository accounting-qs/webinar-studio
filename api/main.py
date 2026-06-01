import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db.session import engine
from db.models import Base

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Webinar Studio starting up")
    # Tables are managed by Alembic migrations — do not create_all here

    # Resume in-flight imports orphaned by a backend restart. Each import worker
    # writes processed_rows to its upload row every batch, so we can skip the
    # CSV reader past that point and continue. If we can't resume (missing
    # storage_path / field_mappings), fall back to marking failed so the user
    # knows to retry manually. NOTE: single-instance only — if Render ever
    # runs >1 instance, this needs a row-level lock so only one picks up each.
    try:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession
        from db.models import UploadHistory, WebinarCalendarUpload
        from api.routers.outreach.uploads import resume_orphan_import
        from api.routers.calendar_uploads import resume_orphan_calendar_import

        async with AsyncSession(engine) as db:
            result = await db.execute(
                select(UploadHistory).where(UploadHistory.status.in_(["processing", "paused"]))
            )
            stale = result.scalars().all()
            for u in stale:
                if resume_orphan_import(u):
                    logger.info(f"Resumed orphan import {u.id} ({u.file_name}) from row {u.processed_rows}")
                else:
                    u.status = "failed"
                    u.error_message = "Server restarted during import; resume metadata missing. Please retry."
                    logger.warning(f"Could not resume {u.id} ({u.file_name}) — marked failed")
            if stale:
                await db.commit()

            cal_result = await db.execute(
                select(WebinarCalendarUpload).where(WebinarCalendarUpload.status.in_(["processing", "paused"]))
            )
            cal_stale = cal_result.scalars().all()
            for u in cal_stale:
                if resume_orphan_calendar_import(u):
                    logger.info(f"Resumed orphan calendar import {u.id} ({u.file_name}) from row {u.processed_rows}")
                else:
                    u.status = "failed"
                    u.error_message = "Server restarted during import; resume metadata missing. Please retry."
                    logger.warning(f"Could not resume calendar {u.id} ({u.file_name}) — marked failed")
            if cal_stale:
                await db.commit()
    except Exception as e:
        logger.error(f"Startup recovery failed: {e}")

    # Start GHL sync scheduler (reads interval/weekly schedule from DB)
    try:
        from services import ghl_scheduler
        await ghl_scheduler.start()
    except Exception as e:
        logger.error(f"GHL scheduler start failed: {e}")

    yield

    try:
        from services import ghl_scheduler
        await ghl_scheduler.stop()
    except Exception as e:
        logger.error(f"GHL scheduler stop failed: {e}")

    await engine.dispose()
    logger.info("Webinar Studio shut down")


app = FastAPI(
    title="Webinar Studio API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://competeiq-frontend.onrender.com",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from api.routers import webhook, competitors, ads, generation, outreach, statistics, connectors, ghl_sync, blocklist, chat, calendar_uploads, public_stats
from api.routers.costs import router as costs_router

app.include_router(webhook.router, prefix="/webhook", tags=["webhook"])
app.include_router(competitors.router, prefix="/competitors", tags=["competitors"])
app.include_router(ads.router, prefix="/ads", tags=["ads"])
app.include_router(generation.router, prefix="/generate", tags=["generation"])
app.include_router(outreach.router, prefix="/outreach", tags=["outreach"])
app.include_router(costs_router)
app.include_router(statistics.router, prefix="/statistics", tags=["statistics"])
app.include_router(connectors.router, prefix="/connectors", tags=["connectors"])
app.include_router(ghl_sync.router, prefix="/ghl-sync", tags=["ghl-sync"])
app.include_router(blocklist.router, prefix="/blocklist", tags=["blocklist"])
app.include_router(chat.router, prefix="/chat", tags=["chat"])
app.include_router(calendar_uploads.router, prefix="/calendar-uploads", tags=["calendar-uploads"])
app.include_router(public_stats.router, prefix="/public", tags=["public"])

# Phase 1b — uncomment as built:
# from api.routers import outputs, brain, monitoring
# app.include_router(outputs.router, prefix="/outputs", tags=["outputs"])
# app.include_router(brain.router, prefix="/brain", tags=["brain"])
# app.include_router(monitoring.router, prefix="/monitoring", tags=["monitoring"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "webinar-studio"}
