from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

from sqlalchemy import case, func, or_, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.connectors.openalex import OpenAlexConnector
from app.database import SessionLocal
from app.models import Researcher, ResearcherWork
from app.services.harmonizer import is_article_document_type, normalize_publication
from app.services.pipeline import IngestionPipeline


def main() -> None:
    args = _parse_args()
    connector = OpenAlexConnector()
    pipeline = IngestionPipeline()
    totals = {
        "researchers_processed": 0,
        "researchers_skipped": 0,
        "records_fetched": 0,
        "records_selected": 0,
        "records_upserted": 0,
        "researcher_work_links": 0,
    }

    session = SessionLocal()
    try:
        researchers = _select_researchers(session, args)
        print(f"researchers_selected={len(researchers)}")
        for index, researcher in enumerate(researchers, start=1):
            label = researcher.display_name or researcher.orcid
            openalex_author_id = researcher.openalex_author_id
            orcid = researcher.orcid
            print(f"[{index}/{len(researchers)}] {label} orcid={orcid} openalex_author_id={openalex_author_id or ''}")

            if not openalex_author_id and not orcid:
                totals["researchers_skipped"] += 1
                print("  skipped=no_openalex_author_id_or_orcid")
                continue

            if openalex_author_id:
                fetched = list(connector.fetch_for_author(openalex_author_id, limit=args.max_works_per_researcher))
                fetch_mode = "openalex_author_id"
            else:
                fetched = list(connector.fetch_for_orcid(orcid))
                fetch_mode = "orcid"
                if args.max_works_per_researcher > 0:
                    fetched = fetched[: args.max_works_per_researcher]

            publications = [normalize_publication(item) for item in fetched]
            if args.article_only:
                publications = [p for p in publications if is_article_document_type(p.document_type)]

            counters = {"researcher_work_links": 0}
            upserted = pipeline._save_openalex_publications(session, publications, counters)
            researcher.openalex_harvested_at = datetime.utcnow()
            session.commit()

            totals["researchers_processed"] += 1
            totals["records_fetched"] += len(fetched)
            totals["records_selected"] += len(publications)
            totals["records_upserted"] += upserted
            totals["researcher_work_links"] += int(counters.get("researcher_work_links") or 0)
            print(
                "  "
                f"mode={fetch_mode} fetched={len(fetched)} selected={len(publications)} "
                f"upserted={upserted} links={counters.get('researcher_work_links', 0)}"
            )
    finally:
        session.close()

    print("summary=")
    for key, value in totals.items():
        print(f"  {key}={value}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harvest OpenAlex full author profiles in controlled batches.")
    parser.add_argument("--limit-researchers", type=int, default=25, help="Researchers to process this run.")
    parser.add_argument(
        "--max-works-per-researcher",
        type=int,
        default=0,
        help="Maximum OpenAlex works per researcher; 0 means all works.",
    )
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="Only process matching researcher names. May be passed more than once.",
    )
    parser.add_argument(
        "--min-current-links",
        type=int,
        default=0,
        help="Only process researchers with at least this many existing researcher-work links.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Include researchers already marked as OpenAlex-expanded.",
    )
    parser.add_argument("--article-only", action="store_true", help="Keep only article/review records.")
    return parser.parse_args()


def _select_researchers(session, args: argparse.Namespace) -> list[Researcher]:
    link_count = func.count(ResearcherWork.id).label("link_count")
    query = (
        select(Researcher, link_count)
        .outerjoin(ResearcherWork, ResearcherWork.researcher_id == Researcher.id)
        .where(or_(Researcher.openalex_author_id.is_not(None), Researcher.orcid.is_not(None)))
        .group_by(Researcher.id)
    )
    if not args.force:
        query = query.where(Researcher.openalex_harvested_at.is_(None))
    if args.min_current_links > 0:
        query = query.having(link_count >= args.min_current_links)

    name_filters = []
    for name in args.name or []:
        cleaned = " ".join(str(name or "").split()).strip()
        if not cleaned:
            continue
        pattern = f"%{cleaned}%"
        name_filters.append(
            or_(
                Researcher.display_name.ilike(pattern),
                Researcher.credit_name.ilike(pattern),
                Researcher.given_names.ilike(pattern),
                Researcher.family_name.ilike(pattern),
            )
        )
    if name_filters:
        query = query.where(or_(*name_filters))

    query = query.order_by(
        case((Researcher.openalex_harvested_at.is_(None), 0), else_=1),
        link_count.desc(),
        Researcher.id.asc(),
    )
    if args.limit_researchers > 0:
        query = query.limit(args.limit_researchers)
    return [researcher for researcher, _ in session.execute(query).all()]


if __name__ == "__main__":
    main()
