from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import SourceRecord, Work
from app.services.analytics import AnalyticsService


HOME_INSTITUTION = "University of Pretoria"

HEADERS = [
    "Article Title",
    "Main Author",
    "Publication Year",
    "Faculty",
    "Department",
    "Institution Affiliation",
    "Main Institution",
    "Collaborating Institution",
    "Article URI",
    "DOI",
]


def clean_text(value) -> str:
    return " ".join(str(value or "").split()).strip()


def load_existing_affiliations(path: Path) -> dict[tuple[str, str], str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    affiliations: dict[tuple[str, str], str] = {}
    for record in payload.get("records", []):
        title = clean_text(record.get("Article Title")).lower()
        doi = clean_text(record.get("DOI")).lower()
        affiliation = clean_text(record.get("Institution Affiliation"))
        if title and affiliation:
            affiliations[(title, doi)] = affiliation
            affiliations.setdefault((title, ""), affiliation)
    return affiliations


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned_values: list[str] = []
    for value in values:
        cleaned = clean_text(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned_values.append(cleaned)
    return cleaned_values


def openalex_institutions_by_work_id(records: list[SourceRecord]) -> dict[int, list[str]]:
    institutions_by_work_id: dict[int, list[str]] = {}
    for record in records:
        payload = record.raw_payload or {}
        institutions: list[str] = []
        for authorship in payload.get("authorships", []):
            for institution in authorship.get("institutions", []):
                name = institution.get("display_name")
                if name:
                    institutions.append(name)
        if institutions:
            institutions_by_work_id[record.work_id] = unique_values(institutions)
    return institutions_by_work_id


def split_institutions(institutions: list[str]) -> tuple[str, str, str]:
    normalized = unique_values(institutions)
    home_matches = [name for name in normalized if name.lower() == HOME_INSTITUTION.lower()]
    main = home_matches[0] if home_matches else (normalized[0] if normalized else HOME_INSTITUTION)
    collaborators = [
        name
        for name in normalized
        if name.lower() != main.lower() and name.lower() != HOME_INSTITUTION.lower()
    ]
    affiliation_text = "; ".join(normalized)
    return main, "; ".join(collaborators), affiliation_text


def extract_doi(service: AnalyticsService, work: Work, source_record: SourceRecord) -> str:
    doi = clean_text(work.doi or source_record.doi)
    if doi:
        return doi

    payload = source_record.raw_payload or {}
    for identifier in service._dspace_values(payload, "identifier"):
        match = re.search(r"\b10\.\d{4,9}/[^\s,\];)]+", identifier, flags=re.IGNORECASE)
        if match:
            return match.group(0).rstrip(".")
    return ""


def extract_uri(service: AnalyticsService, work: Work, source_record: SourceRecord, doi: str) -> str:
    payload = source_record.raw_payload or {}
    identifiers = service._dspace_values(payload, "identifier")
    for identifier in identifiers:
        cleaned = clean_text(identifier)
        if cleaned.startswith("http://hdl.handle.net/") or cleaned.startswith("https://hdl.handle.net/"):
            return cleaned
    for identifier in identifiers:
        cleaned = clean_text(identifier)
        if cleaned.startswith(("http://", "https://")) and "doi.org/" not in cleaned.lower():
            return cleaned
    if source_record.source_url:
        return clean_text(source_record.source_url)
    if work.primary_source_url:
        return clean_text(work.primary_source_url)
    return f"https://doi.org/{doi}" if doi else ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export platform article metadata for Excel workbook creation."
    )
    parser.add_argument("--output", required=True, help="JSON output path")
    args = parser.parse_args()

    output_path = Path(args.output)
    existing_affiliations = load_existing_affiliations(output_path)
    service = AnalyticsService()

    records: list[dict[str, str]] = []
    session = SessionLocal()
    try:
        rows = session.execute(
            select(Work, SourceRecord)
            .join(SourceRecord, SourceRecord.work_id == Work.id)
            .where(SourceRecord.source_name == "dspace")
        ).all()
        openalex_records = session.execute(
            select(SourceRecord).where(SourceRecord.source_name == "openalex")
        ).scalars().all()
    finally:
        session.close()

    openalex_institutions = openalex_institutions_by_work_id(openalex_records)

    seen_work_ids: set[int] = set()
    for work, source_record in rows:
        if work.id in seen_work_ids:
            continue
        seen_work_ids.add(work.id)

        payload = source_record.raw_payload or {}
        source_document_type = service._source_record_document_type(payload)
        if source_document_type:
            is_article = service._is_article_document_type(source_document_type)
        else:
            is_article = service._is_article_work(work)
        if not is_article:
            continue

        title = clean_text(source_record.title or service._first_dspace_value(payload, "title") or work.title)
        publication_year = service._normalize_publication_year(
            source_record.publication_year or work.publication_year
        )
        dspace_authors = service._dspace_creators(payload)
        main_author = dspace_authors[0] if dspace_authors else ""
        faculty, department = service._dspace_faculty_department(
            dspace_payload=payload,
            title=title,
            journal=work.journal,
            publisher=work.publisher,
            keywords=work.keywords or [],
        )
        doi = extract_doi(service, work, source_record)
        affiliations = openalex_institutions.get(work.id) or work.affiliations or []
        if not isinstance(affiliations, list):
            affiliations = [affiliations]
        main_institution, collaborating_institution, affiliation_text = split_institutions(affiliations)
        affiliation_text = affiliation_text or existing_affiliations.get((title.lower(), doi.lower()), "")
        affiliation_text = affiliation_text or existing_affiliations.get((title.lower(), ""), "")
        if affiliation_text and not collaborating_institution:
            _, collaborating_institution, _ = split_institutions(affiliation_text.split(";"))
        article_uri = extract_uri(service, work, source_record, doi)

        records.append(
            {
                "Article Title": title,
                "Main Author": clean_text(main_author),
                "Publication Year": str(publication_year or ""),
                "Faculty": clean_text(faculty),
                "Department": clean_text(department),
                "Institution Affiliation": affiliation_text,
                "Main Institution": main_institution,
                "Collaborating Institution": collaborating_institution,
                "Article URI": article_uri,
                "DOI": doi,
            }
        )

    records.sort(
        key=lambda row: (
            -(int(row["Publication Year"]) if row["Publication Year"].isdigit() else 0),
            row["Article Title"].lower(),
            row["Main Author"].lower(),
            row["DOI"].lower(),
        )
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"headers": HEADERS, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Exported {len(records)} article metadata rows to {output_path}")


if __name__ == "__main__":
    main()
