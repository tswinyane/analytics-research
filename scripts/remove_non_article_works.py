from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import Work
from app.services.analytics import AnalyticsService
from app.services.harmonizer import is_article_document_type


@dataclass
class RemovalStats:
    scanned_works: int = 0
    non_article_works: int = 0
    source_records_deleted: int = 0
    document_type_breakdown: Counter = field(default_factory=Counter)
    source_breakdown: Counter = field(default_factory=Counter)


def remove_non_article_works(*, apply_changes: bool) -> RemovalStats:
    """One-time cleanup: delete Work rows (and their SourceRecords, via cascade) whose
    document_type doesn't qualify as an article. Companion to merge_duplicate_works.py —
    that script collapses duplicate rows, this one narrows the dataset down to the "Articles
    only from OpenAlex and DSpace" scope. Dry-run by default; pass --apply to actually delete.
    """
    stats = RemovalStats()
    session = SessionLocal()
    try:
        works = session.execute(
            select(Work).options(selectinload(Work.source_records))
        ).scalars().all()
        stats.scanned_works = len(works)

        for work in works:
            if is_article_document_type(work.document_type):
                continue
            stats.non_article_works += 1
            stats.document_type_breakdown[work.document_type or "(none)"] += 1
            for source_name in work.source_names or ["(unknown)"]:
                stats.source_breakdown[source_name] += 1
            stats.source_records_deleted += len(work.source_records or [])
            if apply_changes:
                session.delete(work)

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
        description=(
            "One-time routine to delete Work rows that aren't articles (see "
            "app/services/harmonizer.is_article_document_type for the definition). Run "
            "without --apply first to see what would be removed."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()

    stats = remove_non_article_works(apply_changes=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] scanned_works={stats.scanned_works}")
    print(f"[{mode}] non_article_works={stats.non_article_works}")
    print(f"[{mode}] source_records_deleted={stats.source_records_deleted}")
    print(f"[{mode}] remaining_works={stats.scanned_works - stats.non_article_works}")
    print(f"[{mode}] by document_type (top 20):")
    for document_type, count in stats.document_type_breakdown.most_common(20):
        print(f"[{mode}]   {document_type!r}: {count}")
    print(f"[{mode}] by source_name:")
    for source_name, count in stats.source_breakdown.most_common():
        print(f"[{mode}]   {source_name!r}: {count}")

    if not args.apply and stats.non_article_works:
        print(
            f"[{mode}] Dry run only — re-run with --apply to actually delete "
            f"{stats.non_article_works} works."
        )


if __name__ == "__main__":
    main()
