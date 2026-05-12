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

    # Recover stale imports stuck in "processing" from a previous crash/restart
    try:
        from sqlalchemy import select, update
        from sqlalchemy.ext.asyncio import AsyncSession
        from db.models import UploadHistory
        async with AsyncSession(engine) as db:
            result = await db.execute(
                select(UploadHistory).where(UploadHistory.status.in_(["processing", "paused"]))
            )
            stale = result.scalars().all()
            if stale:
                for u in stale:
                    u.status = "failed"
                    u.error_message = "Server restarted during import. Please retry."
                    logger.warning(f"Marked stale import {u.id} ({u.file_name}) as failed")
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

from api.routers import webhook, competitors, ads, generation, outreach, statistics, connectors, ghl_sync, blocklist, chat
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

# Phase 1b — uncomment as built:
# from api.routers import outputs, brain, monitoring
# app.include_router(outputs.router, prefix="/outputs", tags=["outputs"])
# app.include_router(brain.router, prefix="/brain", tags=["brain"])
# app.include_router(monitoring.router, prefix="/monitoring", tags=["monitoring"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "webinar-studio"}
