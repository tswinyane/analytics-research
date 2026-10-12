from __future__ import annotations

import argparse
import sys
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete, inspect, select, update
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import ResearcherWork, SourceRecord, Work
from app.services.analytics import AnalyticsService

TITLE_STOPWORDS = {
    "a", "an", "and", "the", "of", "in", "on", "for", "to", "with", "from", "by",
    "at", "as", "is", "are", "be", "this", "that", "these", "those",
}


@dataclass
class MergeStats:
    scanned_works: int = 0
    duplicate_clusters: int = 0
    duplicate_works: int = 0
    merged_into_primary: int = 0
    source_records_relinked: int = 0
    source_records_deleted: int = 0
    works_deleted: int = 0


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = normalized.encode("ascii", "ignore").decode("ascii").lower()
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    return " ".join(normalized.split())


def _publication_key(work: Work) -> str:
    if work.doi:
        return f"doi:{work.doi.strip().lower()}"
    title_key = _title_fingerprint(work.title)
    if not title_key:
        return ""
    return f"title:{title_key}"


def _title_fingerprint(value: str | None) -> str:
    if not value:
        return ""
    base_title = re.split(r"\s*[:;\-]\s*", value, maxsplit=1)[0]
    normalized = _normalize_text(base_title)
    if not normalized:
        return ""
    tokens = [token for token in normalized.split() if token]
    tokens = [token for token in tokens if token not in TITLE_STOPWORDS and not token.isdigit()]
    if not tokens:
        return ""
    return " ".join(tokens[:14])


def _score_work(work: Work) -> tuple[int, int, int, int]:
    non_empty_fields = sum(
        1
        for value in [
            work.title,
            work.abstract,
            work.doi,
            work.publication_year,
            work.publication_date,
            work.journal,
            work.document_type,
            work.language,
            work.publisher,
            work.open_access_status,
            work.primary_source_url,
        ]
        if value
    )
    source_count = work.source_count or 0
    citation_count = work.citation_count or 0
    authors_count = len(work.authors or [])
    return (non_empty_fields, source_count, citation_count, authors_count)


def _prefer_longer(existing: str | None, incoming: str | None) -> str | None:
    if not incoming:
        return existing
    if not existing:
        return incoming
    return incoming if len(incoming) > len(existing) else existing


def _merge_unique(existing: list[str] | None, incoming: list[str] | None) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*(existing or []), *(incoming or [])]:
        text = " ".join(str(value).split()).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        merged.append(text)
        seen.add(key)
    return merged


def _merge_work_fields(primary: Work, duplicate: Work) -> None:
    primary.title = _prefer_longer(primary.title, duplicate.title)
    primary.abstract = _prefer_longer(primary.abstract, duplicate.abstract)
    primary.doi = primary.doi or duplicate.doi
    primary.publication_year = primary.publication_year or duplicate.publication_year
    primary.publication_date = primary.publication_date or duplicate.publication_date
    primary.journal = primary.journal or duplicate.journal
    primary.document_type = primary.document_type or duplicate.document_type
    primary.language = primary.language or duplicate.language
    primary.publisher = primary.publisher or duplicate.publisher
    primary.open_access_status = primary.open_access_status or duplicate.open_access_status
    primary.primary_source_url = primary.primary_source_url or duplicate.primary_source_url
    primary.citation_count = max(primary.citation_count or 0, duplicate.citation_count or 0) or None

    primary.authors = _merge_unique(primary.authors, duplicate.authors)
    primary.affiliations = _merge_unique(primary.affiliations, duplicate.affiliations)
    primary.keywords = _merge_unique(primary.keywords, duplicate.keywords)
    primary.source_names = _merge_unique(primary.source_names, duplicate.source_names)
    primary.source_count = len(primary.source_names or [])


def _move_source_records(primary: Work, duplicate: Work, stats: MergeStats) -> None:
    existing_keys = {(record.source_name, record.source_record_id) for record in (primary.source_records or [])}
    for record in list(duplicate.source_records or []):
        key = (record.source_name, record.source_record_id)
        if key in existing_keys:
            stats.source_records_deleted += 1
            continue
        record.work_id = primary.id
        stats.source_records_relinked += 1
        existing_keys.add(key)


def _move_researcher_links(session, primary: Work, duplicate: Work) -> None:
    """Re-point ORCID researcher links from a duplicate work to the primary one."""
    already_linked = list(
        session.scalars(select(ResearcherWork.researcher_id).where(ResearcherWork.work_id == primary.id)).all()
    )
    move = update(ResearcherWork).where(ResearcherWork.work_id == duplicate.id)
    if already_linked:
        move = move.where(ResearcherWork.researcher_id.not_in(already_linked))
    session.execute(move.values(work_id=primary.id).execution_options(synchronize_session=False))
    session.execute(
        delete(ResearcherWork)
        .where(ResearcherWork.work_id == duplicate.id)
        .execution_options(synchronize_session=False)
    )


def merge_duplicate_works(*, apply_changes: bool) -> MergeStats:
    stats = MergeStats()
    session = SessionLocal()
    try:
        works = session.execute(
            select(Work).options(selectinload(Work.source_records))
        ).scalars().all()
        stats.scanned_works = len(works)
        has_researcher_links = inspect(session.get_bind()).has_table(ResearcherWork.__tablename__)

        clusters: dict[str, list[Work]] = {}
        for work in works:
            key = _publication_key(work)
            if not key:
                continue
            clusters.setdefault(key, []).append(work)

        duplicate_groups = [group for group in clusters.values() if len(group) > 1]
        stats.duplicate_clusters = len(duplicate_groups)
        stats.duplicate_works = sum(len(group) - 1 for group in duplicate_groups)

        for group in duplicate_groups:
            group_sorted = sorted(
                group,
                key=lambda work: (_score_work(work), -work.id),
                reverse=True,
            )
            primary = group_sorted[0]
            duplicates = group_sorted[1:]

            for duplicate in duplicates:
                _merge_work_fields(primary, duplicate)
                _move_source_records(primary, duplicate, stats)
                if has_researcher_links:
                    _move_researcher_links(session, primary, duplicate)
                session.delete(duplicate)
                stats.works_deleted += 1
                stats.merged_into_primary += 1

        if apply_changes:
            session.commit()
            AnalyticsService().clear_cache()
        else:
            session.rollback()

        return stats
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time routine to merge duplicate Work rows in the database."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()

    stats = merge_duplicate_works(apply_changes=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] scanned_works={stats.scanned_works}")
    print(f"[{mode}] duplicate_clusters={stats.duplicate_clusters}")
    print(f"[{mode}] duplicate_works={stats.duplicate_works}")
    print(f"[{mode}] merged_into_primary={stats.merged_into_primary}")
    print(f"[{mode}] source_records_relinked={stats.source_records_relinked}")
    print(f"[{mode}] source_records_deleted={stats.source_records_deleted}")
    print(f"[{mode}] works_deleted={stats.works_deleted}")


if __name__ == "__main__":
    main()
