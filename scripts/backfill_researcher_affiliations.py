from __future__ import annotations

from pathlib import Path
import re
import sys

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal
from app.models import Researcher, SourceRecord


ORCID_PATTERN = re.compile(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}")


def main() -> None:
    affiliations_by_orcid: dict[str, list[str]] = {}
    scanned = 0

    session = SessionLocal()
    try:
        records = session.execute(
            select(SourceRecord.raw_payload).where(SourceRecord.source_name == "openalex")
        ).yield_per(500)
        for (payload,) in records:
            scanned += 1
            if not isinstance(payload, dict):
                continue
            for authorship in payload.get("authorships") or []:
                author = authorship.get("author") or {}
                orcid = _normalize_orcid(author.get("orcid"))
                if not orcid:
                    continue
                affiliations = _authorship_affiliations(authorship)
                if affiliations:
                    affiliations_by_orcid[orcid] = _merge_text_lists(
                        affiliations_by_orcid.get(orcid),
                        affiliations,
                    )

        updated = 0
        for start in range(0, len(affiliations_by_orcid), 500):
            orcids = list(affiliations_by_orcid)[start:start + 500]
            researchers = session.scalars(select(Researcher).where(Researcher.orcid.in_(orcids))).all()
            for researcher in researchers:
                merged = _merge_text_lists(
                    researcher.affiliations,
                    affiliations_by_orcid.get(researcher.orcid),
                )
                if merged != (researcher.affiliations or []):
                    researcher.affiliations = merged
                    updated += 1
            session.commit()
    finally:
        session.close()

    print(f"openalex_records_scanned={scanned}")
    print(f"orcids_with_affiliation_clues={len(affiliations_by_orcid)}")
    print(f"researchers_updated={updated}")


def _authorship_affiliations(authorship: dict) -> list[str]:
    affiliations: list[str] = []
    for value in authorship.get("raw_affiliation_strings") or []:
        text = " ".join(str(value or "").split()).strip()
        if text:
            affiliations.append(text)
    for institution in authorship.get("institutions") or []:
        name = " ".join(str(institution.get("display_name") or "").split()).strip()
        if name:
            affiliations.append(name)
    return _merge_text_lists(None, affiliations)


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


def _normalize_orcid(value: str | None) -> str | None:
    match = ORCID_PATTERN.search(str(value or ""))
    return match.group(0).upper() if match else None


if __name__ == "__main__":
    main()
