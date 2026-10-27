from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import analytics, router
from app.config import settings
from app.database import engine, init_db
from app.services.affiliation import ensure_researcher_work_affiliation_column

logger = logging.getLogger(__name__)


def _warm_dashboard_cache_safely() -> None:
    try:
        analytics.warm_dashboard_cache()
    except Exception:
        logger.exception("Dashboard cache warm-up failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.init_db_on_startup:
        logger.info("Initializing database schema during application startup")
        await asyncio.to_thread(init_db)
    else:
        logger.info("Skipping database schema initialization during application startup")
    try:
        await asyncio.to_thread(ensure_researcher_work_affiliation_column, engine)
    except Exception:
        logger.exception("Could not ensure researcher_works.at_institution column")
    warmup_task = asyncio.create_task(asyncio.to_thread(_warm_dashboard_cache_safely))
    try:
        yield
    finally:
        if not warmup_task.done():
            warmup_task.cancel()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)

@app.get("/")
def root() -> dict:
    return {
        "message": "UP Research Analytics API",
        "health": "/health",
        "overview": "/api/overview",
    }

@app.get("/health")
def health_check() -> dict:
    return {"status": "ok", "application": settings.app_name}
