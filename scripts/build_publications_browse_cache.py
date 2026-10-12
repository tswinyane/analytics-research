from __future__ import annotations

import time

from app.database import init_db
from app.services.analytics import AnalyticsService


def main() -> None:
    init_db()
    service = AnalyticsService()
    started = time.perf_counter()
    rows = service._publication_browse_base_rows()
    elapsed = time.perf_counter() - started
    print(
        "Built publications browse cache: "
        f"rows={len(rows)} elapsed_seconds={elapsed:.2f}"
    )


if __name__ == "__main__":
    main()
