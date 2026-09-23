"""
Outreach router — combined from domain-specific sub-routers.
Mounted at /outreach in main.py.
"""
from fastapi import APIRouter

from api.routers.outreach.buckets import router as buckets_router
from api.routers.outreach.senders import router as senders_router
from api.routers.outreach.webinars import router as webinars_router
from api.routers.outreach.uploads import router as uploads_router
from api.routers.outreach.contacts import router as contacts_router
from api.routers.outreach.brain import router as brain_router
from api.routers.outreach.releases import router as releases_router
from api.routers.outreach.skarpe import router as skarpe_router

router = APIRouter()
router.include_router(buckets_router)
router.include_router(senders_router)
router.include_router(webinars_router)
router.include_router(uploads_router)
router.include_router(contacts_router)
router.include_router(brain_router)
router.include_router(releases_router)
router.include_router(skarpe_router)
