from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

from sqlalchemy import or_, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.connectors.openalex import OpenAlexConnector
from app.database import SessionLocal
from app.models import Researcher
from app.services.harmonizer import is_article_document_type, normalize_publication
from app.services.pipeline import IngestionPipeline


def main() -> None:
    args = _parse_args()
    session = SessionLocal()
    try:
        researcher = _find_researcher(session, args)
        if researcher is None and not (args.openalex_author_id or args.orcid):
            raise SystemExit("No matching researcher found. Pass --orcid or --openalex-author-id explicitly.")

        openalex_author_id = args.openalex_author_id or (researcher.openalex_author_id if researcher else None)
        orcid = args.orcid or (researcher.orcid if researcher else None)
        display_name = args.name or (researcher.display_name if researcher else None) or orcid or openalex_author_id
        print(f"target={display_name}")
        print(f"orcid={orcid or ''}")
        print(f"openalex_author_id={openalex_author_id or ''}")

        connector = OpenAlexConnector()
        if openalex_author_id:
            fetched = list(connector.fetch_for_author(openalex_author_id, limit=args.limit))
            fetch_mode = "openalex_author_id"
        elif orcid:
            fetched = list(connector.fetch_for_orcid(orcid))
            fetch_mode = "orcid"
            if args.limit > 0:
                fetched = fetched[: args.limit]
        else:
            raise SystemExit("Need --orcid or --openalex-author-id to harvest.")

        publications = [normalize_publication(item) for item in fetched]
        if args.article_only:
            publications = [p for p in publications if is_article_document_type(p.document_type)]

        counters = {
            "researcher_work_links": 0,
            "records_fetched": len(fetched),
            "records_selected": len(publications),
        }
        pipeline = IngestionPipeline()
        upserted = pipeline._save_openalex_publications(session, publications, counters)
        if researcher is not None:
            if openalex_author_id and not researcher.openalex_author_id:
                researcher.openalex_author_id = pipeline._openalex_short_id(openalex_author_id)
            researcher.openalex_harvested_at = datetime.utcnow()
        session.commit()

        print(f"fetch_mode={fetch_mode}")
        print(f"records_fetched={len(fetched)}")
        print(f"records_selected={len(publications)}")
        print(f"records_upserted={upserted}")
        print(f"researcher_work_links={counters.get('researcher_work_links', 0)}")
    finally:
        session.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harvest one researcher's OpenAlex works.")
    parser.add_argument("--name", help="Researcher display/name search, e.g. 'Rangan Gupta'.")
    parser.add_argument("--orcid", help="Researcher ORCID iD.")
    parser.add_argument("--openalex-author-id", help="OpenAlex author id, e.g. A5014077031.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum works to fetch; 0 means all.")
    parser.add_argument("--article-only", action="store_true", help="Keep only article/review records.")
    return parser.parse_args()


def _find_researcher(session, args: argparse.Namespace) -> Researcher | None:
    if args.orcid:
        researcher = session.scalar(select(Researcher).where(Researcher.orcid == args.orcid))
        if researcher is not None:
            return researcher
    if args.openalex_author_id:
        author_id = args.openalex_author_id.rstrip("/").rsplit("/", 1)[-1]
        researcher = session.scalar(select(Researcher).where(Researcher.openalex_author_id == author_id))
        if researcher is not None:
            return researcher
    if not args.name:
        return None

    name = " ".join(args.name.split()).strip()
    if not name:
        return None
    pattern = f"%{name}%"
    rows = session.scalars(
        select(Researcher)
        .where(
            or_(
                Researcher.display_name.ilike(pattern),
                Researcher.credit_name.ilike(pattern),
                Researcher.given_names.ilike(pattern),
                Researcher.family_name.ilike(pattern),
            )
        )
        .order_by(Researcher.id.asc())
        .limit(10)
    ).all()
    if len(rows) == 1:
        return rows[0]
    if rows:
        print("Multiple researchers matched; use --orcid or --openalex-author-id:")
        for row in rows:
            print(f"  id={row.id} name={row.display_name} orcid={row.orcid} openalex_author_id={row.openalex_author_id}")
        raise SystemExit(2)
    return None


if __name__ == "__main__":
    main()
