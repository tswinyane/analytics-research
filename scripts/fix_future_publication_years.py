"""Correct publication years that lie in the future (e.g. 2027).

Journals often assign online-first articles to a future issue, and OpenAlex/ORCID then
report that future year. This sets such works to the year they first appeared in OpenAlex
(its created_date, when harvested) or otherwise the current year, and clears publication
dates later than today. New harvests apply the same rule automatically.

    python scripts/fix_future_publication_years.py            # show what would change
    python scripts/fix_future_publication_years.py --apply    # change it
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from pathlib import Path
import sys

from sqlalchemy import or_, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal
from app.models import SourceRecord, Work
from app.services.harmonizer import correct_future_publication_date


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write the corrections (default: dry run).")
    args = parser.parse_args()
    today = date.today()

    session = SessionLocal()
    try:
        works = session.scalars(
            select(Work).where(or_(Work.publication_year > today.year, Work.publication_date > today))
        ).all()
        records = {}
        if works:
            for work_id, payload in session.execute(
                select(SourceRecord.work_id, SourceRecord.raw_payload)
                .where(SourceRecord.work_id.in_([w.id for w in works]))
                .where(SourceRecord.source_name == "openalex")
            ).all():
                if isinstance(payload, dict) and payload.get("created_date"):
                    records[work_id] = payload["created_date"]

        changes = Counter()
        for work in works:
            new_year, new_date = correct_future_publication_date(
                work.publication_year, work.publication_date, records.get(work.id), today
            )
            changes[(work.publication_year, new_year)] += 1
            if args.apply:
                work.publication_year = new_year
                work.publication_date = new_date

        source_rows = session.scalars(
            select(SourceRecord).where(SourceRecord.publication_year > today.year)
        ).all()
        if args.apply:
            for record in source_rows:
                record.publication_year, _ = correct_future_publication_date(
                    record.publication_year, None, records.get(record.work_id), today
                )
            session.commit()

        print(f"Works with a future year or date: {len(works)}")
        for (old, new), count in sorted(changes.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
            print(f"  {old} -> {new}: {count}")
        print(f"Source records with a future year: {len(source_rows)}")
        if not args.apply:
            print("\nDry run - nothing changed. Re-run with --apply to write these corrections.")
            return
        print("\nCorrected.")
    finally:
        session.close()

    # Dashboard/publication caches hold the old years; drop them so they rebuild.
    for name in ("dashboard_cache.json", "dashboard_rows_cache.json", "publications_browse_cache.json"):
        path = PROJECT_ROOT / "logs" / name
        try:
            path.unlink()
            print(f"Removed cached {path.name}")
        except FileNotFoundError:
            pass
    print("Restart the backend so it picks up the corrected years.")


if __name__ == "__main__":
    main()
