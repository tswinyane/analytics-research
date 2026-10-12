from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class HarmonizedPublication:
    source_name: str
    source_record_id: str
    title: str | None = None
    abstract: str | None = None
    doi: str | None = None
    publication_year: int | None = None
    publication_date: date | None = None
    journal: str | None = None
    document_type: str | None = None
    language: str | None = None
    publisher: str | None = None
    open_access_status: str | None = None
    citation_count: int | None = None
    authors: list[str] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    source_url: str | None = None
    raw_payload: dict | None = None

    def to_record(self) -> dict:
        return asdict(self)
