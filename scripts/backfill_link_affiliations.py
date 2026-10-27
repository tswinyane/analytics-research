"""Decide which researcher works count towards University of Pretoria totals.

A work counts for a researcher when they are currently at UP (all their works count), or
when they were at UP on that work (former staff keep only their UP-affiliated works).
See app/services/affiliation.py. Harvests keep this up to date for new links; run this
once after upgrading, and whenever you want to re-check everyone:

    python scripts/backfill_link_affiliations.py                 # everything
    python scripts/backfill_link_affiliations.py --offline       # no OpenAlex calls
    python scripts/backfill_link_affiliations.py --orcid 0000-0002-9183-9614

Pin someone's current status by hand (automatic refreshes never overwrite it):

    python scripts/backfill_link_affiliations.py --set-current 0000-0002-9183-9614 no
    python scripts/backfill_link_affiliations.py --set-current 0000-0002-9183-9614 auto   # un-pin
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from sqlalchemy import case, func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal, engine
from app.models import Researcher, ResearcherWork
from app.services.affiliation import (
    counted_link_condition,
    ensure_researcher_work_affiliation_column,
    refresh_current_status,
    refresh_link_affiliations,
    set_current_status_manually,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--orcid", help="Only this researcher.")
    parser.add_argument("--offline", action="store_true", help="Skip OpenAlex; use local evidence only.")
    parser.add_argument("--set-current", nargs=2, metavar=("ORCID", "yes|no|auto"), help="Pin current-UP status.")
    parser.add_argument("--top", type=int, default=15, help="Show the N researchers with the biggest change.")
    args = parser.parse_args()

    ensure_researcher_work_affiliation_column(engine)
    session = SessionLocal()
    try:
        if args.set_current:
            orcid, choice = args.set_current
            value = {"yes": True, "no": False, "auto": None}.get(choice.lower())
            if choice.lower() not in {"yes", "no", "auto"}:
                raise SystemExit("--set-current takes yes, no or auto")
            researcher = set_current_status_manually(session, orcid, value)
            if researcher is None:
                raise SystemExit(f"No researcher with ORCID {orcid}")
            print(f"{researcher.display_name} ({researcher.orcid}): currently at UP = {choice}")
            if value is None:
                print(refresh_current_status(session, [researcher.id], use_openalex=not args.offline))
            _report(session, [researcher.id], args.top)
            return

        query = select(Researcher.id).order_by(Researcher.id)
        if args.orcid:
            query = query.where(Researcher.orcid == args.orcid.strip().upper())
        researcher_ids = list(session.scalars(query).all())
        started = time.monotonic()

        print(f"1/2 Checking which of {len(researcher_ids)} researcher(s) are currently at UP...")
        status = refresh_current_status(session, researcher_ids, use_openalex=not args.offline)
        print(
            f"    currently at UP: {status['current']}, former: {status['former']}, "
            f"unknown: {status['unknown']} (from OpenAlex: {status['from_openalex']}, "
            f"changed: {status['changed']})"
        )

        print("2/2 Checking each researcher's works for a UP affiliation...")
        totals = {"links": 0, "at_institution": 0, "changed": 0}
        for position, researcher_id in enumerate(researcher_ids, start=1):
            result = refresh_link_affiliations(session, researcher_id)
            session.commit()
            for key in totals:
                totals[key] += result[key]
            if position % 200 == 0:
                print(f"    {position}/{len(researcher_ids)} researchers, {time.monotonic() - started:.0f}s")
        print(
            f"    links: {totals['links']}, listing UP on the paper: {totals['at_institution']}, "
            f"updated: {totals['changed']}"
        )
        _report(session, researcher_ids, args.top)
    finally:
        session.close()


def _report(session, researcher_ids: list[int], top: int) -> None:
    counted = func.sum(case((counted_link_condition(), 1), else_=0))
    rows = session.execute(
        select(
            Researcher.display_name,
            Researcher.orcid,
            Researcher.currently_at_institution,
            Researcher.current_affiliation_source,
            func.count(ResearcherWork.id),
            counted,
        )
        .join(ResearcherWork, ResearcherWork.researcher_id == Researcher.id)
        .where(Researcher.id.in_(researcher_ids))
        .group_by(Researcher.id)
    ).all()
    counted_total = sum(int(r[5] or 0) for r in rows)
    linked_total = sum(int(r[4] or 0) for r in rows)
    print(f"\nWorks counted towards researchers: {counted_total} of {linked_total} linked")
    reductions = sorted((r for r in rows if int(r[5] or 0) < int(r[4] or 0)), key=lambda r: int(r[5] or 0) - int(r[4]))
    if reductions:
        print("Biggest reductions (linked works -> counted works):")
        for name, orcid, current, source, linked, kept in reductions[:top]:
            label = "current" if current else ("former" if current is False else "unknown")
            print(f"  {int(linked):>6} -> {int(kept or 0):<6} {name} ({orcid}) [{label}, {source or '-'}]")


if __name__ == "__main__":
    main()
