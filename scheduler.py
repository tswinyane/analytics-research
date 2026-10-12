from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import settings
from app.database import init_db
from app.services.pipeline import IngestionPipeline


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def run_harvest_job() -> None:
    logger.info("Starting scheduled harvest")
    pipeline = IngestionPipeline()
    summary = pipeline.ingest_sources(limit=settings.harvest_default_limit)
    logger.info("Scheduled harvest finished: %s", summary)


def main() -> None:
    init_db()

    if settings.run_harvest_on_startup:
        run_harvest_job()

    scheduler = BlockingScheduler(timezone="Africa/Johannesburg")
    scheduler.add_job(
        run_harvest_job,
        trigger="cron",
        hour=settings.harvest_schedule_hour,
        minute=settings.harvest_schedule_minute,
        id="daily_harvest",
        replace_existing=True,
    )
    logger.info(
        "Daily harvest scheduled for %02d:%02d Africa/Johannesburg",
        settings.harvest_schedule_hour,
        settings.harvest_schedule_minute,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
