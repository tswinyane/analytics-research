from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Work(Base):
    __tablename__ = "works"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    publication_year: Mapped[int | None] = mapped_column(Integer, index=True)
    publication_date: Mapped[date | None] = mapped_column(Date)
    journal: Mapped[str | None] = mapped_column(String(500))
    document_type: Mapped[str | None] = mapped_column(String(100))
    language: Mapped[str | None] = mapped_column(String(50))
    publisher: Mapped[str | None] = mapped_column(String(255))
    open_access_status: Mapped[str | None] = mapped_column(String(100))
    citation_count: Mapped[int | None] = mapped_column(Integer)
    authors: Mapped[list[str] | None] = mapped_column(JSON)
    affiliations: Mapped[list[str] | None] = mapped_column(JSON)
    keywords: Mapped[list[str] | None] = mapped_column(JSON)
    primary_source_url: Mapped[str | None] = mapped_column(Text)
    source_names: Mapped[list[str] | None] = mapped_column(JSON)
    source_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    source_records: Mapped[list["SourceRecord"]] = relationship(
        back_populates="work",
        cascade="all, delete-orphan",
    )


class SourceRecord(Base):
    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint("source_name", "source_record_id", name="uq_source_record"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    publication_year: Mapped[int | None] = mapped_column(Integer, index=True)
    citation_count: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict | None] = mapped_column(JSON)
    harvested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    work: Mapped[Work] = relationship(back_populates="source_records")


class HarvestRun(Base):
    __tablename__ = "harvest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    records_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_upserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_message: Mapped[str | None] = mapped_column(Text)


class HarvestState(Base):
    __tablename__ = "harvest_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    state_payload: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class AuthorAlias(Base):
    __tablename__ = "author_aliases"
    __table_args__ = (
        UniqueConstraint("source_name", "identity_key", name="uq_author_alias_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    identity_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class Researcher(Base):
    """A researcher recorded by the platform. Every researcher MUST have an ORCID iD.

    `orcid` is the bare iD (e.g. 0000-0002-1825-0097), unique and NOT NULL, so the
    database itself rejects any attempt to record a researcher without one.
    """

    __tablename__ = "researchers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    orcid: Mapped[str] = mapped_column(String(19), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), index=True)
    given_names: Mapped[str | None] = mapped_column(String(255))
    family_name: Mapped[str | None] = mapped_column(String(255))
    credit_name: Mapped[str | None] = mapped_column(String(255))
    affiliations: Mapped[list[str] | None] = mapped_column(JSON)
    openalex_author_id: Mapped[str | None] = mapped_column(String(32), index=True)
    discovered_via: Mapped[str] = mapped_column(String(50), default="orcid", nullable=False)
    orcid_works_last_modified: Mapped[int | None] = mapped_column(BigInteger)
    orcid_harvested_at: Mapped[datetime | None] = mapped_column(DateTime)
    openalex_harvested_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    work_links: Mapped[list["ResearcherWork"]] = relationship(
        back_populates="researcher",
        cascade="all, delete-orphan",
    )


class ResearcherWork(Base):
    """Links an ORCID-identified researcher to a canonical work."""

    __tablename__ = "researcher_works"
    __table_args__ = (
        UniqueConstraint("researcher_id", "work_id", name="uq_researcher_work"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    researcher_id: Mapped[int] = mapped_column(ForeignKey("researchers.id"), nullable=False, index=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), nullable=False, index=True)
    linked_via: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    researcher: Mapped[Researcher] = relationship(back_populates="work_links")
