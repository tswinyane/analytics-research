from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Researcher, ResearcherWork, Work
from app.services.faculties import classify_record


def main() -> None:
    session = SessionLocal()
    try:
        total_works = session.scalar(select(func.count()).select_from(Work)) or 0
        total_researchers = session.scalar(select(func.count()).select_from(Researcher)) or 0
        total_links = session.scalar(select(func.count()).select_from(ResearcherWork)) or 0
        researchers_with_affiliations = (
            session.scalar(
                select(func.count())
                .select_from(Researcher)
                .where(Researcher.affiliations.is_not(None))
            )
            or 0
        )

        print(f"works={total_works}")
        print(f"researchers={total_researchers}")
        print(f"researcher_work_links={total_links}")
        print(f"researchers_with_affiliations={researchers_with_affiliations}")

        rows = session.execute(
            select(Work, Researcher.affiliations)
            .join(ResearcherWork, ResearcherWork.work_id == Work.id)
            .join(Researcher, Researcher.id == ResearcherWork.researcher_id)
            .where(Researcher.affiliations.is_not(None))
            .limit(1000)
        ).all()
    finally:
        session.close()

    counts: Counter[tuple[str, str]] = Counter()
    for work, affiliations in rows:
        combined_affiliations = _merge_text_lists(work.affiliations, affiliations)
        faculty, department = classify_record(
            title=work.title,
            abstract=work.abstract,
            journal=work.journal,
            publisher=work.publisher,
            authors=work.authors or [],
            affiliations=combined_affiliations,
            keywords=work.keywords or [],
        )
        counts[(faculty, department)] += 1

    print("sampled_linked_work_mappings=")
    for (faculty, department), count in counts.most_common(20):
        print(f"  {count}\t{faculty}\t{department}")


def _merge_text_lists(*values: list[str] | None) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            text = " ".join(str(item or "").split()).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return merged


if __name__ == "__main__":
    main()
